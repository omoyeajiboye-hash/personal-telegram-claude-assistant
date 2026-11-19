# AGENTS.md

Instructions and engineering invariants for coding agents working in this repository.

## Project Overview

This repository implements a **private, locally hosted Personal Telegram Assistant powered by Claude** (Python 3.11+, Windows host target).

- **Transport:** Telegram long polling via `python-telegram-bot` (no web server, no public inbound endpoint; any existing webhook is removed on startup).
- **AI Provider:** Anthropic Messages API via the official `anthropic` Python SDK (configurable model ID via `CLAUDE_MODEL`).
- **Storage:** Local SQLite in WAL mode (`src/telegram_assistant/db.py`).
- **Action Layer:** Small validated Python functions strictly confined to `APPROVED_FOLDER` (`src/telegram_assistant/tools.py`).

## Repository Layout

- `src/telegram_assistant/`
  - `__main__.py`: CLI (`run`, `stop`, `check-config`, `set-secret`, `backup`, `restore`, `status`)
  - `config.py`: Env/`.env`/OS-keyring configuration loader and validation
  - `security.py`: Owner/chat checks, path sandboxing, secret redaction, untrusted-data framing, Telegram chunking
  - `db.py`: SQLite schema, conversations, update dedup, tasks, single-use approvals, budget tracking, crash reconciliation
  - `tools.py`: Tool allowlist, permission tiers, validated local actions
  - `claude_client.py`: Messages API wrapper, budget guard, bounded retries, bounded tool-use loop
  - `bot.py`: Telegram controller, command handlers, per-chat serialization, safe reply delivery
  - `ops.py`: Metadata-only redacted logging, backup/restore with Windows ACLs, local stop sentinel
- `tests/test_acceptance.py`: Automated acceptance test suite covering all 12 Section 9 criteria
- `scripts/`: Windows Task Scheduler, local stop, and backup PowerShell scripts
- `docs/`: Command guide, tool/permission catalogue, DB schema, setup/recovery, usage/maintenance, acceptance test results
- `assistant_folder/`: Default approved folder for permitted text files and notes

## Development & Test Commands (Windows PowerShell)

```powershell
# Run automated acceptance test suite
.\.venv\Scripts\pytest.exe -v

# Validate configuration without printing secrets
.\.venv\Scripts\python.exe -m telegram_assistant check-config

# Run CLI commands
.\.venv\Scripts\python.exe -m telegram_assistant --help
```

## Non-Negotiable Security & Privacy Rules

1. **Never expose or commit secrets.**
   - Never hardcode, log, print, or commit `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, GitHub tokens, `.env`, or `Telegram Bot.docx`.
   - Keep `RedactingLogFilter` active on all log handlers and pass all outbound Telegram messages through `SecretRedactor.redact()`.
   - Default logging must record operational metadata only — never full user prompts, assistant replies, or local file contents.
2. **Strict single-owner private-chat access.**
   - Always enforce `is_authorized_owner()` (matching numeric `OWNER_USER_ID`, matching numeric `OWNER_CHAT_ID`, and `chat_type == "private"`) before invoking Claude, reading history, or validating/executing tools.
3. **Code-enforced filesystem sandbox.**
   - Every local file path must pass `resolve_safe_path()` against `APPROVED_FOLDER`.
   - Reject `..` traversal, absolute/UNC/drive paths, NUL/control chars, Windows reserved device names, disallowed extensions, symlinks, and Windows junctions.
   - Treat all file contents returned to Claude as **untrusted data** (`wrap_untrusted_file_content()`). Document text must never expand tool permissions.
4. **Action permission policy.**
   - Immediate read-only: `list_files`, `read_text_file`, `list_tasks`, `list_allowed_apps`.
   - Create-only: `create_note` must **never** silently overwrite an existing file (keep atomic `O_CREAT | O_EXCL`).
   - Controlled launch: `open_app` must launch ONLY verified executables from the fixed allowlist with `shell=False` and zero arbitrary arguments, respect `ENABLE_SYSTEM_CONTROL` / `/pause` / `CONFIRM_OPEN_APP`, and automatically escalate to confirmation if untrusted file content was read in the same turn.
   - Confirmation-required: `overwrite_note`, `run_local_workflow`, and `system_power` (`lock`, `sleep`, `restart`, `shutdown`; `cancel_shutdown` is a safe immediate abort) must require a single-use approval bound to owner ID, chat ID, exact argument SHA-256 hash, and expiry, with full permission/path revalidation immediately before execution.
   - Unsupported: deleting files, arbitrary shell commands, sending messages to others, and installing software remain unconditionally blocked. Do not add these without an explicit scope decision and permission review.

## Reliability & Cost Invariants

- **Idempotency & crash recovery:** Record `update_id` in `processed_updates` before executing. On startup, transition interrupted `running` tasks to `needs_review` (never auto-retry uncertain side-effecting work) and expire stale `pending` tasks.
- **No action retry on reply failure:** Retry transient Telegram send failures only; never re-execute a side-effecting tool solely because delivering the Telegram reply failed.
- **Fail-closed budget guard:** Always reserve estimated call cost in `inflight_reservations` before calling Anthropic's API, release in `finally`, and record actual token usage in `usage_records`.
- **Bounds:** Respect configured limits for history size/chars, output tokens, tool steps, file size, excerpt length, job timeout, retries, and concurrent jobs.
- **Cached status labeling:** `/status` must explicitly label cached Telegram/Anthropic status as cached rather than live probes.

## Checklist When Adding Features

When adding or modifying a tool, command, or schema table:
1. Enforce permission tier and input validation in `tools.py` / `security.py`.
2. Update the relevant docs in `docs/` (`COMMANDS.md`, `TOOLS_AND_PERMISSIONS.md`, `DATABASE_SCHEMA.md`).
3. Add or update deterministic tests in `tests/test_acceptance.py` and run `.\.venv\Scripts\pytest.exe -v`.
4. Pin any dependency changes in `requirements.txt` and `pyproject.toml`.
