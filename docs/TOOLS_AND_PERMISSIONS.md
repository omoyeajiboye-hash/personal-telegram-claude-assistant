# Supported Tool Catalogue and Permission Rules

Claude has **no direct access to the host computer**. Claude only proposes structured client tool calls; the local Python application (`src/telegram_assistant/tools.py` and `src/telegram_assistant/security.py`) validates every argument, resolves paths, enforces permission rules in code, obtains confirmation where required, executes the action, and returns the observed result.

## Permission Tiers

| Action / Tool | Permission Policy | Code-Enforced Rules |
| --- | --- | --- |
| Question answering / drafting | **Execute immediately** | Bounded conversation history, output tokens, and monthly budget check. |
| `list_files` | **Immediate read** | Lists permitted text files inside `APPROVED_FOLDER`. Rejects traversal and skips symlinks/junctions. |
| `read_text_file` | **Immediate read** | Validates resolved real path stays inside `APPROVED_FOLDER`, rejects `..`/absolute/UNC/drive paths and symlinks/junctions, enforces allowed text extensions (`.txt`, `.md`, `.markdown`, `.csv`, `.json`, `.yaml`, `.yml`, `.log`), enforces `MAX_FILE_BYTES` (256 KB), truncates to `MAX_EXCERPT_CHARS` (12,000 chars), and wraps output in untrusted-data markers. |
| `list_tasks` | **Immediate read** | Reads `tasks.md` (or `tasks.txt`) inside `APPROVED_FOLDER`, parses `- [ ]` and `- [x]` items, and wraps output in untrusted-data markers. |
| `list_allowed_apps` | **Immediate read** | Lists which allowlisted desktop apps are currently installed on the host Windows PC. Requires `ENABLE_SYSTEM_CONTROL=true`. |
| `create_note` | **Create-only (never silently overwrite)** | Creates a new `.md` or `.txt` file inside `APPROVED_FOLDER` using atomic `O_CREAT \| O_EXCL`. Fails cleanly if the target file already exists. |
| `open_app` | **Controlled launch (allowlist only)** | Launches only verified executables from the fixed Windows allowlist (`chrome`, `edge`, `notepad`, `calculator`, `explorer`, `paint`, `word`, `excel`, `powerpoint`, `vscode`, `spotify`) with `shell=False` and zero user-supplied arguments. Respects `ENABLE_SYSTEM_CONTROL`, `/pause`, and `CONFIRM_OPEN_APP`; automatically escalated to require `/confirm <token>` if untrusted file content was read during the same turn. |
| `overwrite_note` | **Confirmation required** | Displays exact target path, size, and content preview. Requires single-use `/confirm <token>` bound to owner user ID, chat ID, exact argument SHA-256 hash, and expiry (`APPROVAL_TTL_SECONDS`, default 300s). Revalidates permissions immediately before execution and replaces atomically. |
| `run_local_workflow` | **Confirmation required** | Strictly limited to two predefined Python workflows (zero shell commands): `daily_summary_note` (generates `daily-digest-YYYY-MM-DD.md`) and `archive_completed_tasks` (moves `- [x]` tasks from `tasks.md` to `completed-tasks-archive.md`). Requires single-use `/confirm <token>`. |
| `system_power` | **Confirmation required** (`cancel_shutdown` immediate) | Strictly limited to `lock`, `sleep`, `restart`, `shutdown` (scheduled with `SHUTDOWN_DELAY_SECONDS`, default 60s), and `cancel_shutdown` (`shutdown /a`). `lock`, `sleep`, `restart`, and `shutdown` ALWAYS require single-use `/confirm <token>`. Uses fixed `shell=False` argument vectors only. Respects `ENABLE_SYSTEM_CONTROL` and `/pause`. |
| Delete files, send messages to others, install software, arbitrary shell commands | **Unsupported (blocked in code)** | Not declared in the tool allowlist; any attempt (including via prompt injection) is unconditionally rejected and recorded in `audit_events`. |

## Prompt Injection Defense

1. File contents returned by `read_text_file` and `list_tasks` are enclosed in explicit `<<<BEGIN_FILE_DATA>>> ... <<<END_FILE_DATA>>>` markers instructing Claude to treat them strictly as passive data.
2. Security boundaries do **not** rely on model compliance alone: path sandboxing, file-extension allowlists, symlink/junction blocking, tool allowlists, `/pause`, and single-use owner confirmations are enforced deterministically in Python code.
