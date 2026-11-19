# Telegram Command Guide

All commands are restricted to the single configured numeric owner (`OWNER_USER_ID`) in the configured private chat (`OWNER_CHAT_ID`, `chat.type == "private"`). Unauthorized accounts and group chats are ignored before any model call, history lookup, or local tool check.

## Available Commands

| Command | Purpose | Operating Behaviour |
| --- | --- | --- |
| `/start`, `/help` | Show features, commands, and example requests | Executes immediately. |
| `/new` | Begin a fresh Claude conversation | Archives the current conversation ID and starts a new one. **Does not delete retained records.** |
| `/status` | Inspect local application health and task state | Shows running/paused state, configured model ID, current/latest task, any `needs_review` tasks after a crash/restart, retention policy, and **explicitly labeled cached Telegram/Anthropic status** (not a live network probe). |
| `/usage` | Inspect monthly token usage and estimated cost | Displays recorded input/output/total tokens, estimated USD spend based on configured per-million-token rates, in-flight reserved budget, and remaining monthly budget. |
| `/cancel` | Cancel queued work and request safe-checkpoint stop | Immediately cancels `pending` / `waiting_confirmation` tasks in SQLite and signals any active task to stop at its next safe checkpoint (before the next API call or tool step). **Cannot undo actions that have already completed.** |
| `/pause` | Disable new local actions while preserving chat | Sets `actions_paused=true`. Q&A and drafting continue working, while all local tool calls and `/confirm` executions are blocked. |
| `/resume` | Re-enable local actions | Sets `actions_paused=false`. |
| `/forget` | Clear retained conversation data | Deletes all retained conversations and messages for the chat from SQLite, runs `VACUUM` to reclaim freed pages, records a metadata-only audit entry, and reports how existing local backups expire (`BACKUP_RETENTION_DAYS`, default 30 days). |
| `/confirm <token>` | Approve a proposed protected local action | Validates owner ID, chat ID, expiry, argument integrity hash, single-use status, and paused state, then revalidates path/permission rules immediately before executing once. |
| `/deny <token>` | Reject a proposed protected local action | Marks the approval denied and cancels the waiting task. |

## Example Requests

- `Explain how SQLite WAL mode works`
- `Draft a polite follow-up email declining a meeting`
- `Read notes.txt in the assistant folder and summarize it`
- `Create a note called meeting-summary`
- `Show my task list`
- `Overwrite notes.txt with ...` *(prompts for `/confirm <token>`)*
- `Run the daily_summary_note workflow` *(prompts for `/confirm <token>`)*
- `What apps can you open?` / `Open Chrome` / `Open Notepad`
- `Lock my laptop` / `Put my PC to sleep` / `Shut down my laptop` *(prompts for `/confirm <token>`)*
- `Cancel shutdown` *(aborts a pending scheduled shutdown/restart)*

## Request Serialization & Long Replies

- Requests within the private chat are serialized one at a time. If a second message arrives while a task is active, it is queued and acknowledged.
- Long-running requests send a progress acknowledgement after `LONG_JOB_ACK_SECONDS` (default 3 seconds).
- Replies longer than Telegram's 4,096-character limit are automatically split at paragraph/newline/word boundaries and delivered in order.
