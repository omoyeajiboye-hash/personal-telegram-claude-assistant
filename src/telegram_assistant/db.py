"""SQLite storage layer for conversations, tasks, approvals, updates, usage, and state."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

VALID_TASK_STATUSES = frozenset(
    {
        "pending",
        "running",
        "waiting_confirmation",
        "succeeded",
        "failed",
        "cancelled",
        "needs_review",
    }
)

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_conversations_chat_id
    ON conversations(chat_id, created_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
    ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    user_id INTEGER,
    status TEXT NOT NULL,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    update_id INTEGER,
    chat_id INTEGER NOT NULL,
    conversation_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'running', 'waiting_confirmation',
        'succeeded', 'failed', 'cancelled', 'needs_review'
    )),
    side_effecting INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    token TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'used', 'denied', 'expired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_approvals_status
    ON approvals(status, expires_at);

CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    month_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_month
    ON usage_records(month_key, created_at);

CREATE TABLE IF NOT EXISTS inflight_reservations (
    reservation_id TEXT PRIMARY KEY,
    task_id TEXT,
    estimated_cost_usd REAL NOT NULL,
    month_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    task_id TEXT,
    tool_name TEXT,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def month_key_for(dt: datetime | None = None) -> str:
    d = dt or utc_now()
    return d.strftime("%Y-%m")


def canonical_args_json(arguments: dict[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return encoded, digest


@dataclass(frozen=True)
class ApprovalRecord:
    token: str
    task_id: str
    owner_user_id: int
    chat_id: int
    tool_name: str
    arguments: dict[str, Any]
    args_hash: str
    description: str
    status: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class UsageSummary:
    month_key: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    inflight_reserved_usd: float
    request_count: int


@dataclass(frozen=True)
class RecoveryReport:
    needs_review_task_ids: list[str]
    expired_pending_task_ids: list[str]
    expired_approval_tokens: list[str]
    cleared_reservations: int


class Database:
    """Thread-safe SQLite repository backing the Telegram assistant."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # --- App state ---

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        now = to_iso(utc_now())
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO app_state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def is_actions_paused(self) -> bool:
        return self.get_state("actions_paused", "false") == "true"

    def set_actions_paused(self, paused: bool) -> None:
        self.set_state("actions_paused", "true" if paused else "false")

    def set_cached_service_status(self, service: str, status: str) -> None:
        now = to_iso(utc_now())
        self.set_state(f"cached_status_{service}", status)
        self.set_state(f"cached_checked_at_{service}", now)

    def get_cached_service_status(self, service: str) -> tuple[str, str]:
        status = self.get_state(f"cached_status_{service}", "unknown (not yet checked)")
        checked_at = self.get_state(f"cached_checked_at_{service}", "never")
        return status or "unknown", checked_at or "never"

    # --- Update deduplication ---

    def claim_update(self, update_id: int, chat_id: int | None, user_id: int | None) -> bool:
        """Atomically record update_id. Returns True if new, False if duplicate/replayed."""
        now = to_iso(utc_now())
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO processed_updates(update_id, chat_id, user_id, status, processed_at)
                VALUES (?, ?, ?, 'accepted', ?)
                """,
                (update_id, chat_id, user_id, now),
            )
            return cursor.rowcount == 1

    def update_processed_status(self, update_id: int, status: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE processed_updates SET status = ? WHERE update_id = ?",
                (status, update_id),
            )

    # --- Conversations & Messages ---

    def get_or_create_active_conversation(self, chat_id: int) -> str:
        state_key = f"active_conversation_{chat_id}"
        existing = self.get_state(state_key)
        if existing:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id FROM conversations WHERE id = ? AND archived_at IS NULL",
                    (existing,),
                ).fetchone()
                if row:
                    return str(row["id"])
        return self.start_new_conversation(chat_id)

    def start_new_conversation(self, chat_id: int) -> str:
        """Start a fresh conversation while retaining previous conversations intact."""
        now = to_iso(utc_now())
        new_id = str(uuid.uuid4())
        state_key = f"active_conversation_{chat_id}"
        with self.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET archived_at = ? WHERE chat_id = ? AND archived_at IS NULL",
                (now, chat_id),
            )
            conn.execute(
                "INSERT INTO conversations(id, chat_id, created_at, archived_at) VALUES (?, ?, ?, NULL)",
                (new_id, chat_id, now),
            )
            conn.execute(
                """
                INSERT INTO app_state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (state_key, new_id, now),
            )
        return new_id

    def add_message(self, conversation_id: str, role: str, content: str) -> int:
        now = to_iso(utc_now())
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, now),
            )
            return int(cursor.lastrowid)

    def get_bounded_history(
        self,
        conversation_id: str,
        *,
        max_messages: int,
        max_chars: int,
    ) -> list[dict[str, str]]:
        """Load newest conversation messages up to message count and character limits."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT role, content FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_id, max_messages),
            ).fetchall()

        selected: list[dict[str, str]] = []
        total_chars = 0
        for row in rows:
            content = str(row["content"])
            if selected and (total_chars + len(content) > max_chars):
                break
            if not selected and len(content) > max_chars:
                content = content[:max_chars]
            selected.append({"role": str(row["role"]), "content": content})
            total_chars += len(content)

        selected.reverse()
        # Anthropic Messages API requires conversation history to start with a 'user' message
        while selected and selected[0]["role"] != "user":
            selected.pop(0)
        return selected

    def count_retained_conversations(self, chat_id: int) -> tuple[int, int]:
        with self._lock:
            conv_count = self._conn.execute(
                "SELECT COUNT(*) AS c FROM conversations WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()["c"]
            msg_count = self._conn.execute(
                """
                SELECT COUNT(*) AS c FROM messages m
                JOIN conversations c ON m.conversation_id = c.id
                WHERE c.chat_id = ?
                """,
                (chat_id,),
            ).fetchone()["c"]
            return int(conv_count), int(msg_count)

    def prune_expired_conversations(self, retention_days: int) -> int:
        cutoff = to_iso(utc_now() - timedelta(days=retention_days))
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            deleted_msgs = cur.rowcount
            conn.execute(
                """
                DELETE FROM conversations
                WHERE created_at < ?
                  AND id NOT IN (SELECT DISTINCT conversation_id FROM messages)
                  AND archived_at IS NOT NULL
                """,
                (cutoff,),
            )
            return int(deleted_msgs)

    def forget_conversation_data(self, chat_id: int) -> tuple[int, int]:
        """Clear all retained conversations and messages for chat_id and reclaim SQLite space."""
        now = to_iso(utc_now())
        with self.transaction() as conn:
            conv_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM conversations WHERE chat_id = ?", (chat_id,)
                ).fetchall()
            ]
            msg_count = 0
            if conv_ids:
                placeholders = ",".join("?" for _ in conv_ids)
                msg_count = conn.execute(
                    f"DELETE FROM messages WHERE conversation_id IN ({placeholders})",
                    conv_ids,
                ).rowcount
            conv_count = conn.execute(
                "DELETE FROM conversations WHERE chat_id = ?", (chat_id,)
            ).rowcount
            conn.execute(
                "DELETE FROM app_state WHERE key = ?",
                (f"active_conversation_{chat_id}",),
            )
            conn.execute(
                """
                INSERT INTO app_state(key, value, updated_at)
                VALUES ('last_forget_at', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO audit_events(event_type, task_id, tool_name, status, metadata_json, created_at)
                VALUES ('forget_conversation_data', NULL, NULL, 'succeeded', ?, ?)
                """,
                (json.dumps({"conversations_deleted": conv_count, "messages_deleted": msg_count}), now),
            )
        # Reclaim freed pages so deleted conversation text does not linger in SQLite free pages
        with self._lock:
            self._conn.execute("VACUUM")
        return int(conv_count), int(msg_count)

    # --- Tasks ---

    def create_task(
        self,
        *,
        chat_id: int,
        kind: str,
        summary: str,
        update_id: int | None = None,
        conversation_id: str | None = None,
        side_effecting: bool = False,
        status: str = "pending",
        created_at: datetime | None = None,
    ) -> str:
        if status not in VALID_TASK_STATUSES:
            raise ValueError(f"Invalid task status: {status}")
        task_id = f"task-{uuid.uuid4().hex[:10]}"
        ts = to_iso(created_at or utc_now())
        started_at = ts if status == "running" else None
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    id, update_id, chat_id, conversation_id, kind, status,
                    side_effecting, summary, error, created_at, updated_at, started_at, finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)
                """,
                (
                    task_id,
                    update_id,
                    chat_id,
                    conversation_id,
                    kind,
                    status,
                    1 if side_effecting else 0,
                    summary,
                    ts,
                    ts,
                    started_at,
                ),
            )
        return task_id

    def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        error: str | None = None,
        side_effecting: bool | None = None,
    ) -> None:
        if status not in VALID_TASK_STATUSES:
            raise ValueError(f"Invalid task status: {status}")
        now = to_iso(utc_now())
        with self.transaction() as conn:
            row = conn.execute("SELECT started_at, side_effecting FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return
            started_at = row["started_at"]
            if status == "running" and not started_at:
                started_at = now
            finished_at = now if status in {"succeeded", "failed", "cancelled", "needs_review"} else None
            se_val = int(side_effecting) if side_effecting is not None else int(row["side_effecting"])
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = ?, side_effecting = ?, updated_at = ?,
                    started_at = ?, finished_at = COALESCE(?, finished_at)
                WHERE id = ?
                """,
                (status, error, se_val, now, started_at, finished_at, task_id),
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def get_active_or_latest_task(self, chat_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE chat_id = ?
                ORDER BY
                    CASE status
                        WHEN 'running' THEN 0
                        WHEN 'waiting_confirmation' THEN 1
                        WHEN 'pending' THEN 2
                        WHEN 'needs_review' THEN 3
                        ELSE 4
                    END,
                    updated_at DESC
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_tasks_needing_review(self, chat_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND status = 'needs_review' ORDER BY updated_at DESC",
                (chat_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def cancel_pending_tasks(self, chat_id: int) -> list[str]:
        now = to_iso(utc_now())
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE chat_id = ? AND status IN ('pending', 'waiting_confirmation')",
                (chat_id,),
            ).fetchall()
            ids = [str(r["id"]) for r in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE tasks
                    SET status = 'cancelled', error = 'Cancelled by owner', updated_at = ?, finished_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [now, now, *ids],
                )
                conn.execute(
                    f"""
                    UPDATE approvals
                    SET status = 'denied', resolved_at = ?
                    WHERE task_id IN ({placeholders}) AND status = 'pending'
                    """,
                    [now, *ids],
                )
            return ids

    # --- Single-use Approvals ---

    def create_approval(
        self,
        *,
        task_id: str,
        owner_user_id: int,
        chat_id: int,
        tool_name: str,
        arguments: dict[str, Any],
        description: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        created = now or utc_now()
        expires = created + timedelta(seconds=ttl_seconds)
        token = secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")[:12]
        args_json, args_hash = canonical_args_json(arguments)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO approvals(
                    token, task_id, owner_user_id, chat_id, tool_name,
                    arguments_json, args_hash, description, status,
                    created_at, expires_at, resolved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL)
                """,
                (
                    token,
                    task_id,
                    owner_user_id,
                    chat_id,
                    tool_name,
                    args_json,
                    args_hash,
                    description,
                    to_iso(created),
                    to_iso(expires),
                ),
            )
        return ApprovalRecord(
            token=token,
            task_id=task_id,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            tool_name=tool_name,
            arguments=json.loads(args_json),
            args_hash=args_hash,
            description=description,
            status="pending",
            created_at=to_iso(created),
            expires_at=to_iso(expires),
        )

    def consume_approval(
        self,
        *,
        token: str,
        user_id: int,
        chat_id: int,
        now: datetime | None = None,
    ) -> tuple[ApprovalRecord | None, str]:
        """Atomically validate and mark an approval single-use.

        Returns (ApprovalRecord, '') on success or (None, reason) on rejection.
        """
        check_time = now or utc_now()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE token = ?", (token,)).fetchone()
            if not row:
                return None, "Confirmation token not found."
            if int(row["owner_user_id"]) != int(user_id) or int(row["chat_id"]) != int(chat_id):
                return None, "Confirmation token does not match owner or chat."
            if row["status"] == "used":
                return None, "Confirmation token has already been used."
            if row["status"] == "denied":
                return None, "Confirmation token was previously denied."
            if row["status"] == "expired":
                return None, "Confirmation token has expired."

            expires_at = from_iso(row["expires_at"])
            if check_time > expires_at:
                conn.execute(
                    "UPDATE approvals SET status = 'expired', resolved_at = ? WHERE token = ? AND status = 'pending'",
                    (to_iso(check_time), token),
                )
                conn.execute(
                    """
                    UPDATE tasks SET status = 'cancelled', error = 'Approval expired', updated_at = ?, finished_at = ?
                    WHERE id = ? AND status = 'waiting_confirmation'
                    """,
                    (to_iso(check_time), to_iso(check_time), row["task_id"]),
                )
                return None, "Confirmation token has expired."

            args_json = str(row["arguments_json"])
            expected_hash = hashlib.sha256(args_json.encode("utf-8")).hexdigest()
            if expected_hash != row["args_hash"]:
                return None, "Confirmation arguments integrity check failed."

            cur = conn.execute(
                """
                UPDATE approvals
                SET status = 'used', resolved_at = ?
                WHERE token = ? AND status = 'pending'
                """,
                (to_iso(check_time), token),
            )
            if cur.rowcount != 1:
                return None, "Confirmation token is no longer valid."

            return (
                ApprovalRecord(
                    token=str(row["token"]),
                    task_id=str(row["task_id"]),
                    owner_user_id=int(row["owner_user_id"]),
                    chat_id=int(row["chat_id"]),
                    tool_name=str(row["tool_name"]),
                    arguments=json.loads(args_json),
                    args_hash=str(row["args_hash"]),
                    description=str(row["description"]),
                    status="used",
                    created_at=str(row["created_at"]),
                    expires_at=str(row["expires_at"]),
                ),
                "",
            )

    def deny_approval(self, *, token: str, user_id: int, chat_id: int) -> tuple[bool, str]:
        now = to_iso(utc_now())
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE token = ?", (token,)).fetchone()
            if not row:
                return False, "Confirmation token not found."
            if int(row["owner_user_id"]) != int(user_id) or int(row["chat_id"]) != int(chat_id):
                return False, "Confirmation token does not match owner or chat."
            if row["status"] != "pending":
                return False, f"Confirmation token is already {row['status']}."
            conn.execute(
                "UPDATE approvals SET status = 'denied', resolved_at = ? WHERE token = ?",
                (now, token),
            )
            conn.execute(
                """
                UPDATE tasks SET status = 'cancelled', error = 'Denied by owner', updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (now, now, row["task_id"]),
            )
            return True, "Proposed action denied and cancelled."

    # --- Token usage & budget reservations ---

    def reserve_budget(
        self,
        *,
        task_id: str | None,
        estimated_cost_usd: float,
        monthly_budget_usd: float,
        dt: datetime | None = None,
    ) -> tuple[str | None, UsageSummary]:
        """Atomically check monthly spend + inflight reservations + estimated call cost."""
        now_dt = dt or utc_now()
        mk = month_key_for(now_dt)
        with self.transaction() as conn:
            usage_row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0) AS in_tok,
                    COALESCE(SUM(output_tokens), 0) AS out_tok,
                    COALESCE(SUM(estimated_cost_usd), 0.0) AS cost,
                    COUNT(*) AS cnt
                FROM usage_records
                WHERE month_key = ?
                """,
                (mk,),
            ).fetchone()
            res_row = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS res FROM inflight_reservations WHERE month_key = ?",
                (mk,),
            ).fetchone()

            recorded_cost = float(usage_row["cost"])
            reserved_cost = float(res_row["res"])
            projected = recorded_cost + reserved_cost + max(0.0, estimated_cost_usd)

            summary = UsageSummary(
                month_key=mk,
                input_tokens=int(usage_row["in_tok"]),
                output_tokens=int(usage_row["out_tok"]),
                total_tokens=int(usage_row["in_tok"]) + int(usage_row["out_tok"]),
                estimated_cost_usd=recorded_cost,
                inflight_reserved_usd=reserved_cost,
                request_count=int(usage_row["cnt"]),
            )

            if projected > monthly_budget_usd:
                return None, summary

            res_id = f"res-{uuid.uuid4().hex[:10]}"
            conn.execute(
                """
                INSERT INTO inflight_reservations(reservation_id, task_id, estimated_cost_usd, month_key, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (res_id, task_id, max(0.0, estimated_cost_usd), mk, to_iso(now_dt)),
            )
            return res_id, summary

    def release_budget_reservation(self, reservation_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM inflight_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )

    def record_usage(
        self,
        *,
        task_id: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        input_cost_per_million_usd: float,
        output_cost_per_million_usd: float,
        dt: datetime | None = None,
    ) -> float:
        now_dt = dt or utc_now()
        cost = (
            (input_tokens / 1_000_000.0) * input_cost_per_million_usd
            + (output_tokens / 1_000_000.0) * output_cost_per_million_usd
        )
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO usage_records(
                    task_id, model, input_tokens, output_tokens,
                    estimated_cost_usd, month_key, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    model,
                    int(input_tokens),
                    int(output_tokens),
                    cost,
                    month_key_for(now_dt),
                    to_iso(now_dt),
                ),
            )
        return cost

    def get_monthly_usage(self, dt: datetime | None = None) -> UsageSummary:
        mk = month_key_for(dt)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0) AS in_tok,
                    COALESCE(SUM(output_tokens), 0) AS out_tok,
                    COALESCE(SUM(estimated_cost_usd), 0.0) AS cost,
                    COUNT(*) AS cnt
                FROM usage_records
                WHERE month_key = ?
                """,
                (mk,),
            ).fetchone()
            res_row = self._conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS res FROM inflight_reservations WHERE month_key = ?",
                (mk,),
            ).fetchone()
        return UsageSummary(
            month_key=mk,
            input_tokens=int(row["in_tok"]),
            output_tokens=int(row["out_tok"]),
            total_tokens=int(row["in_tok"]) + int(row["out_tok"]),
            estimated_cost_usd=float(row["cost"]),
            inflight_reserved_usd=float(res_row["res"]),
            request_count=int(row["cnt"]),
        )

    # --- Audit & Crash Recovery ---

    def log_audit(
        self,
        *,
        event_type: str,
        status: str,
        task_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = to_iso(utc_now())
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO audit_events(event_type, task_id, tool_name, status, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_type, task_id, tool_name, status, json.dumps(metadata or {}), now),
            )

    def reconcile_on_startup(
        self,
        *,
        stale_task_max_age_seconds: int,
        now: datetime | None = None,
    ) -> RecoveryReport:
        """Recover safely after a crash or restart.

        - Running tasks whose outcome is uncertain are marked `needs_review` (never auto-retried).
        - Stale pending tasks older than stale_task_max_age_seconds are expired (`cancelled`).
        - Expired pending approvals are marked `expired`.
        - Orphaned in-flight budget reservations are cleared.
        """
        check_time = now or utc_now()
        now_iso = to_iso(check_time)
        stale_cutoff = to_iso(check_time - timedelta(seconds=stale_task_max_age_seconds))

        with self.transaction() as conn:
            running_rows = conn.execute("SELECT id FROM tasks WHERE status = 'running'").fetchall()
            needs_review_ids = [str(r["id"]) for r in running_rows]
            if needs_review_ids:
                placeholders = ",".join("?" for _ in needs_review_ids)
                conn.execute(
                    f"""
                    UPDATE tasks
                    SET status = 'needs_review',
                        error = 'Interrupted by restart/crash; outcome uncertain and not auto-retried.',
                        updated_at = ?,
                        finished_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [now_iso, now_iso, *needs_review_ids],
                )

            stale_rows = conn.execute(
                "SELECT id FROM tasks WHERE status = 'pending' AND created_at < ?",
                (stale_cutoff,),
            ).fetchall()
            expired_pending_ids = [str(r["id"]) for r in stale_rows]
            if expired_pending_ids:
                placeholders = ",".join("?" for _ in expired_pending_ids)
                conn.execute(
                    f"""
                    UPDATE tasks
                    SET status = 'cancelled',
                        error = 'Expired stale queued task after outage/restart.',
                        updated_at = ?,
                        finished_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [now_iso, now_iso, *expired_pending_ids],
                )

            exp_appr_rows = conn.execute(
                "SELECT token, task_id FROM approvals WHERE status = 'pending' AND expires_at <= ?",
                (now_iso,),
            ).fetchall()
            expired_tokens = [str(r["token"]) for r in exp_appr_rows]
            if expired_tokens:
                placeholders = ",".join("?" for _ in expired_tokens)
                conn.execute(
                    f"""
                    UPDATE approvals
                    SET status = 'expired', resolved_at = ?
                    WHERE token IN ({placeholders})
                    """,
                    [now_iso, *expired_tokens],
                )
                task_ids = [str(r["task_id"]) for r in exp_appr_rows]
                t_placeholders = ",".join("?" for _ in task_ids)
                conn.execute(
                    f"""
                    UPDATE tasks
                    SET status = 'cancelled', error = 'Approval expired before confirmation.',
                        updated_at = ?, finished_at = ?
                    WHERE id IN ({t_placeholders}) AND status = 'waiting_confirmation'
                    """,
                    [now_iso, now_iso, *task_ids],
                )

            cleared_res = conn.execute("DELETE FROM inflight_reservations").rowcount

        return RecoveryReport(
            needs_review_task_ids=needs_review_ids,
            expired_pending_task_ids=expired_pending_ids,
            expired_approval_tokens=expired_tokens,
            cleared_reservations=int(cleared_res),
        )

    def backup_to(self, target_path: Path) -> Path:
        """Create a consistent SQLite snapshot using the built-in online backup API."""
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            dest = sqlite3.connect(str(target_path))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        return target_path
