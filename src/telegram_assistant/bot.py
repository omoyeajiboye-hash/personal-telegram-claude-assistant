"""Telegram long-polling bot controller, command handlers, serialization, and safe replies."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

import httpx
from telegram import Update
from telegram.error import InvalidToken, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from .claude_client import ClaudeAssistantService
from .config import Config
from .db import Database, RecoveryReport
from .ops import clear_local_stop, is_stop_requested
from .security import SecretRedactor, is_authorized_owner, split_telegram_message
from .tools import LocalToolExecutor

logger = logging.getLogger(__name__)

SendFn = Callable[[int, str], Awaitable[None]]

HELP_TEXT = """Personal Telegram Assistant (powered by Claude)

Capabilities:
- Answer questions, explain concepts, draft text, and handle follow-up conversations
- Read permitted text files in your approved assistant folder
- Create new notes in that folder (never silently overwrites existing files)
- View your task list (tasks.md)
- Overwrite an existing note or run a predefined safe workflow (only after explicit single-use confirmation)
- Open allowlisted Windows apps (e.g., Chrome, Edge, Notepad, Calculator, Explorer, Word, Excel)
- System power controls (lock, sleep, restart, shutdown with safety delay — always requires single-use confirmation; cancel_shutdown aborts a pending shutdown/restart)

Commands:
/start, /help — Show this guide and examples
/new — Start a fresh conversation (retained records are kept)
/status — Show local status, active task, configured model, and cached service status
/usage — Show recorded monthly tokens, estimated spend, and budget
/cancel — Cancel queued work and request cancellation at safe checkpoints
/pause — Disable new local actions while keeping chat access
/resume — Re-enable local actions
/forget — Clear retained conversation history per documented retention policy
/confirm <token> — Approve a pending protected action once
/deny <token> — Reject a pending protected action

