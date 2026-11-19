# Usage, Cost Planning, Privacy, and Maintenance Guide

## 1. Cost Planning & Budget Controls

- Ongoing operating costs consist of Anthropic API token usage, electricity/internet, and periodic maintenance. A consumer Claude subscription does **not** include API usage.
- Estimated API spend is calculated as:
  `input_tokens / 1,000,000 * INPUT_COST_PER_MILLION_USD + output_tokens / 1,000,000 * OUTPUT_COST_PER_MILLION_USD`
- Always confirm current per-million-token rates in Anthropic's pricing documentation whenever you change `CLAUDE_MODEL` in `.env`.
- **Application-level fail-closed budget guard:** Before every Messages API call, the app sums recorded monthly spend, active in-flight cost reservations, and the estimated cost of the upcoming request. If the total would exceed `MONTHLY_BUDGET_USD`, the call is refused immediately.
- **Provider-level spending limit:** Also configure a monthly spend cap directly inside the Anthropic Console as a second independent safeguard.
- Check live monthly usage anytime via `/usage` in Telegram or `python -m telegram_assistant status` locally.

## 2. Retention & `/forget` Policy

- **Active conversation history:** Retained locally in SQLite for `RETENTION_DAYS` (default: 30 days) and pruned automatically on startup.
- **`/new`:** Starts a fresh model conversation without deleting retained records.
- **`/forget`:** Immediately deletes all retained conversations and messages for your chat from `assistant.db` and executes SQLite `VACUUM` so deleted text does not remain in free database pages. A metadata-only audit timestamp (`last_forget_at`) is recorded.
- **Backups:** Previously created backup bundles in `BACKUP_DIR` expire and are automatically pruned after `BACKUP_RETENTION_DAYS` (default: 30 days) when the backup job runs. If immediate erasure is required, also delete prior bundles inside `.\backups\`.

## 3. Secret Handling & Rotation Procedure

- **Never** commit `.env`, `Telegram Bot.docx`, database files, or backup archives to source control (all are excluded in `.gitignore`).
- **Automated redaction:** `SecretRedactor` strips known secret literals and credential patterns (`sk-ant-...`, Telegram bot tokens, GitHub tokens) from all operational log handlers and outbound Telegram replies.
- **Rotating the Telegram Bot Token:**
  1. Open `@BotFather` in Telegram -> `/mybots` -> select your bot -> **API Token** -> **Revoke current token**.
  2. Update the stored secret: `.\.venv\Scripts\python.exe -m telegram_assistant set-secret TELEGRAM_BOT_TOKEN` (or update `.env`).
  3. Restart the bot (`.\scripts\stop_bot.ps1`, then restart the scheduled task).
- **Rotating the Anthropic API Key:**
  1. Generate a new key in the Anthropic Console.
  2. Update the stored secret: `.\.venv\Scripts\python.exe -m telegram_assistant set-secret ANTHROPIC_API_KEY` (or update `.env`).
  3. Restart the bot and verify `/status` and a test prompt.
  4. Revoke the old key in the Anthropic Console.
- **Important:** If any API key or token was ever placed in a plaintext document (such as a Word file) or shared channel, rotate it immediately after migrating it to Windows Credential Manager.

## 4. Maintenance Checklist

- **Weekly:** Run `.\scripts\backup.ps1` (or schedule it daily in Task Scheduler) and inspect `/usage`.
- **Monthly:** Review `CLAUDE_MODEL` availability, pinned dependency updates (`requirements.txt`), and run `.venv\Scripts\pytest.exe -v` after any upgrade.
- **Whenever adding a new tool or integration:** Re-review the permission model in `docs/TOOLS_AND_PERMISSIONS.md` and add acceptance tests before enabling.
