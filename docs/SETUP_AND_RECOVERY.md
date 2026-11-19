# Setup, Startup, Backup, and Recovery Guide

## 1. Cloud Data Flow Disclosure

This application runs locally on your Windows computer, **not** as an offline AI model:
- Messages you send to the bot travel through **Telegram's servers** and are fetched by this application via outbound HTTPS long polling (no public inbound port or webhook is used; any existing webhook is deleted automatically on startup).
- Selected prompts, bounded conversation history, and bounded excerpts from local files you ask to read are sent to **Anthropic's Messages API**.
- Only permitted files inside `APPROVED_FOLDER` can ever be read, and file contents are capped to `MAX_EXCERPT_CHARS` (12,000 characters by default).
- Operational logs record metadata only (no full prompts, no file contents, no secrets).

## 2. Prerequisites & Installation (Windows)

1. Install Python 3.11+ (tested on Python 3.14.7) under a **standard Windows user account** (do not run the bot as Administrator).
2. Create and activate the virtual environment, then install pinned dependencies:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\pip.exe install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` (`.env` is gitignored):
   ```powershell
   Copy-Item .env.example .env
   ```
4. Set your numeric `OWNER_USER_ID` and `OWNER_CHAT_ID` (for a private chat, these are typically the same positive integer; verify via `@userinfobot` or a quick `getUpdates` check on your bot).
5. Store secrets securely in **Windows Credential Manager** (recommended over plaintext `.env`):
   ```powershell
   .\.venv\Scripts\python.exe -m telegram_assistant set-secret TELEGRAM_BOT_TOKEN
   .\.venv\Scripts\python.exe -m telegram_assistant set-secret ANTHROPIC_API_KEY
   ```
   *(Alternatively, set `TELEGRAM_BOT_TOKEN` and `ANTHROPIC_API_KEY` in `.env` and restrict `.env` ACLs to your user account only.)*
6. Validate configuration without printing secrets:
   ```powershell
   .\.venv\Scripts\python.exe -m telegram_assistant check-config
   ```

## 3. Running & Local Stop Control

Start interactively:
```powershell
.\.venv\Scripts\python.exe -m telegram_assistant run
```

Stop cleanly from a second terminal (without killing abruptly):
```powershell
.\scripts\stop_bot.ps1
# or:
.\.venv\Scripts\python.exe -m telegram_assistant stop
```

## 4. Windows Automatic Startup & Restart (Task Scheduler)

Register a Task Scheduler job running under your standard user account (`RunLevel Limited`) with auto-start at logon and automatic restart on failure:
```powershell
.\scripts\install_windows_task.ps1
Start-ScheduledTask -TaskName "PersonalTelegramClaudeAssistant"
```

To remove the scheduled task:
```powershell
.\scripts\uninstall_windows_task.ps1
```

**Power & Sleep Settings:** The bot cannot respond while the computer is asleep, hibernating, or offline. Prolonged outages may cause Telegram to drop older queued updates. In Windows Power Settings, disable automatic sleep while plugged in if you want continuous availability.

## 5. Backup and Recovery

### Creating a Backup
```powershell
.\scripts\backup.ps1
# or:
.\.venv\Scripts\python.exe -m telegram_assistant backup
```
This uses SQLite's online backup API to snapshot `assistant.db` consistently, archives `APPROVED_FOLDER` into `approved_folder.zip`, restricts backup folder ACLs to the current Windows user (`icacls`), and prunes backups older than `BACKUP_RETENTION_DAYS` (default 30 days).

### Testing & Performing Recovery
1. Stop the running bot (`.\scripts\stop_bot.ps1`).
2. Restore from a timestamped bundle under `.\backups\backup-YYYYMMDDTHHMMSSZ`:
   ```powershell
   .\.venv\Scripts\python.exe -m telegram_assistant restore .\backups\backup-20261119T200000Z
   ```
3. Verify status and restart:
   ```powershell
   .\.venv\Scripts\python.exe -m telegram_assistant status
   .\.venv\Scripts\python.exe -m telegram_assistant run
   ```
4. If the bot restarted after an unexpected crash, run `/status` in Telegram to inspect any tasks marked `needs_review` (in-progress actions are never silently auto-retried).
