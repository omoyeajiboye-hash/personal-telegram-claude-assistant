# Acceptance Test Results

- **Date:** 2026-11-19
- **Platform:** Windows (`win32`), Python `3.14.7`, `pytest 9.2.0`, `pytest-asyncio 1.5.0`
- **Command:** `.venv\Scripts\pytest.exe -v`
- **Result:** **16 passed, 0 failed** in `9.11s`

## Section 9 Acceptance Checklist Mapping

| # | Acceptance Criterion | Test Function | Status |
| --- | --- | --- | --- |
| 1 | Authorized owner receives a useful answer and context-aware follow-up response | `test_ac01_owner_qa_and_context_followup` | PASS |
| 2 | Unauthorized account (wrong user ID, wrong chat ID, group/supergroup) cannot invoke Claude, inspect history, or execute tools | `test_ac02_unauthorized_cannot_invoke_claude_or_tools` | PASS |
| 3 | `/new` starts a fresh model conversation without deleting retained records; retention and `/forget` clear data and report backup expiry | `test_ac03_new_retention_and_forget` | PASS |
| 4 | Bot reads a permitted text file, creates a new note (refusing silent overwrite), and reports accurate tool results | `test_ac04_read_file_create_note_and_accurate_results` | PASS |
| 5 | Attempts to access files outside approved directory (traversal, absolute/UNC/drive paths, reserved names, disallowed extensions, symlink/junction escapes) are rejected | `test_ac05_reject_traversal_and_symlink_escapes` | PASS |
| 6 | Protected actions run only after matching single-use owner confirmation; wrong-owner, reused, expired, and denied approvals fail | `test_ac06_protected_action_confirmation_single_use_and_expiry` | PASS |
| 7 | Replayed updates and restarts do not silently repeat completed or in-progress actions; Telegram reply failure never retries a side-effecting action | `test_ac07_replay_dedup_restart_recovery_and_no_retry_on_reply_fail` | PASS |
| 8 | Rate limits, invalid credentials, network loss, and unavailable files produce clear errors without exposing secrets in replies or logs | `test_ac08_errors_clear_without_leaking_secrets` | PASS |
| 9 | Long replies (>4,096 chars) arrive intact in correctly sized chunks; tool loops stop at `MAX_TOOL_STEPS`; spending stops at `MONTHLY_BUDGET_USD` | `test_ac09_long_replies_tool_loop_limit_and_budget_stop` | PASS |
| 10 | Safe-checkpoint `/cancel`, `/pause` & `/resume`, local stop sentinel control, backup creation, and full recovery verified | `test_ac10_cancellation_pause_local_stop_and_backup_recovery` | PASS |
| 11 | Document containing malicious prompt-injection instructions cannot expand bot permissions | `test_ac11_prompt_injection_in_document_cannot_expand_permissions` | PASS |
| 12 | Bot reports success only after observed execution; uncertain interrupted tasks are marked `needs_review` and surfaced in `/status` with clearly labeled cached service status | `test_ac12_accurate_status_and_needs_review_visibility` | PASS |
| + | Task list reading (`list_tasks`), predefined workflows (`daily_summary_note`, `/deny` on `archive_completed_tasks`), and `/usage` reporting | `test_workflows_list_tasks_usage_and_deny` | PASS |
| + | Allowlisted app listing & launching (`list_allowed_apps`, `open_app`), blocking non-allowlisted apps, `/pause`, and `ENABLE_SYSTEM_CONTROL=false` | `test_open_app_allowlist_and_controls` | PASS |
| + | Confirmation-gated Windows power actions (`shutdown` with 60s delay, `restart`, `lock`, `sleep`), token single-use, and immediate `cancel_shutdown` | `test_system_power_requires_confirmation_and_cancel_shutdown` | PASS |
| + | Prompt injection in untrusted document cannot trigger `open_app` or `system_power` without explicit owner confirmation | `test_prompt_injection_cannot_trigger_open_app_or_shutdown` | PASS |

## Note on Live Credentials & Host Sign-Off

The automated suite exercises the full application logic, SQLite persistence, security boundaries, error handling, and recovery deterministically without making billable external API calls or exposing real credentials. Before live operation on the host computer:
1. Confirm your numeric `OWNER_USER_ID` and `OWNER_CHAT_ID` in `.env`.
2. Store `TELEGRAM_BOT_TOKEN` and `ANTHROPIC_API_KEY` in Windows Credential Manager via `python -m telegram_assistant set-secret ...` (and rotate any credentials that were stored in plaintext in `Telegram Bot.docx`).
3. Run `python -m telegram_assistant check-config`, start the bot, and optionally register auto-start via `scripts/install_windows_task.ps1`.
