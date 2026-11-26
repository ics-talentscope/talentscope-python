import json
import threading
import time
from types import SimpleNamespace

import pytest

from core import security
from services import interviews, llm, resumes, settings

from .test_interview import RID, seed_candidate


@pytest.fixture(autouse=True)
def fresh_gate(monkeypatch):
    monkeypatch.setattr(llm, "gate", llm.Gate())
    llm._cache.clear()
    llm._ctx_cache.update(value=2048, at=time.monotonic())
    security.reset_rate_limits()


def fake_request(peer, forwarded=None):
    headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_client_ip_ignores_spoofed_forwarded_header_from_public_peer():
    assert security.get_client_ip(fake_request("8.8.8.8", "1.2.3.4")) == "8.8.8.8"


def test_client_ip_uses_last_public_hop_behind_private_proxies():
    request = fake_request("172.18.0.2", "6.6.6.6, 8.8.4.4, 10.0.0.4")
    assert security.get_client_ip(request) == "8.8.4.4"


def test_fit_prompt_trims_oversized_prompts_and_clamps_output():
    prompt = "H" * 200 + "M" * 20000 + "T" * 200
    fitted, out = llm.fit_prompt("sys", prompt, 3072)
    assert llm.estimate_tokens(fitted) + out + llm.estimate_tokens("sys") <= 2048
    assert fitted.startswith("H") and fitted.endswith("T") and out >= llm.MIN_OUTPUT_TOKENS
    assert llm.gate.stats["oversize_trimmed"] == 1


def test_gate_serves_live_before_batch(monkeypatch):
    monkeypatch.setenv("LLM_CONCURRENCY", "1")
    order = []
    release = threading.Event()

    def slow_request(system, prompt, temperature, max_tokens, timeout_s):
        order.append(prompt)
        if prompt == "first":
            release.wait(2)
        return "ok"

    monkeypatch.setattr(llm, "_request", slow_request)
    first = threading.Thread(target=llm.complete, args=("first",), kwargs={"temperature": 0.9})
    first.start()
    time.sleep(0.1)
    batch = threading.Thread(target=llm.complete, args=("batch",), kwargs={"temperature": 0.9, "priority": llm.PRIORITY_BATCH})
    batch.start()
    time.sleep(0.1)
    live = threading.Thread(target=llm.complete, args=("live",), kwargs={"temperature": 0.9, "priority": llm.PRIORITY_LIVE})
    live.start()
    time.sleep(0.1)
    release.set()
    for t in (first, batch, live):
        t.join(3)
    assert order == ["first", "live", "batch"]


def test_gate_rejects_when_queue_is_full(monkeypatch):
    monkeypatch.setenv("LLM_CONCURRENCY", "1")
    monkeypatch.setenv("LLM_MAX_QUEUE", "1")
    llm.gate.acquire(llm.PRIORITY_BATCH, 1)
    waiter = threading.Thread(target=lambda: pytest.raises(llm.LlmBusy, llm.gate.acquire, llm.PRIORITY_BATCH, 0.3))
    waiter.start()
    time.sleep(0.1)
    with pytest.raises(llm.LlmBusy):
        llm.gate.acquire(llm.PRIORITY_LIVE, 1)
    waiter.join(2)


def test_breaker_opens_after_repeated_failures_and_cache_skips_repeat_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_request", lambda *a: calls.append(1) or "answer")
    assert llm.complete("same prompt") == "answer"
    assert llm.complete("same prompt") == "answer"
    assert len(calls) == 1 and llm.gate.stats["cache_hits"] == 1

    def boom(*a):
        raise llm.LlmError("down")

    monkeypatch.setattr(llm, "_request", boom)
    for i in range(llm.BREAKER_THRESHOLD):
        with pytest.raises(llm.LlmError):
            llm.complete(f"p{i}", temperature=0.9)
    with pytest.raises(llm.LlmBusy):
        llm.complete("p-next", temperature=0.9)


def test_interview_duration_uses_default_then_candidate_override(client, store):
    auth = seed_candidate(store)
    assert client.get(f"/api/interview/{RID}/timing", headers=auth).json()["durationSeconds"] == 15 * 60
    settings.update_interview_defaults({"durationMinutes": 30})
    assert client.get(f"/api/interview/{RID}/timing", headers=auth).json()["durationSeconds"] == 30 * 60
    store.update("resumes", {"report": {"interviewConfig": {"durationMinutes": 45}}}, {"id": RID})
    resumes._cache.clear()
    assert client.get(f"/api/interview/{RID}/timing", headers=auth).json()["durationSeconds"] == 45 * 60


