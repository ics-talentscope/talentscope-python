import asyncio
import base64
import json
import re

import pytest

from core.tokens import sign_interview_token
from services import ai, face, identity, interviews, llm, question_gen, question_queue, resumes, sessions

RID = "11111111-1111-4111-8111-111111111111"


def seed_candidate(store, **report):
    store.seed("resumes", [{"id": RID, "filename": "a.pdf", "parsed": json.dumps({"personal": {"email": "c@x.com"}, "skills": {"technical": ["python"], "tools": []}, "projects": []}),
                            "report": json.dumps(report), "analysis": "{}", "enhanced": "{}"}])
    store.seed("candidate_sessions", [{"id": "s1", "email": "c@x.com", "resume_id": RID, "used": False, "code": None}])
    return {"Authorization": f"Bearer {sign_interview_token(RID)}"}


def test_access_flow(client, store, monkeypatch):
    seed_candidate(store)
    sent_codes = []

    def dispatch(to, subject, html):
        sent_codes.append(re.search(r">(\d{6})</p>", html).group(1))
        return "sent", None

    monkeypatch.setattr("routers.interview.mailer.dispatch", dispatch)
    assert client.post("/api/interview/access", json={"email": "C@x.com"}).status_code == 400
    requested = client.post("/api/interview/access/request-code", json={"email": "C@x.com"})
    assert requested.status_code == 200 and requested.json()["success"] is True and sent_codes
    assert client.post("/api/interview/access/request-code", json={"email": "nobody@x.com"}).json()["success"] is True
    ok = client.post("/api/interview/access", json={"email": "C@x.com", "code": sent_codes[-1]})
    assert ok.json()["success"] is True and ok.json()["resumeId"] == RID and ok.json()["interviewToken"]
    replay = client.post("/api/interview/access", json={"email": "C@x.com", "code": sent_codes[-1]})
    assert replay.status_code == 401
    assert client.post("/api/interview/access", json={}).status_code == 400
    assert store.first("candidate_sessions", {"email": "c@x.com"})["code"] is None


def test_candidate_password_login(client, store):
    seed_candidate(store)
    password = sessions.issue_candidate_password("c@x.com", RID)
    response = client.post("/api/interview/access", json={"email": "c@x.com", "password": password})
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["resumeId"] == RID and response.json()["interviewToken"]
    assert client.post("/api/interview/access", json={"email": "c@x.com", "password": "wrong"}).status_code == 401


def test_access_code_is_rate_limited_and_consumed_after_five_failures(client, store, monkeypatch):
    seed_candidate(store)
    sent_codes = []
    monkeypatch.setattr(
        "routers.interview.mailer.dispatch",
        lambda to, subject, html: (sent_codes.append(re.search(r">(\d{6})</p>", html).group(1)) or "sent", None),
    )
    client.post("/api/interview/access/request-code", json={"email": "c@x.com"})
    for _ in range(5):
        assert client.post("/api/interview/access", json={"email": "c@x.com", "code": "000000"}).status_code == 401
    assert client.post("/api/interview/access", json={"email": "c@x.com", "code": sent_codes[-1]}).status_code == 429
    assert store.first("candidate_sessions", {"email": "c@x.com"})["code"] is None


def test_conclude_marks_session_used_and_blocks_reentry(client, store):
    auth = seed_candidate(store)
    assert client.post(f"/api/interview/{RID}/conclude", headers=auth).json() == {"success": True}
    again = client.post("/api/interview/access/request-code", json={"email": "c@x.com"})
    assert again.status_code == 200
    assert store.first("candidate_sessions", {"email": "c@x.com"})["code"] is None


def test_questions_fall_back_to_defaults_when_qwen_down(client, store, monkeypatch):
    auth = seed_candidate(store, interviewConfig={"interviewType": "technical", "sections": {"overlapping": 2, "gap": 1, "projects": 1, "coding": 1}})
    monkeypatch.setattr(ai, "generate_json", lambda *a, **k: (_ for _ in ()).throw(llm.LlmError("down")))
    res = client.get(f"/api/interview/{RID}/questions", headers=auth)
    assert res.status_code == 200 and len(res.json()) == 5
    assert res.json()[-1].startswith("Coding Challenge:")
    assert client.get(f"/api/interview/{RID}/questions", headers=auth).json() == res.json()
    assert store.count("interview_questions", {"resume_id": RID}) == 5


