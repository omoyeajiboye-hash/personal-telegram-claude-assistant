"""Configuration loading from environment variables, .env, and OS-protected keyring."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

from .security import SecretRedactor

logger = logging.getLogger(__name__)

DEFAULT_KEYRING_SERVICE = "personal-telegram-claude-assistant"


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _get_keyring_secret(service: str, key: str) -> str | None:
    try:
        import keyring
        from keyring.errors import KeyringError

        val = keyring.get_password(service, key)
        return val.strip() if val else None
    except Exception:
        return None


def set_os_secret(key: str, value: str, service: str = DEFAULT_KEYRING_SERVICE) -> None:
    """Store a secret in the host OS credential store (Windows Credential Manager, Keychain, SecretService)."""
    import keyring

    keyring.set_password(service, key, value.strip())


def delete_os_secret(key: str, service: str = DEFAULT_KEYRING_SERVICE) -> None:
    """Remove a secret from the host OS credential store."""
    import keyring

    try:
        keyring.delete_password(service, key)
    except Exception:
        pass


def _parse_int(env: Mapping[str, str], name: str, default: int | None = None, *, min_value: int | None = None) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        if default is not None:
            return default
        raise ConfigError(f"Missing required integer configuration: {name}")
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigError(f"Configuration {name} must be an integer.") from exc
    if min_value is not None and val < min_value:
        raise ConfigError(f"Configuration {name} must be >= {min_value}.")
    return val


def _parse_float(env: Mapping[str, str], name: str, default: float, *, min_value: float = 0.0) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError as exc:
        raise ConfigError(f"Configuration {name} must be a number.") from exc
    if val < min_value:
        raise ConfigError(f"Configuration {name} must be >= {min_value}.")
    return val


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration for the Telegram assistant."""

    telegram_bot_token: str = field(repr=False)
    anthropic_api_key: str = field(repr=False)
    owner_user_id: int
    owner_chat_id: int

    claude_model: str = "claude-sonnet-4-6"
    input_cost_per_million_usd: float = 3.00
    output_cost_per_million_usd: float = 15.00
    monthly_budget_usd: float = 20.00

    approved_folder: Path = Path("./assistant_folder")
    db_path: Path = Path("./data/assistant.db")
    log_dir: Path = Path("./logs")
    backup_dir: Path = Path("./backups")
    stop_file: Path = Path("./STOP_REQUESTED")

    max_history_messages: int = 20
    max_history_chars: int = 24_000
    max_output_tokens: int = 1024
    max_tool_steps: int = 5
    max_file_bytes: int = 262_144
    max_excerpt_chars: int = 12_000
    job_timeout_seconds: int = 90
    api_timeout_seconds: int = 45
    max_api_retries: int = 3
    max_concurrent_jobs: int = 1
    long_job_ack_seconds: float = 3.0
    telegram_connect_timeout: float = 20.0
    telegram_read_timeout: float = 30.0
    telegram_write_timeout: float = 20.0
    telegram_pool_timeout: float = 20.0
    telegram_force_ipv4: bool = True
    telegram_proxy: str | None = None

    enable_system_control: bool = True
    confirm_open_app: bool = False
    shutdown_delay_seconds: int = 60

    approval_ttl_seconds: int = 300
    retention_days: int = 30
    backup_retention_days: int = 30
    stale_task_max_age_seconds: int = 600

    log_level: str = "INFO"
    use_os_keyring: bool = True
    keyring_service_name: str = DEFAULT_KEYRING_SERVICE

    def build_redactor(self) -> SecretRedactor:
        return SecretRedactor([self.telegram_bot_token, self.anthropic_api_key])

    def ensure_directories(self) -> None:
        self.approved_folder.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)


