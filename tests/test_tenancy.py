import hashlib
import hmac
import json
import time

import pytest

from core import tenancy
from core.tokens import _b64url_encode, _secret, sign_token
from services import admin_accounts, settings, storage


def test_store_rows_are_scoped_by_client(store):
    with tenancy.client_context("infinite"):
        store.insert("resumes", {"id": "resume-1", "filename": "infinite.pdf"})
    with tenancy.client_context("slalom"):
        store.insert("resumes", {"id": "resume-2", "filename": "slalom.pdf"})

        assert store.select("resumes", {"id": "resume-1"}) == []
        assert store.select("resumes", {"id": "resume-2"})[0]["filename"] == "slalom.pdf"
    with tenancy.client_context("infinite"):
        assert store.select("resumes", {"id": "resume-2"}) == []
        assert store.select("resumes", {"id": "resume-1"})[0]["filename"] == "infinite.pdf"

    with tenancy.unscoped():
        assert len(store.select("resumes")) == 2


def test_portal_settings_are_scoped_by_client(store):
    with tenancy.client_context("infinite"):
        settings.set_setting("client_probe", {"value": "infinite"})
    with tenancy.client_context("slalom"):
        assert settings.get_setting("client_probe") is None
        settings.set_setting("client_probe", {"value": "slalom"})
    with tenancy.client_context("infinite"):
        assert settings.get_setting("client_probe") == {"value": "infinite"}


def test_admin_passwords_are_client_scoped(store, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "InfinitePass123!")
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomPass123!")
    admin_accounts.invalidate_cache()

    infinite = admin_accounts.authenticate("admin@infinite.com", "InfinitePass123!")
    slalom = admin_accounts.authenticate("admin@slalom.com", "SlalomPass123!")

    assert infinite["client"] == "infinite"
    assert slalom["client"] == "slalom"
    assert admin_accounts.authenticate("admin@slalom.com", "InfinitePass123!") is None
    assert admin_accounts.authenticate("admin@infinite.com", "SlalomPass123!") is None


def test_slalom_admin_password_comes_from_environment_not_database(store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_EMAIL", "bootcamp-admin@slalom.com")
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "EnvironmentPass123!")
    admin_accounts.set_console_password("ObsoleteDatabasePass123!", "slalom")
    stored = admin_accounts.console_secret("slalom")

    access = admin_accounts.authenticate("bootcamp-admin@slalom.com", "EnvironmentPass123!")

    assert access["email"] == "bootcamp-admin@slalom.com"
    assert admin_accounts.authenticate("bootcamp-admin@slalom.com", "ObsoleteDatabasePass123!") is None
    assert admin_accounts.ensure_console_password("slalom") is None
    assert admin_accounts.console_secret("slalom") == stored


def test_mismatched_token_client_is_rejected(client):
    token = sign_token("admin", 60 * 60 * 1000, email="admin@infinite.com", client="slalom")
    response = client.get("/api/admin/auth/validate", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_unknown_client_claim_is_rejected(client):
    # Build the token directly: sign_token() normalizes unknown clients, so it
    # cannot represent a stale claim issued before a client was renamed.
    claims = {
        "employee_id": "ADMIN",
        "email": "admin@infinite.com",
        "client": "retired-client",
        "exp": int(time.time() * 1000) + 60 * 60 * 1000,
    }
    encoded = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    token = f"{encoded}.{signature}"

    response = client.get("/api/admin/auth/validate", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_login_audit_rows_are_scoped_by_client(client, store, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "InfinitePass123!")
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomPass123!")
    admin_accounts.invalidate_cache()

    for email, password in (
        ("admin@infinite.com", "InfinitePass123!"),
        ("admin@slalom.com", "SlalomPass123!"),
    ):
        response = client.post("/api/admin/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text

    with tenancy.unscoped():
        rows = store.select("audit_logs", where={"action": "ADMIN_LOGIN_SUCCESS"})
    assert {row["actor_email"]: row["client_id"] for row in rows} == {
        "admin@infinite.com": "infinite",
        "admin@slalom.com": "slalom",
    }


def test_upload_buckets_are_scoped_on_disk(store, monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(storage, "_sql_ok", lambda: False)

    with tenancy.client_context("infinite"):
        storage.save("resumes", "candidate.txt", b"infinite")
    with tenancy.client_context("slalom"):
        assert storage.read("resumes", "candidate.txt") is None
        storage.save("resumes", "candidate.txt", b"slalom")
        assert storage.read("resumes", "candidate.txt") == b"slalom"

    assert (tmp_path / "resume-candidate.txt").read_bytes() == b"infinite"
    assert (tmp_path / "slalom-resume-candidate.txt").read_bytes() == b"slalom"


def test_raw_sql_filter_follows_request_scope():
    with tenancy.client_context("slalom"):
        clause, params = tenancy.sql_filter("t")
        assert clause == " AND t.client_id = ?"
        assert params == ("slalom",)
    with tenancy.unscoped():
        assert tenancy.sql_filter() == ("", ())
