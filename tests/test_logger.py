from datetime import date
from loguru import logger
import zipfile

from pathlib import Path

from core.logger import _console_filter, _environment_log_directory, archive_completed_months


def test_console_filter_only_shows_startup_and_warnings_or_higher():
    def record(level, action=None):
        return {"level": logger.level(level), "extra": {"action": action}}

    assert not _console_filter(record("INFO", "database_select"))
    assert _console_filter(record("INFO", "application_started"))
    assert _console_filter(record("WARNING"))
    assert _console_filter(record("ERROR"))


def test_log_directory_is_separated_by_environment(tmp_path: Path):
    assert _environment_log_directory(tmp_path, "production") == tmp_path / "prod"
    assert _environment_log_directory(tmp_path, "Prod") == tmp_path / "prod"
    assert _environment_log_directory(tmp_path, "development") == tmp_path / "dev"
    assert _environment_log_directory(tmp_path, "local") == tmp_path / "dev"


def test_archives_completed_month_and_keeps_current_month_logs(tmp_path: Path):
    log_directory = tmp_path / "prod"
    log_directory.mkdir()
    (log_directory / "2026-10-30.log").write_bytes(b"first day\n")
    (log_directory / "2026-10-31.log").write_bytes(b"last day\n")
    current_log = log_directory / "2026-11-01.log"
    current_log.write_bytes(b"current month\n")

    archives = archive_completed_months(log_directory, today=date(2026, 11, 1))

    archive_path = log_directory / "archive" / "2026-10.zip"
    assert archives == [archive_path]
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {"2026-10-30.log", "2026-10-31.log"}
        assert archive.read("2026-10-30.log") == b"first day\n"
        assert archive.testzip() is None
    assert not (log_directory / "2026-10-30.log").exists()
    assert not (log_directory / "2026-10-31.log").exists()
    assert current_log.read_bytes() == b"current month\n"


def test_monthly_archive_includes_late_file_without_duplicate_entries(tmp_path: Path):
    log_directory = tmp_path / "dev"
    log_directory.mkdir()
    (log_directory / "2026-10-31.log").write_bytes(b"initial\n")
    archive_completed_months(log_directory, today=date(2026, 11, 1))

    late_log = log_directory / "2026-10-15.log"
    late_log.write_bytes(b"late file\n")
    archive_completed_months(log_directory, today=date(2026, 11, 2))

    with zipfile.ZipFile(log_directory / "archive" / "2026-10.zip") as archive:
        assert set(archive.namelist()) == {"2026-10-15.log", "2026-10-31.log"}
        assert archive.read("2026-10-15.log") == b"late file\n"
        assert archive.testzip() is None
    assert not late_log.exists()