Examples:
- "Explain how SQLite WAL mode works"
- "Read notes.txt and summarize it"
- "Create a note called meeting-summary"
- "Show my task list"
- "Open Chrome" / "What apps can you open?"
- "Shut down my laptop" / "Lock my screen" / "Cancel shutdown"
"""


class AssistantBotController:
    """Core bot orchestration decoupled from Telegram transport for deterministic testing."""

    def __init__(
        self,
        *,
        config: Config,
        db: Database,
        tools: LocalToolExecutor,
        claude: ClaudeAssistantService,
        redactor: SecretRedactor,
    ) -> None:
        self.config = config
        self.db = db
        self.tools = tools
        self.claude = claude
        self.redactor = redactor

        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._active_cancel_events: dict[int, asyncio.Event] = {}
        self._active_task_ids: dict[int, str] = {}
        self._global_semaphore = asyncio.Semaphore(config.max_concurrent_jobs)
        self.last_recovery_report: RecoveryReport | None = None

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    def startup_maintenance(self) -> RecoveryReport:
        """Run crash reconciliation and retention pruning before accepting updates."""
        report = self.db.reconcile_on_startup(
            stale_task_max_age_seconds=self.config.stale_task_max_age_seconds
        )
        pruned = self.db.prune_expired_conversations(self.config.retention_days)
        self.last_recovery_report = report
        logger.info(
            "startup_maintenance needs_review=%d expired_pending=%d expired_approvals=%d cleared_res=%d pruned_msgs=%d",
            len(report.needs_review_task_ids),
            len(report.expired_pending_task_ids),
            len(report.expired_approval_tokens),
            report.cleared_reservations,
            pruned,
        )
        return report

    def check_owner(self, *, user_id: int | None, chat_id: int | None, chat_type: str | None) -> bool:
        authorized = is_authorized_owner(
            user_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            owner_user_id=self.config.owner_user_id,
            owner_chat_id=self.config.owner_chat_id,
        )
        if not authorized:
            logger.warning(
                "unauthorized_access_rejected chat_type=%s has_user=%s has_chat=%s",
                chat_type or "unknown",
                user_id is not None,
                chat_id is not None,
            )
            self.db.log_audit(
                event_type="unauthorized_access",
                status="rejected",
                metadata={"chat_type": chat_type or "unknown"},
            )
        return authorized

    async def send_safe_reply(self, send_fn: SendFn, chat_id: int, text: str) -> bool:
        """Redact secrets, split into Telegram-safe chunks, and retry ONLY transport errors."""
        sanitized = self.redactor.redact(text)
        chunks = split_telegram_message(sanitized)
        for chunk in chunks:
            sent = False
            for attempt in range(3):
                try:
                    await send_fn(chat_id, chunk)
                    self.db.set_cached_service_status("telegram", "ok")
                    sent = True
                    break
                except RetryAfter as exc:
                    delay = min(10.0, float(getattr(exc, "retry_after", 1.0)))
                    await asyncio.sleep(delay)
                except (NetworkError, TimedOut, ConnectionError, TimeoutError):
                    self.db.set_cached_service_status("telegram", "network_error")
                    if attempt + 1 < 3:
                        await asyncio.sleep(0.5 * (2**attempt))
                except Exception:
                    self.db.set_cached_service_status("telegram", "error")
                    logger.exception("telegram_reply_failed chat_id=%s", chat_id)
                    return False
            if not sent:
                logger.error("telegram_reply_exhausted_retries chat_id=%s", chat_id)
                return False
        return True

    async def handle_update(
        self,
        *,
        update_id: int,
        user_id: int | None,
        chat_id: int | None,
        chat_type: str | None,
        text: str | None,
        send_fn: SendFn,
    ) -> str:
        """Process one incoming Telegram text/command update end-to-end.

        Returns an internal status tag ('ok', 'unauthorized', 'duplicate', 'ignored', 'failed').
        """
        if not self.check_owner(user_id=user_id, chat_id=chat_id, chat_type=chat_type):
            return "unauthorized"

        assert chat_id is not None and user_id is not None

        if not self.db.claim_update(update_id, chat_id, user_id):
            logger.info("duplicate_update_ignored update_id=%d", update_id)
            return "duplicate"

        raw_text = (text or "").strip()
        if not raw_text:
            self.db.update_processed_status(update_id, "ignored_empty")
            await self.send_safe_reply(
                send_fn,
                chat_id,
                "Text messages only in this first release. Send a text question or /help.",
            )
            return "ignored"

        if raw_text.startswith("/"):
            return await self._dispatch_command(
                update_id=update_id,
                user_id=user_id,
                chat_id=chat_id,
                raw_text=raw_text,
                send_fn=send_fn,
            )

        return await self._handle_chat_message(
            update_id=update_id,
            user_id=user_id,
            chat_id=chat_id,
            user_text=raw_text,
            send_fn=send_fn,
        )

    async def _dispatch_command(
        self,
        *,
        update_id: int,
        user_id: int,
        chat_id: int,
        raw_text: str,
        send_fn: SendFn,
    ) -> str:
        parts = raw_text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]

        if cmd in ("/start", "/help"):
            await self.send_safe_reply(send_fn, chat_id, HELP_TEXT)
            self.db.update_processed_status(update_id, "done")
            return "ok"

        if cmd == "/new":
            conv_id = self.db.start_new_conversation(chat_id)
            conv_count, _ = self.db.count_retained_conversations(chat_id)
            await self.send_safe_reply(
                send_fn,
                chat_id,
                f"Started a fresh conversation ({conv_id[:8]}). "
                f"Previous retained records remain stored ({conv_count} total conversation(s)); "
                "use /forget to clear retained conversation history.",
            )
            self.db.update_processed_status(update_id, "done")
            return "ok"

        if cmd == "/status":
            paused = self.db.is_actions_paused()
            latest = self.db.get_active_or_latest_task(chat_id)
            needs_review = self.db.get_tasks_needing_review(chat_id)
            tg_status, tg_ts = self.db.get_cached_service_status("telegram")
            an_status, an_ts = self.db.get_cached_service_status("anthropic")
            task_line = (
                f"{latest['id']} ({latest['status']}: {latest['kind']})"
                if latest
                else "none"
            )
            lines = [
                "Local Application Status:",
                f"- Running: yes (local actions: {'PAUSED' if paused else 'ENABLED'})",
                f"- Configured model: {self.config.claude_model}",
                f"- Current / latest task: {task_line}",
                f"- Tasks needing review: {len(needs_review)}",
                f"- Retention policy: {self.config.retention_days} days (backups: {self.config.backup_retention_days} days)",
                f"- Cached Telegram status (not a live probe): {tg_status} (last observed: {tg_ts})",
                f"- Cached Anthropic status (not a live probe): {an_status} (last observed: {an_ts})",
            ]
            if needs_review:
                lines.append("Uncertain tasks after restart (not auto-retried):")
                for item in needs_review[:5]:
                    lines.append(f"  * {item['id']}: {item['summary']} — {item['error']}")
            await self.send_safe_reply(send_fn, chat_id, "\n".join(lines))
            self.db.update_processed_status(update_id, "done")
            return "ok"

        if cmd == "/usage":
            summary = self.db.get_monthly_usage()
            remaining = max(0.0, self.config.monthly_budget_usd - summary.estimated_cost_usd)
            msg = (
                f"Usage for {summary.month_key} (estimated using configured rates):\n"
                f"- Requests recorded: {summary.request_count}\n"
                f"- Input tokens: {summary.input_tokens:,}\n"
                f"- Output tokens: {summary.output_tokens:,}\n"
                f"- Total tokens: {summary.total_tokens:,}\n"
                f"- Estimated spend: ${summary.estimated_cost_usd:.4f}\n"
                f"- In-flight reserved: ${summary.inflight_reserved_usd:.4f}\n"
                f"- Monthly budget: ${self.config.monthly_budget_usd:.2f} (remaining: ${remaining:.4f})\n"
                f"- Rates: ${self.config.input_cost_per_million_usd:.2f}/1M in, "
                f"${self.config.output_cost_per_million_usd:.2f}/1M out"
            )
            await self.send_safe_reply(send_fn, chat_id, msg)
            self.db.update_processed_status(update_id, "done")
            return "ok"

        if cmd == "/pause":
            self.db.set_actions_paused(True)
            await self.send_safe_reply(
                send_fn,
                chat_id,
                "Local actions are now PAUSED. Chat/Q&A remains available. "
                "Use /resume to re-enable local actions, or use the local stop script to shut down the app.",
            )
            self.db.update_processed_status(update_id, "done")
            return "ok"

        if cmd == "/resume":
            self.db.set_actions_paused(False)
            await self.send_safe_reply(send_fn, chat_id, "Local actions are now RESUMED.")
            self.db.update_processed_status(update_id, "done")
            return "ok"

        if cmd == "/cancel":
            cancelled_ids = self.db.cancel_pending_tasks(chat_id)
            ev = self._active_cancel_events.get(chat_id)
            active_id = self._active_task_ids.get(chat_id)
            signalled = False
            if ev is not None:
                ev.set()
                signalled = True
            notes: list[str] = []
            if cancelled_ids:
                notes.append(f"Cancelled queued/pending task(s): {', '.join(cancelled_ids)}.")
            if signalled:
                notes.append(f"Requested safe-checkpoint cancellation for running task {active_id or ''}.")
            if not notes:
                notes.append("No queued or running tasks to cancel.")
            notes.append("Note: /cancel cannot undo actions that have already completed.")
            await self.send_safe_reply(send_fn, chat_id, " ".join(notes))
            self.db.update_processed_status(update_id, "done")
            return "ok"

        if cmd == "/forget":
            async with self._chat_lock(chat_id):
                convs, msgs = self.db.forget_conversation_data(chat_id)
            await self.send_safe_reply(
                send_fn,
                chat_id,
                f"Cleared retained conversation data ({convs} conversation(s), {msgs} message(s)) "
                f"and compacted the local database (VACUUM).\n"
                f"Existing local backups expire automatically after {self.config.backup_retention_days} days "
                "(or can be deleted immediately from your backup folder).",
            )
            self.db.update_processed_status(update_id, "done")
            return "ok"

        if cmd == "/confirm":
            if not args:
                await self.send_safe_reply(send_fn, chat_id, "Usage: /confirm <token>")
                self.db.update_processed_status(update_id, "done")
                return "ok"
            return await self._handle_confirm(
                update_id=update_id,
                user_id=user_id,
                chat_id=chat_id,
                token=args[0].strip(),
                send_fn=send_fn,
            )

        if cmd == "/deny":
            if not args:
                await self.send_safe_reply(send_fn, chat_id, "Usage: /deny <token>")
                self.db.update_processed_status(update_id, "done")
                return "ok"
            ok, msg = self.db.deny_approval(token=args[0].strip(), user_id=user_id, chat_id=chat_id)
            await self.send_safe_reply(send_fn, chat_id, msg)
            self.db.update_processed_status(update_id, "done" if ok else "rejected")
            return "ok"

        await self.send_safe_reply(send_fn, chat_id, f"Unknown command '{cmd}'. Send /help for available commands.")
        self.db.update_processed_status(update_id, "done")
        return "ok"

    async def _handle_confirm(
        self,
        *,
        update_id: int,
        user_id: int,
        chat_id: int,
        token: str,
        send_fn: SendFn,
    ) -> str:
        async with self._chat_lock(chat_id):
            if self.db.is_actions_paused():
                await self.send_safe_reply(
                    send_fn,
                    chat_id,
                    "Cannot execute confirmed action while local actions are PAUSED (/pause). Use /resume first.",
                )
                self.db.update_processed_status(update_id, "rejected_paused")
                return "ok"

            approval, reason = self.db.consume_approval(
                token=token,
                user_id=user_id,
                chat_id=chat_id,
            )
            if approval is None:
                await self.send_safe_reply(send_fn, chat_id, f"Confirmation rejected: {reason}")
                self.db.update_processed_status(update_id, "rejected_approval")
                return "ok"

            self.db.update_task_status(approval.task_id, "running", side_effecting=True)
            # Revalidate permissions & paths immediately before execution
            result = self.tools.execute(
                approval.tool_name,
                approval.arguments,
                actions_paused=self.db.is_actions_paused(),
                confirmed=True,
            )
            if result.ok:
                self.db.update_task_status(approval.task_id, "succeeded", side_effecting=True)
                self.db.log_audit(
                    event_type="confirmed_tool_execution",
                    task_id=approval.task_id,
                    tool_name=approval.tool_name,
                    status="succeeded",
                    metadata={"token": token},
                )
                reply = f"Confirmed action succeeded:\n{result.output}"
            else:
                self.db.update_task_status(
                    approval.task_id,
                    "failed",
                    error=result.error or result.output,
                    side_effecting=True,
                )
                self.db.log_audit(
                    event_type="confirmed_tool_execution",
                    task_id=approval.task_id,
                    tool_name=approval.tool_name,
                    status="failed",
                    metadata={"token": token},
                )
                reply = f"Confirmed action failed:\n{result.output}"

            # Send reply; NEVER retry the side-effecting tool if Telegram reply fails
            await self.send_safe_reply(send_fn, chat_id, reply)
            self.db.update_processed_status(update_id, "done")
            return "ok"

    async def _handle_chat_message(
        self,
        *,
        update_id: int,
        user_id: int,
        chat_id: int,
        user_text: str,
        send_fn: SendFn,
    ) -> str:
        conversation_id = self.db.get_or_create_active_conversation(chat_id)
        task_id = self.db.create_task(
            chat_id=chat_id,
            update_id=update_id,
            conversation_id=conversation_id,
            kind="chat_turn",
            summary="Process owner message",
            status="pending",
        )

        lock = self._chat_lock(chat_id)
        if lock.locked():
            await self.send_safe_reply(
                send_fn,
                chat_id,
                f"Queued your request ({task_id}) behind the active task. Send /cancel to cancel queued work.",
            )

        async with lock:
            # Re-check if task was cancelled while waiting in queue
            task_state = self.db.get_task(task_id)
            if task_state and task_state["status"] == "cancelled":
                self.db.update_processed_status(update_id, "cancelled")
                return "ok"

            async with self._global_semaphore:
                cancel_event = asyncio.Event()
                self._active_cancel_events[chat_id] = cancel_event
                self._active_task_ids[chat_id] = task_id
                self.db.update_task_status(task_id, "running")

                ack_task: asyncio.Task[None] | None = None
                if self.config.long_job_ack_seconds > 0:
                    async def _delayed_ack() -> None:
                        await asyncio.sleep(self.config.long_job_ack_seconds)
                        if not cancel_event.is_set():
                            await self.send_safe_reply(
                                send_fn,
                                chat_id,
                                f"Still working on request {task_id}...",
                            )

                    ack_task = asyncio.create_task(_delayed_ack())

                try:
                    outcome = await self.claude.handle_user_turn(
                        task_id=task_id,
                        conversation_id=conversation_id,
                        user_text=user_text,
                        owner_user_id=user_id,
                        chat_id=chat_id,
                        cancel_event=cancel_event,
                    )
                finally:
                    if ack_task is not None:
                        ack_task.cancel()
                        try:
                            await ack_task
                        except asyncio.CancelledError:
                            pass
                    self._active_cancel_events.pop(chat_id, None)
                    self._active_task_ids.pop(chat_id, None)

                # Persist conversation turns when the request succeeded or proposed confirmation
                if outcome.status in ("succeeded", "waiting_confirmation"):
                    self.db.add_message(conversation_id, "user", user_text)
                    self.db.add_message(conversation_id, "assistant", outcome.reply_text)

                if outcome.status != "waiting_confirmation":
                    self.db.update_task_status(
                        task_id,
                        outcome.status,
                        error=outcome.error,
                        side_effecting=outcome.side_effecting,
                    )

                # Deliver reply; do NOT re-run side-effecting work if Telegram send fails
                await self.send_safe_reply(send_fn, chat_id, outcome.reply_text)
                self.db.update_processed_status(update_id, outcome.status)
                return "ok" if outcome.status != "failed" else "failed"


def _build_telegram_request(config: Config, *, read_timeout: float) -> HTTPXRequest:
    """Create an HTTPXRequest with Windows-friendly timeouts and optional IPv4 binding."""
    proxy = config.telegram_proxy
    has_env_proxy = bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("ALL_PROXY"))
    httpx_kwargs: dict[str, Any] = {}
    # On Windows, broken/slow IPv6 routes to api.telegram.org commonly cause 5s connect timeouts.
    # Bind to 0.0.0.0 to prefer IPv4 when no explicit/environment proxy is configured.
    if config.telegram_force_ipv4 and not proxy and not has_env_proxy:
        httpx_kwargs["transport"] = httpx.AsyncHTTPTransport(local_address="0.0.0.0")

    return HTTPXRequest(
        connect_timeout=config.telegram_connect_timeout,
        read_timeout=read_timeout,
        write_timeout=config.telegram_write_timeout,
        pool_timeout=config.telegram_pool_timeout,
        proxy=proxy,
        httpx_kwargs=httpx_kwargs or None,
    )


async def run_telegram_bot(config: Config) -> None:
    """Start the long-polling Telegram bot with webhook removal and local stop-file support."""
    config.ensure_directories()
    clear_local_stop(config.stop_file)
    redactor = config.build_redactor()
    db = Database(config.db_path)
    tools = LocalToolExecutor(
        approved_folder=config.approved_folder,
        max_file_bytes=config.max_file_bytes,
        max_excerpt_chars=config.max_excerpt_chars,
        enable_system_control=config.enable_system_control,
        confirm_open_app=config.confirm_open_app,
        shutdown_delay_seconds=config.shutdown_delay_seconds,
    )
    claude = ClaudeAssistantService(
        config=config,
        db=db,
        tools=tools,
        redactor=redactor,
    )
    controller = AssistantBotController(
        config=config,
        db=db,
        tools=tools,
        claude=claude,
        redactor=redactor,
    )
    recovery = controller.startup_maintenance()

    api_request = _build_telegram_request(config, read_timeout=config.telegram_read_timeout)
    polling_request = _build_telegram_request(
        config,
        read_timeout=max(config.telegram_read_timeout, 40.0),
    )
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .request(api_request)
        .get_updates_request(polling_request)
        .build()
    )

    async def _send_fn(chat_id: int, text: str) -> None:
        await application.bot.send_message(chat_id=chat_id, text=text)

    async def _on_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        await controller.handle_update(
            update_id=update.update_id,
            user_id=user.id if user else None,
            chat_id=chat.id if chat else None,
            chat_type=chat.type if chat else None,
            text=msg.text if msg else None,
            send_fn=_send_fn,
        )

    for command in ("start", "help", "new", "status", "usage", "cancel", "pause", "resume", "forget", "confirm", "deny"):
        application.add_handler(CommandHandler(command, _on_update))
    application.add_handler(MessageHandler(filters.ALL, _on_update))

    stop_event = asyncio.Event()

    async def _watch_stop_file() -> None:
        while not stop_event.is_set():
            if is_stop_requested(config.stop_file):
                logger.info("local_stop_file_detected initiating_shutdown=true")
                stop_event.set()
                break
            await asyncio.sleep(1.0)

    watcher = asyncio.create_task(_watch_stop_file())
    initialized = False
    try:
        max_attempts = max(1, config.max_api_retries + 1)
        for attempt in range(max_attempts):
            try:
                if not initialized:
                    await application.initialize()
                    initialized = True
                # Section 3: Remove any existing bot webhook before enabling long polling
                await application.bot.delete_webhook(drop_pending_updates=False)
                break
            except InvalidToken:
                db.set_cached_service_status("telegram", "invalid_token")
                logger.error("telegram_startup_invalid_token")
                raise RuntimeError(
                    "Telegram rejected TELEGRAM_BOT_TOKEN as invalid. Verify or rotate the bot token via @BotFather."
                ) from None
            except (TimedOut, NetworkError) as exc:
                db.set_cached_service_status("telegram", "network_error")
                if attempt + 1 >= max_attempts:
                    logger.error("telegram_startup_unreachable attempts=%d", max_attempts)
                    raise RuntimeError(
                        "Could not connect to Telegram API (api.telegram.org) after bounded retries. "
                        "Check your internet connection, firewall/VPN, or configure TELEGRAM_PROXY in .env."
                    ) from exc
                backoff = min(8.0, 1.0 * (2**attempt))
                logger.warning(
                    "telegram_startup_retry attempt=%d backoff_s=%.1f",
                    attempt + 1,
                    backoff,
                )
                await asyncio.sleep(backoff)

        db.set_cached_service_status("telegram", "ok")
        await application.start()
        assert application.updater is not None
        await application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            timeout=25,
            bootstrap_retries=config.max_api_retries,
        )
        logger.info("telegram_bot_started model=%s", config.claude_model)

        if recovery.needs_review_task_ids:
            await controller.send_safe_reply(
                _send_fn,
                config.owner_chat_id,
                "Assistant restarted. The following in-progress task(s) had uncertain outcomes "
                "and were marked needs_review instead of being retried automatically: "
                + ", ".join(recovery.needs_review_task_ids),
            )

        await stop_event.wait()
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        if application.updater and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        if initialized:
            await application.shutdown()
        clear_local_stop(config.stop_file)
        db.close()
        logger.info("telegram_bot_stopped")
