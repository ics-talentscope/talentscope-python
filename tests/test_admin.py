import json
import re

import pytest

from services import mailer

RID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture()
def admin(client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "boss@infinite.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "AdminPw1!")
    token = client.post("/api/admin/auth/login", json={"email": "boss@infinite.com", "password": "AdminPw1!"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def seed(store, rm="rm@infinite.com"):
    store.seed("resumes", [{"id": RID, "filename": "a.pdf", "parsed": json.dumps({"personal": {"email": "c@x.com", "fullName": "Cand Idate"}}),
                            "report": json.dumps({"rmEmail": rm, "videoUrl": "/v", "proctoring": {"warningCount": 1}}),
                            "analysis": "{}", "enhanced": "{}", "created_at": "2026-09-01T00:00:00Z"}])
    store.seed("candidate_sessions", [{"id": "s1", "email": "c@x.com", "resume_id": RID, "used": True, "code": None}])
    store.seed("interview_attempts", [{"id": "a1", "resume_id": RID, "question_index": -1, "question_text": "Intro", "mock_score": 7,
                                       "mock_feedback": "ok", "candidate_answer": "hi"}])


def test_all_admin_routes_require_token(client):
    for method, path in [("get", "/api/admin/resumes"), ("delete", "/api/admin/resumes"), ("get", "/api/admin/emails"),
                         ("get", "/api/admin/logs"), ("get", "/api/admin/reset_logs"), ("get", "/api/admin/sync"),
                         ("post", "/api/admin/portal_settings"), ("get", f"/api/admin/resumes/{RID}")]:
        assert getattr(client, method)(path).status_code == 401, path


def test_list_filters_by_rm_and_enriches(client, store, admin):
    seed(store)
    res = client.get("/api/admin/resumes", headers=admin).json()
    item = res["resumes"][0]
    assert res["count"] == 1 and item["isConcluded"] is True and item["interview_attempts"][0]["question_number"] == "Intro"
    mine = client.get("/api/admin/resumes?email=rm@infinite.com", headers=admin).json()
    other = client.get("/api/admin/resumes?email=someone@infinite.com", headers=admin).json()
    assert mine["count"] == 1 and other["count"] == 0


def test_get_delete_and_download(client, store, admin, tmp_path, monkeypatch):
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)
    seed(store)
    assert client.get(f"/api/admin/resumes/{RID}", headers=admin).json()["resume"]["id"] == RID
    assert client.get(f"/api/admin/resumes/{RID}/download", headers=admin).status_code == 404
    (tmp_path / "resumes").mkdir(exist_ok=True)
    (tmp_path / "resumes" / f"{RID}-a.pdf").write_bytes(b"%PDF-1.4 x")
    dl = client.get(f"/api/admin/resumes/{RID}/download", headers=admin)
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/pdf"
    assert client.request("DELETE", "/api/admin/resumes", headers=admin, json={"ids": ["nope"]}).status_code == 400
    assert client.delete(f"/api/admin/resumes/{RID}", headers=admin).json() == {"success": True}
    assert store.count("resumes") == 0 and store.count("interview_attempts") == 0 and store.count("candidate_sessions") == 0
    assert client.get(f"/api/admin/resumes/{RID}", headers=admin).status_code == 404


def test_suitability_override_and_validation(client, store, admin):
    seed(store)
    assert client.post(f"/api/admin/resumes/{RID}/suitability", headers=admin, json={"suitability": "maybe"}).status_code == 400
    ok = client.post(f"/api/admin/resumes/{RID}/suitability", headers=admin, json={"suitability": "suitable", "activeJdId": "jd1"})
    assert ok.json()["suitability"] == "suitable"
    report = json.loads(store.first("resumes", {"id": RID})["report"])
    assert report["suitabilityOverridden"] is True and report["jdId"] == "jd1" and report["rmEmail"] == "rm@infinite.com"


def test_reset_clears_candidate_state(client, store, admin):
    seed(store)
    assert client.post(f"/api/admin/resumes/{RID}/reset", headers=admin, json={"adminEmail": "boss@infinite.com"}).json() == {"success": True}
    report = json.loads(store.first("resumes", {"id": RID})["report"])
    assert "videoUrl" not in report and "proctoring" not in report and report["rmEmail"] == "rm@infinite.com"
    assert store.count("interview_attempts") == 0
    assert store.first("candidate_sessions", {"email": "c@x.com"})["used"] is False
    assert store.count("reset_logs") == 1
    invalid_email = client.post("/api/admin/resumes/reset_by_email", headers=admin, json={"email": "invalid"})
    assert invalid_email.status_code == 400 and "valid candidate email" in invalid_email.json()["error"]
    assert client.post("/api/admin/resumes/reset_by_email", headers=admin, json={"email": "ghost@x.com"}).status_code == 404
    missing = client.post("/api/admin/resumes/reset_by_email", headers=admin, json={"email": "ghost@x.com"})
    assert missing.json()["error"] == "Candidate not found. Check the email address and try again."
    assert client.post("/api/admin/resumes/reset_by_email", headers=admin, json={"email": "c@x.com"}).status_code == 200


def test_invite_records_email_and_uses_app_url(client, store, admin, monkeypatch, tmp_path):
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("NEXT_PUBLIC_APP_URL", "http://localhost:3000")
    seed(store)
    res = client.post(f"/api/admin/resumes/{RID}/invite", headers=admin,
                      json={"interviewConfig": {"interviewType": "both", "sections": {"overlapping": 1}}}).json()
    assert res["success"] and res["status"] == "simulated" and res["fullName"] == "Cand Idate"
    mail = store.select("simulated_emails")[0]
    assert "http://localhost:3000" in mail["body"] and "vercel.app" not in mail["body"] and "Technical & Behavioral" in mail["subject"]
    assert ">Start Assessment</a>" in mail["body"] and "Temporary password:" in mail["body"]
    password = re.search(r"Temporary password: <strong>([^<]+)</strong>", mail["body"]).group(1)
    assert len(password) >= 12
    assert store.first("candidate_sessions", {"email": "c@x.com"})["password_hash"] != password
    login = client.post("/api/interview/access", json={"email": "c@x.com", "password": password})
    assert login.status_code == 200 and login.json()["resumeId"] == RID
    assert store.first("candidate_sessions", {"email": "c@x.com"})["used"] is False
    emails = client.get("/api/admin/emails", headers=admin).json()["emails"]
    assert emails[0]["to"] == "c@x.com"
    deleted = client.request("DELETE", "/api/admin/emails", headers=admin, json={"ids": [emails[0]["id"]]}).json()
    assert deleted["deletedCount"] == 1 and deleted["emails"] == []


def test_invite_sends_real_mail_when_smtp_configured(client, store, admin, monkeypatch, tmp_path):
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("SMTP_USER", "u@x.com")
    monkeypatch.setenv("SMTP_PASS", "pw")
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, html: sent.append(to))
    seed(store)
    assert client.post(f"/api/admin/resumes/{RID}/invite", headers=admin, json={}).json()["status"] == "sent"
    assert sent == ["c@x.com"]
    monkeypatch.setattr(mailer, "send", lambda *a: (_ for _ in ()).throw(RuntimeError("smtp down")))
    assert client.post(f"/api/admin/resumes/{RID}/invite", headers=admin, json={}).json()["status"] == "failed_real_fallback_simulated"


def test_portal_settings_sync_and_logs(client, store, admin, monkeypatch, tmp_path):
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("services.structured_log.UPLOAD_DIR", tmp_path)
    assert client.get("/api/portal_settings").json() == {"showSystemLogsViewer": True}
    upd = client.post("/api/admin/portal_settings", headers=admin, json={"settings": {"showSystemLogsViewer": False}}).json()
    assert upd["settings"]["showSystemLogsViewer"] is False
    assert client.get("/api/portal_settings").json() == {"showSystemLogsViewer": False}
    assert client.get("/api/admin/sync", headers=admin).json()["revision"] == 0
    logs = client.get("/api/admin/logs?module=employee", headers=admin).json()["logs"]
    assert logs[0]["action"] == "UPDATE_PORTAL_SETTINGS"
    assert client.get("/api/admin/logs?module=employee&download=true", headers=admin).status_code == 200
    assert client.delete("/api/admin/logs", headers=admin).json() == {"success": True}
    cleared_logs = client.get("/api/admin/logs", headers=admin).json()["logs"]
    actions = {entry["action"] for entry in cleared_logs}
    assert "UPDATE_PORTAL_SETTINGS" not in actions
    assert "CLEAR_SYSTEM_LOGS" in actions


def test_admin_employee_test_attempts_and_video(client, store, admin, monkeypatch, tmp_path):
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)
    tid, q0, q1 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "cccccccc-cccc-4ccc-8ccc-cccccccccc00", "cccccccc-cccc-4ccc-8ccc-cccccccccc01"
    store.seed("tests", [{"id": tid, "status": "completed"}])
    store.seed("test_questions", [{"id": q1, "test_id": tid, "question_index": 1, "question_text": "B?", "options": ["x", "y"]},
                                  {"id": q0, "test_id": tid, "question_index": 0, "question_text": "A?", "options": ["x", "y"]}])
    store.seed("test_attempts", [{"id": "a", "test_id": tid, "question_id": q0, "selected_option_index": 1, "is_correct": True}])
    res = client.get(f"/api/admin/employee-tests/{tid}/attempts", headers=admin).json()
    assert [q["question_text"] for q in res["questions"]] == ["A?"]
    assert res["questions"][0]["selected_option_text"] == "B) y"
    assert client.get("/api/admin/employee-tests/cccccccc-cccc-4ccc-8ccc-cccccccccc99/attempts", headers=admin).status_code == 404
    assert client.get(f"/api/admin/employee-tests/{tid}/video", headers=admin).status_code == 404
    from services import test_video

    webm = b"\x1a\x45\xdf\xa3" + b"\x1f\x43\xb6\x75" + b"v" * 6000
    assert test_video.save(tid, webm)
    token = admin["Authorization"].split()[1]
    inline = client.get(f"/api/admin/employee-tests/{tid}/video?inline=1&filename=x.webm&token={token}")
    assert inline.status_code == 200 and 'inline; filename="x.webm"' in inline.headers["content-disposition"]
    part = client.get(f"/api/admin/employee-tests/{tid}/video", headers={**admin, "Range": "bytes=0-4"})
    assert part.status_code == 206 and len(part.content) == 5
    assert client.get(f"/api/admin/employee-tests/{tid}/video").status_code == 401
