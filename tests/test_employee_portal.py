import pytest

from core.tokens import sign_token
from services import portal_roster

EMP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture()
def auth(store):
    store.seed("employees", [{"id": EMP, "employee_id": "EMP1", "email": "e@x.com", "full_name": "E", "role": "employee",
                              "product": "One-NDS", "product_qb_eligible": True}])
    return {"Authorization": "Bearer " + sign_token("EMP1")}


def test_catalog_and_subjects(client, auth):
    subjects = client.get("/api/employee/subjects", headers=auth).json()
    assert subjects[0]["id"] == "resource-product-assessment" and subjects[0]["title"] == "One-SDL Question Bank"
    catalog = client.get("/api/employee/catalog", headers=auth).json()
    assert catalog[0]["modules"][0]["topics"][0]["id"] == "resource-product-assessment-test"
    assert len(catalog) == len(subjects)
    assert client.get("/api/employee/catalog").json() == []
    assert client.get("/api/employee/subjects").status_code == 401


def test_ai_quiz_creates_resumes_and_resets(client, auth, store):
    first = client.post("/api/employee/learning/ai-fundamentals/ai-quiz", headers=auth, json={"difficulty": "easy"})
    assert first.status_code == 200 and first.json()["isResume"] is False and first.json()["total_questions"] == 10
    test_id = first.json()["test_id"]
    assert store.count("test_questions", {"test_id": test_id}) == 10
    again = client.post("/api/employee/learning/ai-fundamentals/ai-quiz", headers=auth).json()
    assert again["isResume"] is True and again["test_id"] == test_id
    store.update("tests", {"status": "completed"}, {"id": test_id})
    fresh = client.post("/api/employee/learning/ai-fundamentals/ai-quiz", headers=auth).json()
    assert fresh["isResume"] is False and fresh["test_id"] == test_id and store.count("test_questions", {"test_id": test_id}) == 10
    assert client.post("/api/employee/learning/does-not-exist-zzz/ai-quiz", headers=auth).status_code == 404


def test_ai_quiz_blocked_for_assessment_only(client, auth, store):
    store.update("employees", {"assessment_only": True}, {"employee_id": "EMP1"})
    assert client.post("/api/employee/learning/ai-fundamentals/ai-quiz", headers=auth).status_code == 403


def test_assigned_test_states(client, auth, store):
    empty = client.get("/api/employee/assigned-test", headers=auth).json()
    assert empty == {"active_test": None, "completed_test": None}
    store.seed("tests", [{"id": "t1", "employee_id": EMP, "employee_code": "EMP1", "topic_id": "resource-product-assessment", "subject_id": "s",
                          "topic_title": "Prod", "total_questions": 25, "status": "pending", "created_at": "2026-10-01T00:00:00Z"},
                         {"id": "t2", "employee_id": EMP, "employee_code": "EMP1", "topic_id": "resource-product-assessment-history:x", "subject_id": "s",
                          "topic_title": "Prod (previous)", "total_questions": 25, "status": "completed", "score_correct": 20,
                          "completed_at": "2026-09-01T00:00:00Z"}])
    res = client.get("/api/employee/assigned-test", headers=auth).json()
    assert res["active_test"]["test_id"] == "t1" and res["completed_test"]["score_percent"] == 80 and res["completed_test"]["recording_missing"] is True
    assert res["completed_test"]["topic_title"] == "Prod"


