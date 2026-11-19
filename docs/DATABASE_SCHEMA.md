# Local SQLite Database Schema

The local application stores operational state, bounded conversation history, update deduplication records, tasks, single-use approvals, usage metrics, and metadata-only audit events in a single SQLite database (`DB_PATH`, default `./data/assistant.db`) running in `WAL` mode with `PRAGMA foreign_keys = ON`.

## Tables

### `app_state`
Key-value operational flags and cached service metadata (`actions_paused`, `active_conversation_<chat_id>`, `cached_status_telegram`, `cached_checked_at_telegram`, `cached_status_anthropic`, `cached_checked_at_anthropic`, `last_forget_at`).

### `conversations`
Tracks logical conversations per `chat_id`. `/new` sets `archived_at` on the prior conversation and inserts a new active row without deleting prior retained records.

### `messages`
Stores `user` and `assistant` messages linked by `conversation_id` (`ON DELETE CASCADE`). Loaded in reverse chronological order bounded by both `MAX_HISTORY_MESSAGES` and `MAX_HISTORY_CHARS`, and pruned automatically by `RETENTION_DAYS` or cleared immediately by `/forget` (followed by `VACUUM`).

### `processed_updates`
Primary key on Telegram `update_id`. Atomically claimed via `INSERT OR IGNORE` before any command or model call executes, preventing duplicate execution on replayed updates.

### `tasks`
Tracks request lifecycle across states:
- `pending`
- `running`
- `waiting_confirmation`
- `succeeded`
- `failed`
- `cancelled`
- `needs_review`

On startup after an unexpected crash or restart:
- Any `running` task is transitioned to `needs_review` (never blindly auto-retried).
- Any `pending` task older than `STALE_TASK_MAX_AGE_SECONDS` is expired (`cancelled`).

### `approvals`
Stores single-use confirmation records bound to:
- `token` (cryptographically random primary key)
- `task_id`
- `owner_user_id` and `chat_id`
- `tool_name`, canonical `arguments_json`, and SHA-256 `args_hash`
- `expires_at` (UTC ISO-8601 timestamp)
- `status` (`pending` | `used` | `denied` | `expired`)

Consumed atomically inside a `BEGIN IMMEDIATE` transaction (`WHERE token = ? AND status = 'pending'`) so reused, expired, denied, or mismatched approvals always fail.

### `usage_records` & `inflight_reservations`
- `usage_records`: Monthly token totals (`input_tokens`, `output_tokens`, `estimated_cost_usd`, `month_key`) per API call.
- `inflight_reservations`: Temporary estimated cost reservations held while an Anthropic API call is in flight so concurrent or back-to-back calls cannot exceed `MONTHLY_BUDGET_USD`. Cleared on startup recovery and released in `finally`.

### `audit_events`
Operational metadata log (`event_type`, `task_id`, `tool_name`, `status`, `metadata_json`, `created_at`) — never stores raw prompts, file contents, or secrets.

## DDL Reference

The authoritative DDL is defined in `SCHEMA_SQL` inside `src/telegram_assistant/db.py`.