def test_duration_is_clamped():
    assert settings.clamp_duration(1) == settings.DURATION_MIN
    assert settings.clamp_duration(9999) == settings.DURATION_MAX
    assert settings.clamp_duration("abc", 20) == 20


def test_start_is_idempotent_and_expiry_blocks_submissions(client, store):
    auth = seed_candidate(store, interviewConfig={"durationMinutes": 5})
    first = client.post(f"/api/interview/{RID}/start", headers=auth).json()["data"]
    second = client.post(f"/api/interview/{RID}/start", headers=auth).json()["data"]
    assert first["startedAt"] == second["startedAt"] and first["remainingSeconds"] <= 300
    ok = client.post(f"/api/interview/{RID}/submit_answer", headers=auth, json={"question_index": 0, "question": "Q?", "answer": "-"})
    assert ok.status_code == 200
    past = "2020-01-01T00:00:00+00:00"
    store.update("resumes", {"report": {"interviewConfig": {"durationMinutes": 5}, "interviewStartedAt": past}}, {"id": RID})
    store.update("candidate_interview_data", {"interview_started_at": past}, {"id": RID})
    resumes._cache.clear()
    late = client.post(f"/api/interview/{RID}/submit_answer", headers=auth, json={"question_index": 1, "question": "Q?", "answer": "-"})
    assert late.status_code == 409
    assert interviews.timing(RID)["remainingSeconds"] == 0


def test_global_rate_limit_and_health_exemption(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_IP_PER_MIN", "3")
    codes = [client.get("/api/portal_settings").status_code for _ in range(6)]
    assert 429 in codes
    assert client.get("/api/health").status_code != 429


def test_inflight_cap_returns_503(client, monkeypatch):
    monkeypatch.setenv("MAX_INFLIGHT", "1")
    security._inflight["count"] = 1
    try:
        assert client.get("/api/portal_settings").status_code == 503
    finally:
        security._inflight["count"] = 0


def test_retention_prunes_only_old_recordings(tmp_path, monkeypatch):
    import os

    from services import storage

    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    old, fresh = tmp_path / "interview-recording-old.webm", tmp_path / "interview-recording-fresh.webm"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (time.time() - 90 * 86400,) * 2)
    assert storage.prune_older_than("recordings", 30) == 1
    assert not old.exists() and fresh.exists()
    assert storage.prune_older_than("recordings", 0) == 0


def test_invalid_interview_ids_are_client_errors_not_500(client):
    for bad in ("not_a_valid_id_%21%21", "42", "zzq-invented-control-9f3a"):
        for suffix in ("questions", "timing", "video"):
            assert client.get(f"/api/interview/{bad}/{suffix}").status_code < 500


def test_oversized_and_malformed_bodies_are_rejected_early(client):
    assert client.post("/api/admin/auth/login", content=b"x", headers={"content-length": "999999999"}).status_code == 413
    assert client.post("/api/admin/auth/login", content=b"x", headers={"content-length": "abc"}).status_code in (400, 422)


def test_responses_carry_security_headers_and_no_store(client):
    res = client.get("/api/portal_settings")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-frame-options"] == "DENY"


def test_api_docs_are_disabled_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_query_string_token_only_works_for_get(client):
    assert client.post("/api/admin/resumes/batch_delete?token=bogus", json={}).status_code in (401, 403, 404, 405)
    from core import auth
    from core.tokens import sign_token

    token = sign_token("admin", 60_000)
    get_req = fake_request("127.0.0.1")
    get_req.method, get_req.headers, get_req.query_params = "GET", {}, {"token": token}
    post_req = fake_request("127.0.0.1")
    post_req.method, post_req.headers, post_req.query_params = "POST", {}, {"token": token}
    assert auth.is_admin_request(get_req) is True
    assert auth.is_admin_request(post_req) is False


def test_sql_injection_payloads_are_treated_as_data(client, store):
    for payload in ("' OR 1=1 --", "'; DROP TABLE resumes; --", "1; WAITFOR DELAY '0:0:5'"):
        res = client.post("/api/admin/auth/login", json={"email": f"{payload}@infinite.com", "password": payload})
        assert res.status_code in (400, 401, 429, 503)
        assert client.get(f"/api/interview/{payload}/timing").status_code < 500
    from core.db import ident

    for bad in ("resumes; DROP TABLE x", "a b", "name]--", ""):
        with pytest.raises(ValueError):
            ident(bad)
