"""Operational utilities: redacted metadata logging, backup/restore, and local stop control."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config
from .db import Database
from .security import RedactingLogFilter, SecretRedactor

logger = logging.getLogger(__name__)


def configure_logging(config: Config, redactor: SecretRedactor) -> None:
    """Configure operational metadata-only logging with secret redaction."""
    config.log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    redacting_filter = RedactingLogFilter(redactor)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    console.addFilter(redacting_filter)

    log_file = config.log_dir / "assistant.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redacting_filter)

    root.addHandler(console)
    root.addHandler(file_handler)

    # Suppress verbose third-party HTTP request/body logging that could leak tokens or prompts
    for noisy in ("httpx", "httpcore", "telegram", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _restrict_owner_permissions(path: Path) -> None:
    """Apply best-effort owner-only permissions on Windows (icacls) or POSIX (chmod)."""
    if os.name == "nt":
        user = os.environ.get("USERNAME")
        if user:
            try:
                subprocess.run(
                    [
                        "icacls",
                        str(path),
                        "/inheritance:r",
                        "/grant:r",
                        f"{user}:(OI)(CI)F" if path.is_dir() else f"{user}:F",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except OSError:
                pass
    else:
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:
            pass


def create_backup(config: Config, db: Database | None = None) -> Path:
    """Create a consistent timestamped backup of the SQLite DB and approved folder."""
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    _restrict_owner_permissions(config.backup_dir)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle_dir = config.backup_dir / f"backup-{stamp}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    owns_db = db is None
    database = db or Database(config.db_path)
    try:
        db_snapshot = bundle_dir / "assistant.db"
        database.backup_to(db_snapshot)
    finally:
        if owns_db:
            database.close()

    folder_zip = bundle_dir / "approved_folder.zip"
    root = config.approved_folder.resolve()
    with zipfile.ZipFile(folder_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if root.exists():
            for item in sorted(root.rglob("*")):
                if item.is_symlink() or not item.is_file():
                    continue
                zf.write(item, arcname=item.relative_to(root).as_posix())

    _restrict_owner_permissions(bundle_dir)
    logger.info("backup_created path=%s", bundle_dir.name)
    return bundle_dir


def restore_backup(
    backup_bundle_dir: Path,
    *,
    target_db_path: Path,
    target_approved_folder: Path,
) -> None:
    """Restore SQLite database and approved folder from a backup bundle."""
    bundle = Path(backup_bundle_dir).resolve()
    db_src = bundle / "assistant.db"
    zip_src = bundle / "approved_folder.zip"
    if not db_src.is_file():
        raise FileNotFoundError(f"Backup database missing: {db_src}")
    if not zip_src.is_file():
        raise FileNotFoundError(f"Backup folder archive missing: {zip_src}")

    target_db_path = Path(target_db_path).resolve()
    target_db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_src, target_db_path)
    # Remove stale WAL/SHM sidecars if present
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target_db_path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()

    target_approved_folder = Path(target_approved_folder).resolve()
    target_approved_folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_src, "r") as zf:
        for member in zf.infolist():
            member_path = (target_approved_folder / member.filename).resolve()
            # Prevent Zip Slip traversal during restore
            member_path.relative_to(target_approved_folder)
            if member.is_dir():
                member_path.mkdir(parents=True, exist_ok=True)
            else:
                member_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src_f, open(member_path, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)

    logger.info("backup_restored bundle=%s", bundle.name)


def prune_old_backups(backup_dir: Path, retention_days: int) -> int:
    """Delete backup bundles older than retention_days."""
    if not backup_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for item in backup_dir.iterdir():
        if not item.is_dir() or not item.name.startswith("backup-"):
            continue
        mtime = datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            shutil.rmtree(item, ignore_errors=True)
            removed += 1
    return removed


def request_local_stop(stop_file: Path) -> Path:
    """Create the local stop sentinel file so a running bot exits cleanly."""
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(f"stop_requested_utc={datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
    return stop_file


def clear_local_stop(stop_file: Path) -> None:
    if stop_file.exists():
        stop_file.unlink()


def is_stop_requested(stop_file: Path) -> bool:
    return stop_file.exists()