def test_results_and_analytics(client, auth, store):
    for i, (pct, when) in enumerate([(80, "2026-09-01T10:00:00Z"), (60, "2026-09-09T10:00:00Z")]):
        tid = f"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbb0{i}"
        qid = f"cccccccc-cccc-4ccc-8ccc-cccccccccc0{i}"
        store.seed("tests", [{"id": tid, "employee_id": EMP, "employee_code": "EMP1", "topic_id": f"topic{i}", "subject_id": "sub1",
                              "topic_title": f"Topic {i}", "subject_title": "Sub", "difficulty": "medium", "total_questions": 10,
                              "status": "completed", "score_correct": pct // 10, "score_percent": pct, "completed_at": when}])
        store.seed("test_attempts", [{"id": f"a{i}", "test_id": tid, "employee_id": EMP, "question_id": qid, "is_correct": True}])
    results = client.get("/api/employee/results", headers=auth).json()
    assert [r["accuracy_pct"] for r in results] == [60, 80]
    assert client.get("/api/employee/results?employee_id=OTHER", headers=auth).status_code == 403
    data = client.get("/api/employee/analytics", headers=auth).json()
    assert data["total_tests_taken"] == 2 and data["total_learning_hours"] == 1 and data["average_score"] == 70
    assert [h["score"] for h in data["score_history"]] == [80, 60]
    assert len(data["weekly_activity"]) == 2 and data["skill_level"] == "intermediate"
    assert data["strongest_subject"]["subject_title"] == "Topic 0"


def test_archived_assessments_are_visible_but_only_completed_scores_are_averaged(client, auth, store):
    history_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
    incomplete_history_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
    current_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3"
    store.seed("tests", [
        {"id": history_id, "employee_id": EMP, "employee_code": "EMP1",
         "topic_id": "resource-product-assessment-history:old", "subject_id": "resource-subject",
         "topic_title": "TPM Upskilling (previous)", "subject_title": "Product Assessment", "difficulty": "medium",
         "total_questions": 25, "status": "completed", "score_correct": 20, "score_percent": 80,
         "completed_at": "2026-09-01T10:00:00Z"},
        {"id": incomplete_history_id, "employee_id": EMP, "employee_code": "EMP1",
         "topic_id": "resource-product-assessment-history:incomplete", "subject_id": "resource-subject",
         "topic_title": "TPM Upskilling (previous)", "subject_title": "Product Assessment", "difficulty": "medium",
         "total_questions": 25, "status": "pending", "created_at": "2026-08-01T10:00:00Z"},
        {"id": current_id, "employee_id": EMP, "employee_code": "EMP1",
         "topic_id": "resource-product-assessment", "subject_id": "resource-subject",
         "topic_title": "CSD", "subject_title": "Product Assessment", "difficulty": "medium",
         "total_questions": 25, "status": "pending"},
    ])

    results = client.get("/api/employee/results", headers=auth).json()
    assert {result["id"] for result in results} == {history_id, incomplete_history_id}
    completed_history = next(result for result in results if result["id"] == history_id)
    assert completed_history["topic_title"] == "TPM Upskilling"
    incomplete = next(result for result in results if result["id"] == incomplete_history_id)
    assert incomplete["is_history"] is True
    assert incomplete["status"] == "pending"
    assert incomplete["accuracy_pct"] is None

    analytics = client.get("/api/employee/analytics", headers=auth).json()
    assert analytics["total_tests_taken"] == 1
    assert analytics["average_score"] == 80

    store.update("tests", {
        "status": "completed", "score_correct": 15, "score_percent": 60,
        "completed_at": "2026-10-01T10:00:00Z",
    }, {"id": current_id})
    analytics = client.get("/api/employee/analytics", headers=auth).json()
    assert analytics["total_tests_taken"] == 2
    assert analytics["average_score"] == 70


def test_admin_portal_keeps_history_in_employee_test_list():
    profile = [{
        "employee_id": "EMP1", "full_name": "E", "role": "employee", "product": "CSD",
        "emp_status": "Confirmed", "remarks": "", "assigned_questions": [], "assigned_question_count": 25,
    }]
    results = [
        {"id": "current", "employeeId": "EMP1", "topicId": "resource-product-assessment", "status": "pending",
         "totalQuestions": 25, "answeredCount": 0, "createdAt": "2026-10-01T10:00:00Z"},
        {"id": "history", "employeeId": "EMP1", "topicId": "resource-product-assessment-history:old",
         "topicTitle": "TPM Upskilling (previous)", "status": "completed", "score": 20, "correctCount": 20,
         "totalQuestions": 25, "answeredCount": 25, "createdAt": "2026-09-01T10:00:00Z",
         "completedAt": "2026-09-01T10:00:00Z"},
    ]

    employee = portal_roster.merge_portal_data(profile, results, {})[0]
    assert employee["test_id"] == "current"
    assert [test["id"] for test in employee["tests"]] == ["history", "current"]
    history = employee["tests"][0]
    assert history["topicTitle"] == "TPM Upskilling"
    assert history["status"] == "completed"
