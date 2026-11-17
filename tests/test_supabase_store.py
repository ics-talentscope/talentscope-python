import json

import httpx
import pytest

from core import db as db_module
from core.config import db_target
from core.db import SupabaseStore


def make_store(handler):
    store = SupabaseStore({
        "url": "https://project.supabase.co",
        "service_role_key": "test-server-key",
        "timeout_s": 1,
    })
    store.client.close()
    store.client = httpx.Client(
        base_url="https://project.supabase.co/rest/v1/",
        headers={
            "apikey": "test-server-key",
            "Authorization": "Bearer test-server-key",
            "Accept-Profile": "public",
            "Content-Profile": "public",
        },
        transport=httpx.MockTransport(handler),
    )
    return store


def test_select_uses_postgrest_filters_headers_and_pagination():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[{"id": "some-id", "email": "user@example.com"}])

    store = make_store(handler)
    try:
        rows = store.select(
            "employees",
            [("email", "ilike", "%@example.com"), ("is_first_login", "eq", True)],
            ["id", "email"],
            order=[("created_at", "desc")],
            limit=10,
            offset=5,
        )
    finally:
        store.client.close()

    request = requests[0]
    assert rows == [{"id": "some-id", "email": "user@example.com"}]
    assert request.method == "GET"
    assert request.url.path == "/rest/v1/employees"
    assert request.url.params["select"] == "id,email"
    assert request.url.params["email"] == "ilike.%@example.com"
    assert request.url.params["is_first_login"] == "eq.true"
    assert request.url.params["order"] == "created_at.desc"
    assert request.url.params["limit"] == "10"
    assert request.url.params["offset"] == "5"
    assert request.headers["apikey"] == "test-server-key"
    assert request.headers["authorization"] == "Bearer test-server-key"


def test_filters_encode_null_empty_in_and_unsafe_values():
    store = object.__new__(SupabaseStore)

    filters = store._where("employees", [
        ("email", "eq", 'a,b"c'),
        ("id", "in", []),
        ("email", "not_null", None),
    ])

    assert filters == [("email", 'eq."a,b\\"c"'), ("email", "not.is.null")]
    with pytest.raises(ValueError):
        store.select("employees; DROP TABLE users")


def test_count_uses_exact_content_range():
    def handler(request):
        assert request.method == "HEAD"
        assert request.headers["prefer"] == "count=exact"
        assert request.url.params["select"] == "*"
        return httpx.Response(200, headers={"content-range": "0-0/7"})

    store = make_store(handler)
    try:
        assert store.count("employees", {"department": "Engineering"}) == 7
    finally:
        store.client.close()


def test_insert_sends_json_and_requests_returned_row():
    def handler(request):
        assert request.method == "POST"
        assert request.headers["prefer"] == "return=representation"
        payload = json.loads(request.content)
        assert payload["value"] == {"mode": "dark"}
        return httpx.Response(201, json=[{"key": "theme", "value": {"mode": "dark"}}])

    store = make_store(handler)
    try:
        row = store.insert("portal_settings", {"key": "theme", "value": {"mode": "dark"}})[0]
    finally:
        store.client.close()

    assert row == {"key": "theme", "value": {"mode": "dark"}}


def test_question_set_insert_does_not_send_an_id():
    def handler(request):
        payload = json.loads(request.content)
        assert payload == {"resume_id": "resume-1", "status": "queued"}
        return httpx.Response(201, json=[payload])

    store = make_store(handler)
    try:
        row = store.insert("question_sets", {"resume_id": "resume-1", "status": "queued"})[0]
    finally:
        store.client.close()

    assert row == {"resume_id": "resume-1", "status": "queued"}


def test_question_set_upsert_does_not_send_an_id():
    def handler(request):
        payload = json.loads(request.content)
        assert payload == {"resume_id": "resume-1", "status": "queued"}
        assert request.url.params["on_conflict"] == "resume_id"
        return httpx.Response(200, json=[payload])

    store = make_store(handler)
    try:
        row = store.upsert("question_sets", {"resume_id": "resume-1", "status": "queued"}, "resume_id")
    finally:
        store.client.close()

    assert row == {"resume_id": "resume-1", "status": "queued"}


def test_update_delete_and_upsert_use_postgrest_methods():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method in {"PATCH", "DELETE", "POST"}:
            return httpx.Response(200, json=[{"id": "some-id"}])
        raise AssertionError(f"Unexpected method: {request.method}")

    store = make_store(handler)
    try:
        assert store.update("employees", {"full_name": "Updated"}, {"id": "some-id"}) == 1
        assert store.delete("employees", {"id": "some-id"}) == 1
        row = store.upsert("portal_settings", {"key": "theme", "value": "dark"}, "key")
    finally:
        store.client.close()

    assert row == {"id": "some-id"}
    assert [request.method for request in requests] == ["PATCH", "DELETE", "POST"]
    assert requests[0].url.params["id"] == "eq.some-id"
    assert "resolution=merge-duplicates" in requests[2].headers["prefer"]
    assert requests[2].url.params["on_conflict"] == "key"


def test_supabase_requires_url_and_service_role_key():
    with pytest.raises(ValueError, match="SUPABASE_SERVICE_ROLE_KEY"):
        SupabaseStore({"url": "https://project.supabase.co", "service_role_key": ""})
    with pytest.raises(ValueError, match="HTTPS"):
        SupabaseStore({"url": "http://project.supabase.co", "service_role_key": "key"})


@pytest.mark.parametrize(("configured", "expected"), [("MSSQL", "MSSQL"), ("supabase", "SUPABASE")])
def test_db_target_is_case_insensitive(monkeypatch, configured, expected):
    monkeypatch.setenv("DB_TARGET", configured)

    assert db_target() == expected


def test_db_target_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("DB_TARGET", "mysql")

    with pytest.raises(ValueError, match="DB_TARGET must be MSSQL or SUPABASE"):
        db_target()


def test_get_store_uses_supabase_when_selected(monkeypatch):
    selected = object()
    db_module.set_store(None)
    monkeypatch.setattr(db_module, "db_target", lambda: "SUPABASE")
    monkeypatch.setattr(db_module, "SupabaseStore", lambda: selected)

    try:
        assert db_module.get_store() is selected
    finally:
        db_module.set_store(None)


def test_get_store_requires_explicit_db_target_for_supabase(monkeypatch):
    selected = object()
    db_module.set_store(None)
    monkeypatch.setenv("DB_PROVIDER", "supabase")
    monkeypatch.setattr(db_module, "db_target", lambda: "MSSQL")
    monkeypatch.setattr(db_module, "MssqlStore", lambda: selected)

    try:
        assert db_module.get_store() is selected
    finally:
        db_module.set_store(None)
