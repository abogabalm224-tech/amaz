"""Backup and restore for server migration."""

import asyncio
import logging
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from config import DATABASE_PATH, TELEGRAM_SESSION_NAME

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent

BACKUP_ROOT_FILES = frozenset(
    {
        ".env",
        "frame.png",
        "requirements.txt",
        "config.py",
        Path(DATABASE_PATH).name,
    }
)

SESSION_BASENAMES = frozenset(
    {
        f"{TELEGRAM_SESSION_NAME}.session",
        f"{TELEGRAM_SESSION_NAME}.session-journal",
    }
)


def _backup_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"amazon_bot_backup_{stamp}.zip"


def _collect_backup_files(root: Path) -> list[tuple[Path, str]]:
    """Return (absolute path, archive name) pairs."""
    items: list[tuple[Path, str]] = []

    for name in BACKUP_ROOT_FILES:
        path = root / name
        if path.is_file():
            items.append((path, name))

    for name in SESSION_BASENAMES:
        path = root / name
        if path.is_file():
            items.append((path, name))

    logs_dir = root / "logs"
    if logs_dir.is_dir():
        for log_file in sorted(logs_dir.iterdir()):
            if log_file.is_file():
                items.append((log_file, f"logs/{log_file.name}"))

    return items


def create_backup_zip(root: Path | None = None) -> Path:
    root = root or PROJECT_ROOT
    out_path = root / _backup_filename()
    items = _collect_backup_files(root)
    if not items:
        raise FileNotFoundError("No backup files found in project directory")

    logger.info("BACKUP START — %s file(s)", len(items))
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path, arcname in items:
            zf.write(file_path, arcname=arcname)
            logger.info("BACKUP ADDED: %s", arcname)

    logger.info("BACKUP CREATED: %s", out_path.name)
    return out_path


def _normalize_zip_name(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def is_safe_zip_member(name: str) -> bool:
    norm = _normalize_zip_name(name)
    if not norm or norm.endswith("/"):
        return False
    if ".." in norm.split("/"):
        return False
    parts = norm.split("/")
    if len(parts) == 1:
        basename = parts[0]
        if basename in BACKUP_ROOT_FILES:
            return True
        if basename in SESSION_BASENAMES:
            return True
        return basename.endswith(".session") or basename.endswith(".session-journal")
    if len(parts) == 2 and parts[0] == "logs":
        return bool(parts[1]) and ".." not in parts[1]
    return False


def validate_backup_zip(zip_path: Path) -> list[str]:
    errors: list[str] = []
    if not zip_path.is_file():
        return ["Backup file not found."]

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return ["Invalid or corrupted ZIP archive."]

    if not names:
        return ["ZIP archive is empty."]

    unsafe = [n for n in names if not is_safe_zip_member(n)]
    if unsafe:
        errors.append(f"Unsafe paths in archive: {', '.join(unsafe[:5])}")

    has_db = Path(DATABASE_PATH).name in {_normalize_zip_name(n) for n in names}
    if not has_db:
        errors.append(f"Missing required file: {Path(DATABASE_PATH).name}")

    return errors


def restore_from_zip(zip_path: Path, root: Path | None = None) -> list[str]:
    root = root or PROJECT_ROOT
    errors = validate_backup_zip(zip_path)
    if errors:
        raise ValueError("; ".join(errors))

    restored: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if not is_safe_zip_member(member):
                continue
            norm = _normalize_zip_name(member)
            target = root / norm
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            restored.append(norm)
            logger.info("RESTORED: %s", norm)

    logger.info("RESTORE COMPLETE — %s file(s)", len(restored))
    return restored


async def create_backup_archive(root: Path | None = None) -> Path:
    return await asyncio.to_thread(create_backup_zip, root)


async def shutdown_for_restore(application) -> None:
    application.bot_data["ready"] = False

    for key in ("worker_task", "approval_timeout_task"):
        task = application.bot_data.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    browser = application.bot_data.get("browser")
    if browser:
        from amazon_scraper import BrowserManager

        if isinstance(browser, BrowserManager):
            await browser.stop()

    from link_resolver import close_http_client
    from telegram_listener import stop_telethon_listener

    await stop_telethon_listener(application)
    await close_http_client()
    logger.info("Shutdown complete (pre-restore)")


def restart_bot_process() -> None:
    logger.info("RESTARTING BOT PROCESS")
    script = Path(__file__).resolve().parent / "bot.py"
    if script.is_file():
        os.execv(sys.executable, [sys.executable, str(script)])
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)


async def apply_restore_and_restart(application, zip_path: Path) -> None:
    await shutdown_for_restore(application)
    restore_from_zip(zip_path)
    restart_bot_process()
