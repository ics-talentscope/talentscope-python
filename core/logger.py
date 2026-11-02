import logging
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger

_configured = False
_LOG_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{20,})?\b")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def _console_filter(record: dict) -> bool:
    return record["level"].no >= logging.WARNING or record["extra"].get("action") == "application_started"


def _environment_log_directory(directory: Path, environment: str) -> Path:
    folder = "prod" if environment.strip().lower() in {"prod", "production"} else "dev"
    return directory / folder


def archive_completed_months(directory: Path, today: date | None = None) -> list[Path]:
    current_month = (today or date.today()).replace(day=1)
    files_by_month: dict[str, list[Path]] = {}
    for path in directory.iterdir():
        match = _LOG_FILENAME_RE.fullmatch(path.name)
        if not path.is_file() or not match:
            continue
        try:
            log_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if log_date < current_month:
            files_by_month.setdefault(log_date.strftime("%Y-%m"), []).append(path)

    archives = []
    archive_directory = directory / "archive"
    for month, files in sorted(files_by_month.items()):
        archive_directory.mkdir(parents=True, exist_ok=True)
        archive_path = archive_directory / f"{month}.zip"
        source_names = {path.name for path in files}
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{month}-", suffix=".zip.tmp", dir=archive_directory
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as output:
                if archive_path.is_file():
                    with zipfile.ZipFile(archive_path, "r") as existing:
                        for item in existing.infolist():
                            if item.filename in source_names or item.is_dir():
                                continue
                            with existing.open(item) as source, output.open(item, "w") as target:
                                shutil.copyfileobj(source, target)
                for path in sorted(files):
                    output.write(path, arcname=path.name)

            with zipfile.ZipFile(temporary_path, "r") as archive:
                damaged_member = archive.testzip()
                if damaged_member is not None or not source_names.issubset(archive.namelist()):
                    raise zipfile.BadZipFile(f"Monthly log archive verification failed: {archive_path}")
            os.replace(temporary_path, archive_path)
            for path in files:
                path.unlink()
            archives.append(archive_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return archives


def run_log_archiver(directory: Path, environment: str) -> None:
    target = _environment_log_directory(directory, environment)
    while True:
        try:
            for archive_path in archive_completed_months(target):
                logger.bind(module="logging", action="monthly_archive_created").info(
                    "Monthly log archive created: {}", archive_path
                )
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            logger.bind(module="logging", action="monthly_archive_failed").exception(
                "Could not archive completed monthly logs"
            )

        now = datetime.now()
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        time.sleep(max(1, (next_run - now).total_seconds()))


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(exception=record.exc_info, depth=6).log(level, record.getMessage())


def _patch_record(record: dict) -> None:
    from core import request_context

    for field in ("module", "action", "status"):
        record["extra"].setdefault(field, "-")
    for key, value in request_context.log_context().items():
        record["extra"].setdefault(key, value)
    record["message"] = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", record["message"])
    record["message"] = _BEARER_RE.sub("Bearer [REDACTED]", record["message"])
    record["message"] = _JWT_RE.sub("[REDACTED_TOKEN]", record["message"])
    record["message"] = _EMAIL_RE.sub("[REDACTED_EMAIL]", record["message"])
    for key, value in tuple(record["extra"].items()):
        if re.search(r"password|secret|token|api[_-]?key|authorization|connection.?string", key, re.I):
            record["extra"][key] = "[REDACTED]"
        elif isinstance(value, str):
            value = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", value)
            value = _BEARER_RE.sub("Bearer [REDACTED]", value)
            record["extra"][key] = _JWT_RE.sub("[REDACTED_TOKEN]", value)
    if record.get("exception"):
        exception = record["exception"]
        if exception.value:
            safe_message = _EMAIL_RE.sub("[REDACTED_EMAIL]", str(exception.value))
            safe_message = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", safe_message)
            safe_message = _BEARER_RE.sub("Bearer [REDACTED]", safe_message)
            if safe_message != str(exception.value):
                try:
                    safe_exception = type(exception.value)(safe_message)
                except Exception:
                    safe_exception = RuntimeError(safe_message)
                record["exception"] = (exception.type, safe_exception, exception.traceback)


def configure_logging(directory: Path, level: str = "INFO", environment: str | None = None) -> None:
    global _configured
    if _configured:
        return

    target = _environment_log_directory(directory, environment or "development")
    target.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.configure(patcher=_patch_record)
    logger.add(
        sys.stderr,
        level=level,
        filter=_console_filter,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}{exception}\n",
        backtrace=False,
        diagnose=False,
    )

    logger.add(
        str(target / "{time:YYYY-MM-DD}.log"),
        level="DEBUG",
        rotation="00:00",
        retention="365 days",
        encoding="utf-8",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{extra[module]: <18} | {extra[action]: <28} | "
            "{extra[status]: <8} | {message} | {extra}{exception}\n"
        ),
        backtrace=False,
        diagnose=False,
        enqueue=True,
    )

    logging.root.handlers = [InterceptHandler()]
    logging.root.setLevel(logging.DEBUG)
    _configured = True
