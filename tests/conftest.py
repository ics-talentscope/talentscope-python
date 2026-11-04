import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import re

import pytest
from fastapi.testclient import TestClient

from core import security
from core.db import MemoryStore, normalize_columns, normalize_where, set_store
from services import admin_accounts, resumes, saml

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "db" / "mssql" / "migrations"


def load_schema() -> dict[str, set]:
    text = "".join(f.read_text(encoding="utf-8") + "\n" for f in sorted(MIGRATIONS.glob("*.sql")))
    tables: dict[str, set] = {}
    for m in re.finditer(r"CREATE TABLE dbo\.(\w+)\s*\((.*?)\n  \);", text, re.S):
        cols = set()
        for line in m.group(2).split("\n"):
            mm = re.match(r"^\s{4}\[?(\w+)\]?\s+(UNIQUEIDENTIFIER|NVARCHAR|INT|BIT|DATETIMEOFFSET|DATE|DECIMAL|BIGINT|FLOAT|VARCHAR|DATETIME2)", line)
            if mm:
                cols.add(mm.group(1))
        tables[m.group(1)] = cols
    for m in re.finditer(r"ALTER TABLE dbo\.(\w+)\s+ADD\s+(\w+)\s+", text, re.I):
        tables.setdefault(m.group(1), set()).add(m.group(2))
    tables.setdefault("portal_settings", set()).add("key")
    return tables


SCHEMA = load_schema()


class SchemaStore(MemoryStore):
    def _check(self, table, columns):
        assert table in SCHEMA, f"unknown table {table}"
        bad = [c for c in columns if c not in SCHEMA[table]]
        assert not bad, f"{table}: columns not in MSSQL schema: {bad}"

    def select(self, table, where=None, columns=None, order=None, limit=None, offset=None):
        self._check(table, [c for c, _, _ in normalize_where(where)] + (normalize_columns(columns) or []))
        return super().select(table, where, columns, order, limit, offset)

    def insert(self, table, rows):
        for row in ([rows] if isinstance(rows, dict) else rows):
            self._check(table, row.keys())
        return super().insert(table, rows)

    def update(self, table, values, where):
        self._check(table, list(values.keys()) + [c for c, _, _ in normalize_where(where)])
        return super().update(table, values, where)

    def delete(self, table, where):
        self._check(table, [c for c, _, _ in normalize_where(where)])
        return super().delete(table, where)

    def upsert(self, table, row, key):
        self._check(table, row.keys())
        return super().upsert(table, row, key)

    def count(self, table, where=None):
        self._check(table, [c for c, _, _ in normalize_where(where)])
        return super().count(table, where)


@pytest.fixture()
def store(monkeypatch):
    memory = SchemaStore()
    set_store(memory)
    security.reset_rate_limits()
    admin_accounts.invalidate_cache()
    saml.reset_replay_cache()
    resumes._cache.clear()
    for name in ("ADMIN_PASSWORD", "SCREENING_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAML_ENABLED", "0")
    monkeypatch.setenv("SAML_ADMIN_SSO", "0")
    yield memory
    set_store(None)


@pytest.fixture()
def client(store):
    from main import app

    return TestClient(app)
