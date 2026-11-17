from pathlib import Path

import pytest

from core.migration_safety import assert_data_preserving_migration


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_DIRECTORIES = (
    ROOT / "db" / "mssql" / "migrations",
    ROOT / "db" / "postgres" / "migrations",
    ROOT / "db" / "supabase" / "migrations",
)


@pytest.mark.parametrize(
    "statement",
    (
        "DELETE FROM resumes WHERE id = 'x';",
        "DELETE r FROM resumes AS r;",
        "TRUNCATE TABLE resumes;",
        "DROP TABLE resumes;",
        "ALTER TABLE resumes DROP COLUMN parsed;",
    ),
)
def test_data_destructive_migration_statements_are_rejected(statement):
    with pytest.raises(RuntimeError, match="data-destructive"):
        assert_data_preserving_migration("unsafe.sql", statement)


def test_comments_and_constraint_replacement_are_allowed():
    sql = """-- DELETE FROM resumes;
    ALTER TABLE dbo.candidate_sessions DROP CONSTRAINT UQ_cs_code;
    CREATE UNIQUE INDEX UX_cs_code ON dbo.candidate_sessions(code) WHERE code IS NOT NULL;
    """
    assert_data_preserving_migration("safe.sql", sql)


def test_existing_migrations_do_not_delete_or_drop_existing_data():
    migrations = [path for folder in MIGRATION_DIRECTORIES for path in folder.glob("*.sql")]
    assert migrations
    for path in migrations:
        assert_data_preserving_migration(path.name, path.read_text(encoding="utf-8-sig"))
