import time
from datetime import datetime, timedelta, timezone

import pytest

from core.tokens import sign_token
from services import employee_tests as et, question_bank

EMP_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
Q = [f"cccccccc-cccc-4ccc-8ccc-cccccccccc0{i}" for i in range(3)]


@pytest.fixture()
def world(store):
    store.seed("employees", [{"id": EMP_UUID, "employee_id": "EMP1", "email": "e1@x.com", "full_name": "E One", "role": "employee",
                              "department": "eng", "is_first_login": False}])
    store.seed("tests", [{"id": TEST_ID, "employee_id": EMP_UUID, "employee_code": "EMP1", "topic_id": "t1", "subject_id": "s1",
                          "topic_title": "One NDS Basics", "subject_title": "Sub", "difficulty": "medium", "total_questions": 3,
                          "time_limit_seconds": 1800, "status": "pending", "current_question_index": 0}])
    store.seed("test_questions", [{"id": Q[i], "test_id": TEST_ID, "question_index": i, "question_text": f"Q{i}",
                                   "options": ["a", "b", "c", "d"], "correct_option_index": 1, "topic_id": "t1"} for i in range(3)])
    return {"Authorization": "Bearer " + sign_token("EMP1")}


def test_requires_auth(client, store):
    assert client.get("/api/employee/tests").json() == []
    assert client.get(f"/api/employee/tests/{TEST_ID}").status_code == 401


def test_list_and_get(client, world):
    listed = client.get("/api/employee/tests", headers=world).json()
    assert len(listed) == 1 and listed[0]["topic_title"] == "ONE SDL Basics"
    got = client.get(f"/api/employee/tests/{TEST_ID}", headers=world).json()
    assert len(got["questions"]) == 3 and got["has_recording"] is False and got["attempts"] == []
    assert all("correct_option_index" not in question and "explanation" not in question for question in got["questions"])
    started = client.post(f"/api/employee/tests/{TEST_ID}/start", headers=world).json()
    assert all("correct_option_index" not in question for question in started["questions"])


