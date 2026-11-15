import pytest

sqlglot = pytest.importorskip("sqlglot")

from core.db import MssqlStore


class Recorder(MssqlStore):
    def __init__(self):
        super().__init__({"target": "local", "server": "x", "port": 1, "database": "d", "user": "u", "password": "p", "encrypt": True,
                          "trust_cert": True, "driver": "ODBC Driver 18 for SQL Server"})
        self.sql: list[tuple[str, tuple]] = []

    def _run(self, sql, params, fetch):
        self.sql.append((sql, params))
        if fetch:
            return [{"n": 1, "id": "x"}]
        return 1


def parse_all(store: Recorder) -> None:
    for statement, params in store.sql:
        assert statement.count("?") == len(params), statement
        for part in [s for s in statement.replace("?", "1").split(";") if s.strip()]:
            sqlglot.parse_one(part, read="tsql")


def test_every_generated_statement_is_valid_tsql():
    store = Recorder()
    store.select("resumes", {"id": "a", "error": None, "filename": ["x", "y"]}, ["id", "filename"], "created_at desc", limit=5)
    store.select("resumes", [("created_at", "gte", "2026-01-01"), ("filename", "like", "a%"), ("id", "not_in", ["1"]), ("error", "not_null", None)],
                 order=[("created_at", "desc"), ("id", "asc")], limit=10, offset=20)
    store.select("resumes", offset=0)
    store.select("tests", {"id": list(range(2500))})
    store.select("tests", {"id": []})
    store.count("resumes", {"id": "a"})
    store.insert("employees", {"employee_id": "E1", "badges": ["a"], "created_at": "2026-01-01T00:00:00Z"})
    store.insert("portal_settings", {"key": "k", "value": {"a": 1}})
    store.update("tests", {"proctoring": {"a": 1}, "status": "completed", "completed_at": "2026-01-01T00:00:00Z"}, {"id": "t"})
    store.update("tests", {"status": "x"}, {"id": list(range(1500))})
    store.delete("test_attempts", {"test_id": "t"})
    store.delete("audit_logs", None)
    store.upsert("employees", {"employee_id": "E1", "full_name": "A"}, "employee_id")
    store.upsert("portal_settings", {"key": "k", "value": 1}, "key")
    store.upsert("candidate_sessions", {"email": "a@x.com", "code": None}, ["email"])
    assert len(store.sql) > 15
    parse_all(store)


def test_identifiers_are_validated():
    store = Recorder()
    with pytest.raises(ValueError):
        store.select("resumes; DROP TABLE x", None)
    with pytest.raises(ValueError):
        store.select("resumes", {"id; --": 1})
    with pytest.raises(ValueError):
        store.update("tests", {"a b": 1}, {"id": 1})


def test_question_sets_insert_uses_resume_id_without_adding_id_column():
    store = Recorder()
    store.insert("question_sets", {"resume_id": "resume-1", "status": "queued"})

    statement, params = store.sql[-1]
    assert "[resume_id]" in statement and "[id]" not in statement
    assert params == ("resume-1", "queued")


def test_connection_string_escapes_password():
    store = Recorder()
    store.settings["password"] = "pa}ss;word"
    conn = store._conn_string()
    assert "PWD={pa}}ss;word}" in conn and "TrustServerCertificate=yes" in conn and "Encrypt=yes" in conn