def test_fallback_claim_verifies_database_update_when_rowcount_is_zero(store, monkeypatch):
    seed_candidate(store, interviewConfig={"interviewType": "technical", "sections": {"overlapping": 2, "gap": 1, "projects": 1, "coding": 1}})
    store.insert("question_sets", {"resume_id": RID, "status": "running", "attempts": 1, "served": None})
    original_update = store.update

    def update_with_missing_rowcount(table, values, where):
        updated = original_update(table, values, where)
        if table == "question_sets" and "served" in values:
            return 0
        return updated

    monkeypatch.setattr(store, "update", update_with_missing_rowcount)

    assert question_queue.serve_fallback(RID)
    assert len(question_queue.existing_questions(RID)) == 5
    row = question_queue._row(RID)
    assert row["status"] == "ready" and row["served"] == "fallback"


def test_question_request_waits_for_slow_ai_generation_before_fallback(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(question_queue, "env_int", lambda _name, _default: 20)
    monkeypatch.setattr(question_queue, "_begin", lambda _resume_id: None)
    monkeypatch.setattr(
        question_queue,
        "_poll",
        lambda _resume_id: ["AI generated question"] if clock["now"] >= 150 else None,
    )
    monkeypatch.setattr(question_queue, "_row", lambda _resume_id: {"status": "running"})
    monkeypatch.setattr(question_queue, "workers_alive", lambda: True)
    monkeypatch.setattr(question_queue.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        question_queue.time,
        "sleep",
        lambda delay: clock.__setitem__("now", clock["now"] + delay),
    )
    monkeypatch.setattr(question_queue, "_finalize", lambda _resume_id: pytest.fail("fallback ran before AI generation finished"))

    assert question_queue.ensure_sync(RID) == ["AI generated question"]


def test_async_question_request_waits_for_active_ai_job(monkeypatch):
    results = iter([None, None, ["AI generated question"]])
    monkeypatch.setattr(question_queue, "_begin", lambda _resume_id: None)
    monkeypatch.setattr(question_queue, "_poll", lambda _resume_id: next(results))
    monkeypatch.setattr(question_queue, "_row", lambda _resume_id: {"status": "running"})
    monkeypatch.setattr(question_queue, "workers_alive", lambda: True)
    monkeypatch.setattr(question_queue, "POLL_S", 0)
    monkeypatch.setattr(question_queue, "_finalize", lambda _resume_id: pytest.fail("active AI job was preempted"))

    assert asyncio.run(question_queue.ensure_async(RID)) == ["AI generated question"]


def test_question_request_uses_fallback_after_ai_generation_fails(monkeypatch):
    monkeypatch.setattr(question_queue, "_begin", lambda _resume_id: None)
    monkeypatch.setattr(question_queue, "_poll", lambda _resume_id: None)
    monkeypatch.setattr(question_queue, "_row", lambda _resume_id: {"status": "failed"})
    monkeypatch.setattr(question_queue, "workers_alive", lambda: True)
    monkeypatch.setattr(question_queue, "_finalize", lambda _resume_id: ["fallback question"])

    assert question_queue.ensure_sync(RID) == ["fallback question"]


def test_question_request_caps_active_generation_wait_at_five_minutes(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(question_queue, "env_int", lambda _name, _default: 600)
    monkeypatch.setattr(question_queue, "_begin", lambda _resume_id: None)
    monkeypatch.setattr(question_queue, "_poll", lambda _resume_id: None)
    monkeypatch.setattr(question_queue, "_row", lambda _resume_id: {"status": "running"})
    monkeypatch.setattr(question_queue, "workers_alive", lambda: True)
    monkeypatch.setattr(question_queue.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        question_queue.time,
        "sleep",
        lambda delay: clock.__setitem__("now", clock["now"] + delay),
    )
    monkeypatch.setattr(question_queue, "_finalize", lambda _resume_id: ["fallback question"])

    assert question_queue.ensure_sync(RID, wait_s=600) == ["fallback question"]
    assert clock["now"] == 300


def test_candidate_questions_use_live_priority_and_technical_questions_are_ai_first(monkeypatch):
    priorities = []

    def fake_complete_json(_prompt, **kwargs):
        priorities.append(kwargs["priority"])
        return [{"question": "Tell me about a time you collaborated with a team."}]

    monkeypatch.setattr(question_gen.llm, "complete_json", fake_complete_json)
    assert question_gen.ai_non_tech({"behavioral": 1, "leadership": 0, "softskills": 0}, "resume facts", "job facts")
    assert priorities == [llm.PRIORITY_LIVE]
    question_gen._ask("background question bank prompt", 100)
    assert priorities[-1] == llm.PRIORITY_BATCH

    verbal_requests = []

    def fake_verbal(overlap, gap, projects, _brief, _jd, _taken):
        verbal_requests.append((overlap, gap, projects))
        return [f"AI verbal question {index}" for index in range(overlap + gap + projects)]

    monkeypatch.setattr(question_gen, "resolve_language", lambda *_args: {"language": "python", "basis": "resume"})
    monkeypatch.setattr(question_gen, "stack_keys", lambda *_args: ["python"])
    monkeypatch.setattr(question_gen, "candidate_brief", lambda _resume: "resume facts")
    monkeypatch.setattr(question_gen, "ai_verbal", fake_verbal)
    monkeypatch.setattr(
        question_gen,
        "ai_coding",
        lambda count, _language, _brief, _jd: [f"AI coding question {index}" for index in range(count)],
    )
    generated = question_gen.build_set(
        {"parsed": {}},
        {"interviewType": "technical", "sections": {"overlapping": 2, "gap": 1, "projects": 1, "coding": 1}},
        "job facts",
        True,
        RID,
    )

    assert verbal_requests == [(2, 1, 1)]
    assert len(generated["items"]) == 5
    assert {item["source"] for item in generated["items"]} == {"ai"}


def test_questions_recover_an_orphaned_fallback_claim(client, store, monkeypatch):
    auth = seed_candidate(store, interviewConfig={"interviewType": "technical", "sections": {"overlapping": 2, "gap": 1, "projects": 1, "coding": 1}})
    store.seed("question_sets", [{"resume_id": RID, "status": "running", "attempts": 1, "served": "fallback"}])
    monkeypatch.setattr(question_queue, "workers_alive", lambda: False)

    response = client.get(f"/api/interview/{RID}/questions", headers=auth)

    assert response.status_code == 200 and len(response.json()) == 5
    row = question_queue._row(RID)
    assert row["status"] == "ready" and row["served"] == "fallback"


def test_questions_serve_generated_output(client, store, monkeypatch):
    auth = seed_candidate(store, interviewConfig={"interviewType": "technical", "sections": {"overlapping": 1, "gap": 1, "projects": 0, "coding": 1}})
    expected = ["Q one?", "Coding Challenge: do x"]
    monkeypatch.setattr(question_queue, "workers_alive", lambda: False)
    monkeypatch.setattr(
        question_gen,
        "build_set",
        lambda *args: {"items": [{"text": question} for question in expected], "language": "python"},
    )
    assert client.get(f"/api/interview/{RID}/questions", headers=auth).json() == expected


def test_questions_fall_back_when_question_queue_registration_fails(client, store, monkeypatch):
    seed_candidate(store, interviewConfig={"interviewType": "technical", "sections": {"overlapping": 2, "gap": 1, "projects": 1, "coding": 1}})
    auth = {"Authorization": f"Bearer {sign_interview_token(RID)}"}
    original_insert = store.insert

    def insert_without_question_set(table, rows):
        if table == "question_sets":
            raise RuntimeError("question queue unavailable")
        return original_insert(table, rows)

    monkeypatch.setattr(store, "insert", insert_without_question_set)
    response = client.get(f"/api/interview/{RID}/questions", headers=auth)
    assert response.status_code == 200 and len(response.json()) == 5
    assert store.count("interview_questions", {"resume_id": RID}) == 5


def test_questions_unknown_resume_and_bad_id(client, store):
    unknown = "22222222-2222-4222-8222-222222222222"
    unknown_auth = {"Authorization": f"Bearer {sign_interview_token(unknown)}"}
    assert client.get(f"/api/interview/{unknown}/questions", headers=unknown_auth).status_code == 404
    assert client.get("/api/interview/not-a-uuid/questions").status_code == 400


def test_submit_answer_heuristics_and_ai(client, store, monkeypatch):
    auth = seed_candidate(store)
    monkeypatch.setattr(ai, "generate_json", lambda *a, **k: {"score": 8, "feedback": "Solid."})
    q = "Explain indexing in databases"
    for idx, ans in enumerate(["", "idk", "A b-tree index speeds lookups by keeping keys ordered so the engine can avoid scanning the table."]):
        res = client.post(f"/api/interview/{RID}/submit_answer", headers=auth, json={"question_index": idx, "question": q, "answer": ans})
        assert res.json() == {"status": "success"}
    scores = [r["mock_score"] for r in sorted(store.select("interview_attempts"), key=lambda r: r["question_index"])]
    assert scores == [0, 1, 8]
    answers = client.get(f"/api/interview/{RID}/answers", headers=auth).json()
    assert len(answers) == 3
    assert client.post(f"/api/interview/{RID}/submit_answer", headers=auth, json={"question_index": "x", "question": q}).status_code == 400


def test_coding_template_scores_one():
    got = interviews.heuristic_evaluation("Coding Challenge: write in JavaScript", "function solution() {\n // write your code here\n return;\n}")
    assert got["score"] == 1
    wrong = interviews.heuristic_evaluation("Coding Challenge: write in JavaScript please", "def solve(x):\n    return x * 2 + 1")
    assert wrong["score"] == 1


def test_proctor_violation_accumulates_and_auto_submits(client, store):
    auth = seed_candidate(store)
    for count in (1, 2, 3):
        res = client.post(f"/api/interview/{RID}/proctor_violation", headers=auth, json={"violationType": "tab_switch", "warningCount": count})
        assert res.status_code == 200
    silent = client.post(f"/api/interview/{RID}/proctor_violation", headers=auth, json={"violationType": "noise", "warningCount": 9, "silent": True}).json()
    proctoring = silent["proctoring"]
    assert proctoring["autoSubmitted"] is True and proctoring["warningCount"] == 3 and proctoring["eventCount"] == 4
    assert client.post(f"/api/interview/{RID}/proctor_violation", headers=auth, json={"warningCount": 1}).status_code == 400


def test_run_code_returns_503_when_qwen_down(client, store, monkeypatch):
    auth = seed_candidate(store)
    monkeypatch.setattr(ai, "generate_json", lambda *a, **k: (_ for _ in ()).throw(llm.LlmError("down")))
    res = client.post(f"/api/interview/{RID}/run_code", headers=auth, json={"question": "q", "language": "python", "code": "print(1)"})
    assert res.status_code == 503 and res.json()["compiles"] is False and "error" in res.json()


def jpeg_b64(size=3000):
    return "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"0" * size).decode()


def test_verify_id_with_face_service(client, store, monkeypatch, tmp_path):
    auth = seed_candidate(store)
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(face, "compare", lambda a, b: {"matched": True, "confidence": 91})
    body = {"idType": "pan", "idImage": jpeg_b64(), "selfieImage": jpeg_b64()}
    res = client.post(f"/api/interview/{RID}/verify_id", headers=auth, json=body)
    assert res.status_code == 200 and res.json()["matched"] is True and res.json()["engine"] == "facenet"
    report = json.loads(store.first("resumes", {"id": RID})["report"])
    assert report["verification"]["status"] == "verified"
    assert client.get(f"/api/interview/{RID}/verification/id", headers=auth).status_code == 200
    assert client.get(f"/api/interview/{RID}/verification/other", headers=auth).status_code == 404
    assert store.first("candidate_sessions", {"email": "c@x.com"})["used"] is False
    started = client.post(f"/api/interview/{RID}/start", headers=auth)
    assert started.status_code == 200 and started.json()["data"]["startedAt"]
    assert store.first("candidate_sessions", {"email": "c@x.com"})["used"] is False


def test_verify_id_engine_down_is_system_error(client, store, monkeypatch, tmp_path):
    auth = seed_candidate(store)
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)

    def boom(a, b):
        raise face.FaceServiceError("down")

    monkeypatch.setattr(face, "compare", boom)
    res = client.post(f"/api/interview/{RID}/verify_id", headers=auth, json={"idType": "pan", "idImage": jpeg_b64(), "selfieImage": jpeg_b64()})
    assert res.json()["isSystemError"] is True and res.json()["matched"] is False


def test_verify_id_rejects_bad_input(client, store):
    auth = seed_candidate(store)
    assert client.post(f"/api/interview/{RID}/verify_id", headers=auth, json={"idType": "passport", "idImage": jpeg_b64(), "selfieImage": jpeg_b64()}).status_code == 400
    assert client.post(f"/api/interview/{RID}/verify_id", headers=auth, json={"idType": "pan", "idImage": jpeg_b64(10), "selfieImage": jpeg_b64(10)}).status_code == 400


def test_descriptor_fallback_match():
    vec = [0.1] * 128
    assert identity.match_descriptors(vec, vec)["matched"] is True
    far = [0.9] * 128
    assert identity.match_descriptors(vec, far)["matched"] is False


def test_video_upload_and_range_streaming(client, store, monkeypatch, tmp_path):
    auth = seed_candidate(store)
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)
    webm = b"\x1a\x45\xdf\xa3" + b"v" * 5000
    res = client.post(f"/api/interview/{RID}/upload_video", headers=auth, files={"video": ("rec.webm", webm, "video/webm")}, data={"duration": "12.5"})
    assert res.status_code == 200 and res.json()["videoUrl"].endswith("/video")
    full = client.get(f"/api/interview/{RID}/video", headers=auth)
    assert full.status_code == 200 and len(full.content) == len(webm)
    part = client.get(f"/api/interview/{RID}/video", headers={**auth, "Range": "bytes=0-9"})
    assert part.status_code == 206 and part.headers["content-range"] == f"bytes 0-9/{len(webm)}"
    bad = client.post(f"/api/interview/{RID}/upload_video", headers=auth, files={"video": ("x.webm", b"not video data", "video/webm")})
    assert bad.status_code == 400