def test_other_employees_test_is_hidden(client, world, store):
    store.seed("employees", [{"id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "employee_id": "EMP2", "email": "e2@x.com", "full_name": "E Two"}])
    other = {"Authorization": "Bearer " + sign_token("EMP2")}
    assert client.get(f"/api/employee/tests/{TEST_ID}", headers=other).status_code == 404
    assert client.post(f"/api/employee/tests/{TEST_ID}/submit", headers=other, json={"answers": [{"question_id": Q[0], "selected_index": 1}]}).status_code == 404


def test_patch_start_then_submit_scores(client, world, store):
    started_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    started = client.patch(f"/api/employee/tests/{TEST_ID}", headers=world,
                           json={"status": "in_progress", "started_at": started_at, "camera_granted": True})
    assert started.status_code == 200
    assert store.first("tests", {"id": TEST_ID})["proctoring"]["sessionStartedAt"]
    assert client.patch(f"/api/employee/tests/{TEST_ID}", headers=world, json={"status": "pending"}).status_code == 409
    answers = [{"question_id": Q[0], "selected_index": 1, "time_seconds": 5}, {"question_id": Q[1], "selected_index": 0},
               {"question_id": "ffffffff-ffff-4fff-8fff-ffffffffffff", "selected_index": 1}]
    res = client.post(f"/api/employee/tests/{TEST_ID}/submit", headers=world, json={"answers": answers}).json()
    assert res["correct"] == 1 and res["total"] == 3 and res["accuracy"] == 33
    assert {question["id"] for question in res["questions"]} == {Q[0], Q[1]}
    assert Q[2] not in {question["id"] for question in res["questions"]}
    assert all("correct_option_index" in question for question in res["questions"])
    row = store.first("tests", {"id": TEST_ID})
    assert row["status"] == "completed" and row["score_percent"] == 33
    assert store.count("test_attempts", {"test_id": TEST_ID}) == 2
    review = client.get(f"/api/employee/tests/{TEST_ID}", headers=world).json()
    assert {question["id"] for question in review["questions"]} == {Q[0], Q[1]}
    assert Q[2] not in {question["id"] for question in review["questions"]}
    again = client.post(f"/api/employee/tests/{TEST_ID}/submit", headers=world, json={"answers": answers}).json()
    assert again["alreadyCompleted"] is True
    assert {question["id"] for question in again["questions"]} == {Q[0], Q[1]}
    assert client.patch(f"/api/employee/tests/{TEST_ID}", headers=world, json={"current_question_index": 1}).status_code == 409


def test_submit_before_start_rejected(client, world):
    res = client.post(f"/api/employee/tests/{TEST_ID}/submit", headers=world, json={"answers": [{"question_id": Q[0], "selected_index": 1}]})
    assert res.status_code == 400
    assert client.post(f"/api/employee/tests/{TEST_ID}/submit", headers=world, json={"answers": []}).status_code == 400


def test_proctoring_cooldown_and_auto_submit(client, world, store, monkeypatch):
    started_at = datetime.now(timezone.utc).isoformat()
    store.update("tests", {"status": "in_progress", "started_at": started_at}, {"id": TEST_ID})
    first = client.post(f"/api/employee/tests/{TEST_ID}/proctor_violation", headers=world, json={"violationType": "Tab Switch"}).json()
    assert first["proctoring"]["warningCount"] == 1 and first["proctoring"]["violations"][0]["category"] == "browser"
    burst = client.post(f"/api/employee/tests/{TEST_ID}/proctor_violation", headers=world, json={"violationType": "Tab Switch"}).json()
    assert burst["proctoring"]["warningCount"] == 1
    monkeypatch.setattr(et, "VIOLATION_COOLDOWN_MS", 0)
    client.post(f"/api/employee/tests/{TEST_ID}/proctor_violation", headers=world, json={"violationType": "Tab Switch"})
    third = client.post(f"/api/employee/tests/{TEST_ID}/proctor_violation", headers=world, json={"violationType": "Phone detected"}).json()
    assert third["autoSubmit"] is True and third["proctoring"]["warningCount"] == 3


def test_reset_regenerates_questions(client, world, store):
    res = client.delete(f"/api/employee/tests/{TEST_ID}", headers=world)
    assert res.status_code == 200
    qs = store.select("test_questions", {"test_id": TEST_ID})
    assert len(qs) == 10 and store.first("tests", {"id": TEST_ID})["status"] == "pending"


def test_product_assessment_reset_archives_history(client, world, store, monkeypatch):
    class ReverseRandom:
        def shuffle(self, values):
            values.reverse()

    monkeypatch.setattr(et.random, "SystemRandom", ReverseRandom)
    store.update("tests", {"topic_id": "resource-product-assessment", "status": "completed", "score_percent": 80}, {"id": TEST_ID})
    store.update("employees", {"product_qb_eligible": True}, {"employee_id": "EMP1"})
    res = client.delete(f"/api/employee/tests/{TEST_ID}", headers=world).json()
    assert res == {"success": True, "archived": True}
    topics = [t["topic_id"] for t in store.select("tests")]
    assert any(t.startswith("resource-product-assessment-history:") for t in topics)
    assert store.first("tests", {"id": TEST_ID})["status"] == "pending"
    shuffled = et.questions_for(TEST_ID)
    assert [question["id"] for question in shuffled] == [Q[2], Q[1], Q[0]]
    assert shuffled[0]["options"] == ["d", "c", "b", "a"]
    assert shuffled[0]["correct_option_index"] == 2
    history = [t for t in store.select("tests") if t["topic_id"].startswith("resource-product-assessment-history")][0]
    assert len(store.select("test_questions", {"test_id": history["id"]})) == 3
    assert client.delete(f"/api/employee/tests/{history['id']}", headers=world).status_code == 403


def test_video_upload_progress_and_chunks(client, world, store, monkeypatch, tmp_path):
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)
    store.update("tests", {"status": "in_progress"}, {"id": TEST_ID})
    webm = b"\x1a\x45\xdf\xa3" + b"\x1f\x43\xb6\x75" + b"v" * 6000
    prog = client.post(f"/api/employee/tests/{TEST_ID}/upload_video/progress", headers=world, files={"video": ("p.webm", webm, "video/webm")})
    assert prog.status_code == 200 and prog.json()["videoUrl"].endswith("/video")
    assert store.first("tests", {"id": TEST_ID})["proctoring"]["videoUploaded"] is True
    half = len(webm) // 2
    r1 = client.post(f"/api/employee/tests/{TEST_ID}/upload_video/chunk", headers=world,
                     files={"chunk": ("c", webm[:half])}, data={"chunkIndex": "0", "totalChunks": "2"})
    assert r1.json()["complete"] is False
    r2 = client.post(f"/api/employee/tests/{TEST_ID}/upload_video/chunk", headers=world,
                     files={"chunk": ("c", webm[half:])}, data={"chunkIndex": "1", "totalChunks": "2"})
    assert r2.json()["complete"] is True and r2.json()["bytes"] == len(webm)
    assert client.get(f"/api/employee/tests/{TEST_ID}", headers=world).json()["has_recording"] is True
    bad = client.post(f"/api/employee/tests/{TEST_ID}/upload_video/chunk", headers=world,
                      files={"chunk": ("c", b"x")}, data={"chunkIndex": "5", "totalChunks": "2"})
    assert bad.status_code == 400


def test_question_bank_shape():
    bank = question_bank.build_databank("AI", "Expert Systems", "medium")
    assert len(bank) == 100
    for q in bank:
        assert len(q["options"]) == 4 and 0 <= q["correctIndex"] < 4
