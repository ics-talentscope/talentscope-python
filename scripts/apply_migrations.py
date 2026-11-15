"""Apply python/db/mssql/migrations with the same checksum rules as python/db/scripts/migrate.ts.

Usage (from the python directory, with the repo .env.local loaded by core.config):
  python scripts/apply_migrations.py
  python scripts/apply_migrations.py status
"""
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import mssql_settings
from core.db import MssqlStore
from core.migration_safety import assert_data_preserving_migration

GO_LINE = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE)


def migrations_dir() -> Path:
    # Canonical location: python/db/mssql/migrations (sibling of scripts/).
    path = Path(__file__).resolve().parents[1] / "db" / "mssql" / "migrations"
    if path.is_dir():
        return path
    raise FileNotFoundError("Could not find python/db/mssql/migrations. Run DB tools from the python folder.")


def split_batches(sql_text: str) -> list[str]:
    batches: list[str] = []
    current: list[str] = []
    for line in sql_text.splitlines():
        if GO_LINE.match(line):
            batch = "\n".join(current).strip()
            if batch:
                batches.append(batch)
            current = []
            continue
        current.append(line)
    tail = "\n".join(current).strip()
    if tail:
        batches.append(tail)
    return batches


def load_migrations(folder: Path) -> list[dict]:
    files = sorted(folder.glob("*.sql"), key=lambda path: path.name)
    out = []
    for path in files:
        text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        out.append({
            "file": path.name,
            "text": text,
            "checksum": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
    return out


def ensure_table(cursor) -> None:
    cursor.execute("""
IF OBJECT_ID(N'dbo.__migrations', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.__migrations (
    id            INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    filename      NVARCHAR(260) NOT NULL UNIQUE,
    applied_at    DATETIMEOFFSET NOT NULL CONSTRAINT DF___migrations_applied_at DEFAULT (SYSUTCDATETIME()),
    checksum      CHAR(64) NULL
  );
END
""")
    cursor.execute("""
IF COL_LENGTH(N'dbo.__migrations', N'checksum') IS NULL
  ALTER TABLE dbo.__migrations ADD checksum CHAR(64) NULL
""")


def applied_map(cursor) -> dict[str, str | None]:
    ensure_table(cursor)
    cursor.execute("SELECT filename, checksum FROM dbo.__migrations")
    rows = {}
    for filename, checksum in cursor.fetchall():
        rows[str(filename)] = str(checksum).strip() if checksum else None
    return rows


def apply(status_only: bool = False) -> None:
    settings = mssql_settings()
    folder = migrations_dir()
    migrations = load_migrations(folder)
    store = MssqlStore(settings)
    conn = store._connect()
    conn.autocommit = True
    cursor = conn.cursor()
    try:
        applied = applied_map(cursor)
        if status_only:
            pending = 0
            for item in migrations:
                stored = applied.get(item["file"], None)
                if item["file"] not in applied:
                    state = "[pending]"
                    pending += 1
                elif stored and stored != item["checksum"]:
                    state = "[EDITED AFTER APPLY]"
                else:
                    state = "[done]"
                print(f"  {state} {item['file']}")
            print(f"{pending} pending / {len(migrations)} total on {settings['server']}/{settings['database']}")
            return

        edited = [item["file"] for item in migrations if item["file"] in applied and applied[item["file"]] not in (None, item["checksum"])]
        if edited:
            raise RuntimeError(
                "These migrations were already applied but their files have been edited:\n  "
                + "\n  ".join(edited)
            )
        for item in migrations:
            if item["file"] in applied and applied[item["file"]] is None:
                cursor.execute(
                    "UPDATE dbo.__migrations SET checksum = ? WHERE filename = ?",
                    item["checksum"],
                    item["file"],
                )
        pending = [item for item in migrations if item["file"] not in applied]
        if not pending:
            print(f"No pending migrations on {settings['server']}/{settings['database']}.")
            return
        for item in pending:
            assert_data_preserving_migration(item["file"], item["text"])
        print(f"Applying {len(pending)} migration(s) to {settings['server']}/{settings['database']}...")
        for item in pending:
            batches = split_batches(item["text"])
            print(f"-> {item['file']} ({len(batches)} batch{'s' if len(batches) != 1 else ''})")
            conn.autocommit = False
            try:
                for batch in batches:
                    cursor.execute(batch)
                cursor.execute(
                    "INSERT INTO dbo.__migrations (filename, checksum) VALUES (?, ?)",
                    item["file"],
                    item["checksum"],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
        print("Migrations complete.")
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    apply(status_only="status" in sys.argv[1:])
