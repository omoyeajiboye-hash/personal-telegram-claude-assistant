# Personal Telegram Assistant powered by Claude

A private, locally hosted Python Telegram assistant using **Telegram long polling** (`python-telegram-bot`), **Anthropic's Messages API** (`anthropic`), **SQLite** storage, and a strictly validated local action layer confined to a designated approved folder.

## Key Capabilities & Safety Controls

- **Single-owner private access:** Enforces numeric `OWNER_USER_ID`, numeric `OWNER_CHAT_ID`, and `chat.type == "private"` before any Claude call, history lookup, or tool validation.
- **Long polling only:** Removes any existing webhook on startup; requires no public inbound endpoint or web server.
- **Code-enforced local permissions:**
  - Immediate read-only: `list_files`, `read_text_file` (bounded excerpts, untrusted-data framing), `list_tasks`
  - Create-only: `create_note` (never silently overwrites existing files)
  - Single-use confirmed side effects: `overwrite_note`, `run_local_workflow` (`daily_summary_note`, `archive_completed_tasks`)
  - Blocked in code: deleting files, arbitrary shell commands, external messaging, installing software, path traversal, and symlink/junction escapes
- **Reliability & cost controls:** Per-chat request serialization, update deduplication, safe-checkpoint `/cancel`, `/pause` & local stop control, crash recovery (`needs_review` without blindly retrying uncertain actions), stale task expiry, secret redaction in logs and replies, monthly budget enforcement with in-flight reservations, and SQLite + folder backup/recovery.

## Quick Start

1. Copy `.env.example` to `.env` and set your numeric `OWNER_USER_ID` and `OWNER_CHAT_ID`.
2. Store secrets in Windows Credential Manager:
   ```powershell
   .\.venv\Scripts\python.exe -m telegram_assistant set-secret TELEGRAM_BOT_TOKEN
   .\.venv\Scripts\python.exe -m telegram_assistant set-secret ANTHROPIC_API_KEY
   ```
3. Validate configuration:
   ```powershell
   .\.venv\Scripts\python.exe -m telegram_assistant check-config
   ```
4. Run the bot:
   ```powershell
   .\.venv\Scripts\python.exe -m telegram_assistant run
   ```

## Handover Documentation

- [Telegram Command Guide](docs/COMMANDS.md)
- [Supported Tool Catalogue & Permission Rules](docs/TOOLS_AND_PERMISSIONS.md)
- [Local Database Schema](docs/DATABASE_SCHEMA.md)
- [Setup, Startup, Backup & Recovery](docs/SETUP_AND_RECOVERY.md)
- [Usage, Cost Planning & Maintenance](docs/USAGE_AND_MAINTENANCE.md)
- [Acceptance Test Results](docs/ACCEPTANCE_TEST_RESULTS.md)