def load_config(
    *,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    require_secrets: bool = True,
) -> Config:
    """Load and validate configuration from .env, environment variables, and OS keyring."""
    if environ is None:
        if env_file is not None:
            load_dotenv(dotenv_path=env_file, override=False)
        else:
            load_dotenv(override=False)
        env: Mapping[str, str] = os.environ
    else:
        env = environ

    use_os_keyring = _parse_bool(env, "USE_OS_KEYRING", True)
    keyring_service = env.get("KEYRING_SERVICE_NAME", DEFAULT_KEYRING_SERVICE).strip() or DEFAULT_KEYRING_SERVICE

    def resolve_secret(name: str) -> str:
        val = env.get(name, "").strip()
        if not val and use_os_keyring:
            val = _get_keyring_secret(keyring_service, name) or ""
        if require_secrets and not val:
            raise ConfigError(
                f"Missing required secret {name}. Set it in .env, environment variables, "
                f"or OS credential store via `python -m telegram_assistant set-secret {name}`."
            )
        return val

    telegram_bot_token = resolve_secret("TELEGRAM_BOT_TOKEN")
    anthropic_api_key = resolve_secret("ANTHROPIC_API_KEY")

    owner_user_id = _parse_int(env, "OWNER_USER_ID", min_value=1)
    owner_chat_id = _parse_int(env, "OWNER_CHAT_ID", min_value=1)

    claude_model = env.get("CLAUDE_MODEL", "claude-sonnet-4-6").strip() or "claude-sonnet-4-6"

    return Config(
        telegram_bot_token=telegram_bot_token,
        anthropic_api_key=anthropic_api_key,
        owner_user_id=owner_user_id,
        owner_chat_id=owner_chat_id,
        claude_model=claude_model,
        input_cost_per_million_usd=_parse_float(env, "INPUT_COST_PER_MILLION_USD", 3.00),
        output_cost_per_million_usd=_parse_float(env, "OUTPUT_COST_PER_MILLION_USD", 15.00),
        monthly_budget_usd=_parse_float(env, "MONTHLY_BUDGET_USD", 20.00),
        approved_folder=Path(env.get("APPROVED_FOLDER", "./assistant_folder")).resolve(),
        db_path=Path(env.get("DB_PATH", "./data/assistant.db")).resolve(),
        log_dir=Path(env.get("LOG_DIR", "./logs")).resolve(),
        backup_dir=Path(env.get("BACKUP_DIR", "./backups")).resolve(),
        stop_file=Path(env.get("STOP_FILE", "./STOP_REQUESTED")).resolve(),
        max_history_messages=_parse_int(env, "MAX_HISTORY_MESSAGES", 20, min_value=1),
        max_history_chars=_parse_int(env, "MAX_HISTORY_CHARS", 24_000, min_value=500),
        max_output_tokens=_parse_int(env, "MAX_OUTPUT_TOKENS", 1024, min_value=64),
        max_tool_steps=_parse_int(env, "MAX_TOOL_STEPS", 5, min_value=1),
        max_file_bytes=_parse_int(env, "MAX_FILE_BYTES", 262_144, min_value=1024),
        max_excerpt_chars=_parse_int(env, "MAX_EXCERPT_CHARS", 12_000, min_value=200),
        job_timeout_seconds=_parse_int(env, "JOB_TIMEOUT_SECONDS", 90, min_value=5),
        api_timeout_seconds=_parse_int(env, "API_TIMEOUT_SECONDS", 45, min_value=5),
        max_api_retries=_parse_int(env, "MAX_API_RETRIES", 3, min_value=0),
        max_concurrent_jobs=_parse_int(env, "MAX_CONCURRENT_JOBS", 1, min_value=1),
        long_job_ack_seconds=_parse_float(env, "LONG_JOB_ACK_SECONDS", 3.0, min_value=0.0),
        telegram_connect_timeout=_parse_float(env, "TELEGRAM_CONNECT_TIMEOUT", 20.0, min_value=5.0),
        telegram_read_timeout=_parse_float(env, "TELEGRAM_READ_TIMEOUT", 30.0, min_value=5.0),
        telegram_write_timeout=_parse_float(env, "TELEGRAM_WRITE_TIMEOUT", 20.0, min_value=5.0),
        telegram_pool_timeout=_parse_float(env, "TELEGRAM_POOL_TIMEOUT", 20.0, min_value=1.0),
        telegram_force_ipv4=_parse_bool(env, "TELEGRAM_FORCE_IPV4", True),
        telegram_proxy=(env.get("TELEGRAM_PROXY", "").strip() or None),
        enable_system_control=_parse_bool(env, "ENABLE_SYSTEM_CONTROL", True),
        confirm_open_app=_parse_bool(env, "CONFIRM_OPEN_APP", False),
        shutdown_delay_seconds=_parse_int(env, "SHUTDOWN_DELAY_SECONDS", 60, min_value=0),
        approval_ttl_seconds=_parse_int(env, "APPROVAL_TTL_SECONDS", 300, min_value=10),
        retention_days=_parse_int(env, "RETENTION_DAYS", 30, min_value=1),
        backup_retention_days=_parse_int(env, "BACKUP_RETENTION_DAYS", 30, min_value=1),
        stale_task_max_age_seconds=_parse_int(env, "STALE_TASK_MAX_AGE_SECONDS", 600, min_value=10),
        log_level=(env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"),
        use_os_keyring=use_os_keyring,
        keyring_service_name=keyring_service,
    )
