"""CLI entry point for the Personal Telegram Assistant powered by Claude."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from .bot import run_telegram_bot
from .config import ConfigError, DEFAULT_KEYRING_SERVICE, load_config, set_os_secret
from .db import Database
from .ops import configure_logging, create_backup, prune_old_backups, request_local_stop, restore_backup


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(env_file=Path(args.env_file) if args.env_file else None)
    redactor = config.build_redactor()
    configure_logging(config, redactor)
    try:
        asyncio.run(run_telegram_bot(config))
    except KeyboardInterrupt:
        print("Shutdown requested via KeyboardInterrupt.")
    except RuntimeError as exc:
        print(f"Error: {redactor.redact(str(exc))}", file=sys.stderr)
        return 1
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    config = load_config(
        env_file=Path(args.env_file) if args.env_file else None,
        require_secrets=False,
    )
    stop_path = request_local_stop(config.stop_file)
    print(f"Local stop requested via sentinel file: {stop_path}")
    return 0


def _cmd_check_config(args: argparse.Namespace) -> int:
    config = load_config(env_file=Path(args.env_file) if args.env_file else None)
    config.ensure_directories()
    print("Configuration valid.")
    print(f"  Model:           {config.claude_model}")
    print(f"  Owner user ID:   {config.owner_user_id}")
    print(f"  Owner chat ID:   {config.owner_chat_id}")
    print(f"  Approved folder: {config.approved_folder}")
    print(f"  Database path:   {config.db_path}")
    print(f"  Monthly budget:  ${config.monthly_budget_usd:.2f}")
    print(f"  Retention days:  {config.retention_days}")
    print("  Secrets:         [PRESENT - REDACTED]")
    return 0


def _cmd_set_secret(args: argparse.Namespace) -> int:
    name = args.name.strip().upper()
    if name not in {"TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY"}:
        print("Supported secret names: TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY", file=sys.stderr)
        return 2
    value = getpass.getpass(f"Enter {name} (input hidden): ").strip()
    if not value:
        print("Aborted: empty value.", file=sys.stderr)
        return 2
    set_os_secret(name, value, service=args.service)
    print(f"Stored {name} securely in OS credential store (service={args.service}).")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    config = load_config(
        env_file=Path(args.env_file) if args.env_file else None,
        require_secrets=False,
    )
    config.ensure_directories()
    bundle = create_backup(config)
    pruned = prune_old_backups(config.backup_dir, config.backup_retention_days)
    print(f"Backup created: {bundle}")
    if pruned:
        print(f"Pruned {pruned} expired backup bundle(s) older than {config.backup_retention_days} days.")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    config = load_config(
        env_file=Path(args.env_file) if args.env_file else None,
        require_secrets=False,
    )
    restore_backup(
        Path(args.bundle),
        target_db_path=config.db_path,
        target_approved_folder=config.approved_folder,
    )
    print(f"Restored database ({config.db_path}) and approved folder ({config.approved_folder}) from {args.bundle}.")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(
        env_file=Path(args.env_file) if args.env_file else None,
        require_secrets=False,
    )
    if not config.db_path.exists():
        print(f"Database not initialized yet at {config.db_path}.")
        return 0
    db = Database(config.db_path)
    try:
        usage = db.get_monthly_usage()
        paused = db.is_actions_paused()
        review = db.get_tasks_needing_review(config.owner_chat_id)
        print(f"Actions paused:    {paused}")
        print(f"Configured model:  {config.claude_model}")
        print(f"Month ({usage.month_key}): {usage.total_tokens:,} tokens, est. ${usage.estimated_cost_usd:.4f} / ${config.monthly_budget_usd:.2f}")
        print(f"Needs review:      {len(review)}")
    finally:
        db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m telegram_assistant",
        description="Personal Telegram assistant powered by Claude.",
    )
    parser.add_argument("--env-file", default=None, help="Optional path to .env file.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Start the Telegram long-polling bot.")
    p_run.set_defaults(func=_cmd_run)

    p_stop = sub.add_parser("stop", help="Gracefully stop a locally running bot via stop file.")
    p_stop.set_defaults(func=_cmd_stop)

    p_check = sub.add_parser("check-config", help="Validate configuration without printing secrets.")
    p_check.set_defaults(func=_cmd_check_config)

    p_secret = sub.add_parser("set-secret", help="Store a secret in the host OS credential manager.")
    p_secret.add_argument("name", help="Secret name (TELEGRAM_BOT_TOKEN or ANTHROPIC_API_KEY).")
    p_secret.add_argument("--service", default=DEFAULT_KEYRING_SERVICE, help="Keyring service name.")
    p_secret.set_defaults(func=_cmd_set_secret)

    p_backup = sub.add_parser("backup", help="Create a backup bundle of the DB and approved folder.")
    p_backup.set_defaults(func=_cmd_backup)

    p_restore = sub.add_parser("restore", help="Restore DB and approved folder from a backup bundle.")
    p_restore.add_argument("bundle", help="Path to a backup-* directory.")
    p_restore.set_defaults(func=_cmd_restore)

    p_status = sub.add_parser("status", help="Show local operational state and monthly usage.")
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
