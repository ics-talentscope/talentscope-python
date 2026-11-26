from datetime import datetime, timezone
import uuid

import pytest

from core import tenancy
from core.tokens import sign_token
from services import (
    admin_accounts,
    employee_portal_admin,
    employee_tests,
    portal,
    slalom_bootcamp as bootcamp,
    topic_sources,
)

BASE = "/api/admin/slalom/bootcamp"


@pytest.fixture(autouse=True)
def _clear_content_caches():
    portal.invalidate_content_cache()
    topic_sources.invalidate_content_cache()
    yield
    portal.invalidate_content_cache()
    topic_sources.invalidate_content_cache()


def _headers(client, email: str, password: str) -> dict:
    response = client.post("/api/admin/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _seed_employee_and_bank(store):
    with tenancy.client_context("slalom"):
        employee_portal_admin.save_question_bank("Bootcamp Bank", 5, [{
            "question_text": f"Bootcamp question {index}",
            "options": ["Correct", "Wrong A", "Wrong B", "Wrong C"],
            "correct_option_index": 0,
            "explanation": "",
            "difficulty": "medium",
        } for index in range(5)])
        store.insert("employees", {
            "id": str(uuid.uuid4()),
            "employee_id": "BOOT1",
            "full_name": "Bootcamp Employee",
            "email": "boot1@example.com",
            "designation": "Engineer",
            "department": "Engineering",
            "role": "employee",
            "product": "Bootcamp Bank",
            "product_qb_eligible": True,
            "assessment_only": True,
            "is_first_login": False,
        })


def _create_assessment(client, headers, name="Security Foundations") -> dict:
    # Mirrors assessmentPayload() in SlalomBootcampManager.tsx.
    response = client.post(BASE + "/assessments", headers=headers, json={
        "name": name,
        "title": name,
        "description": "Slalom bootcamp security assessment",
        "bankKey": "Bootcamp Bank",
        "bank_key": "Bootcamp Bank",
        "product": "Bootcamp Bank",
        "questionCount": 3,
        "question_count": 3,
        "durationSeconds": 600,
        "duration_seconds": 600,
        "durationMinutes": 10,
        "topics": ["Security"],
        "difficultyMix": {"easy": 0, "medium": 3, "hard": 0},
        "randomizeQuestions": True,
        "randomizeOptions": True,
        "status": "published",
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_bootcamp_assessments_have_independent_employee_tests_and_attempts(client, store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")
    _seed_employee_and_bank(store)
    cohort = _create_cohort(client, admin)

    first = _create_assessment(client, admin)
    first_assignment = _assign_bootcamp(client, admin, first["id"], cohort["id"])["assignments"][0]
    employee_headers = {"Authorization": f"Bearer {sign_token('BOOT1', client='slalom')}"}
    first_started = client.patch(
        f"/api/employee/tests/{first_assignment['testId']}",
        headers=employee_headers,
        json={
            "status": "in_progress",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "camera_granted": True,
        },
    )
    assert first_started.status_code == 200, first_started.text
    with tenancy.client_context("slalom"):
        first_questions = store.select(
            "test_questions", {"test_id": first_assignment["testId"]},
            ["id", "correct_option_index"],
        )
    submitted = client.post(
        f"/api/employee/tests/{first_assignment['testId']}/submit",
        headers=employee_headers,
        json={"answers": [
            {"question_id": question["id"], "selected_index": question["correct_option_index"]}
            for question in first_questions
        ]},
    )
    assert submitted.status_code == 200, submitted.text

    second = _create_assessment(client, admin, "Cloud Foundations")
    second_assignment = _assign_bootcamp(client, admin, second["id"], cohort["id"])["assignments"][0]
    assert second_assignment["testId"] != first_assignment["testId"]
    with tenancy.client_context("slalom"):
        first_test = store.first("tests", {"id": first_assignment["testId"]})
        second_test = store.first("tests", {"id": second_assignment["testId"]})
        assert first_test["topic_id"] != second_test["topic_id"]

    second_started = client.patch(
        f"/api/employee/tests/{second_assignment['testId']}",
        headers=employee_headers,
        json={
            "status": "in_progress",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "camera_granted": True,
        },
    )
    assert second_started.status_code == 200, second_started.text
    active = client.get("/api/employee/assigned-test", headers=employee_headers)
    assert active.json()["active_test"]["test_id"] == second_assignment["testId"]


def test_bootcamp_resource_deletes_preserve_delivery_tests(client, store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")
    _seed_employee_and_bank(store)
    assessment = _create_assessment(client, admin)
    cohort = _create_cohort(client, admin)
    assignment = _assign_bootcamp(client, admin, assessment["id"], cohort["id"])["assignments"][0]

    read_assessment = client.get(BASE + f"/assessments/{assessment['id']}", headers=admin)
    assert read_assessment.status_code == 200
    patched_assessment = client.patch(
        BASE + f"/assessments/{assessment['id']}",
        headers=admin,
        json={"description": "Updated bootcamp assessment"},
    )
    assert patched_assessment.status_code == 200
    assert patched_assessment.json()["description"] == "Updated bootcamp assessment"
    read_cohort = client.get(BASE + f"/cohorts/{cohort['id']}", headers=admin)
    assert read_cohort.status_code == 200
    patched_cohort = client.patch(
        BASE + f"/cohorts/{cohort['id']}",
        headers=admin,
        json={"description": "Updated bootcamp cohort"},
    )
    assert patched_cohort.status_code == 200
    assert patched_cohort.json()["description"] == "Updated bootcamp cohort"

    second_assessment = _create_assessment(client, admin, "Second Security Assessment")
    second_assignment = _assign_bootcamp(
        client, admin, second_assessment["id"], cohort["id"],
    )["assignments"][0]

    deleted_assessment = client.delete(
        BASE + f"/assessments/{assessment['id']}", headers=admin,
    )
    assert deleted_assessment.status_code == 200, deleted_assessment.text
    with tenancy.client_context("slalom"):
        assert store.first("bootcamp_assessments", {"id": assessment["id"]}) is None
        assert store.count("bootcamp_assignments", {"assessment_id": assessment["id"]}) == 0
        assert store.first("tests", {"id": assignment["testId"]}) is not None

    deleted_cohort = client.delete(BASE + f"/cohorts/{cohort['id']}", headers=admin)
    assert deleted_cohort.status_code == 200, deleted_cohort.text
    with tenancy.client_context("slalom"):
        assert store.first("bootcamp_cohorts", {"id": cohort["id"]}) is None
        assert store.count("bootcamp_cohort_members", {"cohort_id": cohort["id"]}) == 0
        assert store.count("bootcamp_assignments", {"cohort_id": cohort["id"]}) == 0
        assert store.first("tests", {"id": second_assignment["testId"]}) is not None


def _create_cohort(client, headers, name="Cohort A") -> dict:
    response = client.post(BASE + "/cohorts", headers=headers, json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


def _assign_bootcamp(client, headers, assessment_id: str, cohort_id: str) -> dict:
    members = client.post(
        BASE + f"/cohorts/{cohort_id}/members/bulk",
        headers=headers,
        json={"employeeIds": ["BOOT1"]},
    )
    assert members.status_code == 200, members.text
    assigned = client.post(
        BASE + f"/assessments/{assessment_id}/assign",
        headers=headers,
        json={"cohortId": cohort_id},
    )
    assert assigned.status_code == 200, assigned.text
    return assigned.json()


def test_slalom_bootcamp_access_requires_slalom_client_and_permission(client, store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()

    slalom_admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")
    assert client.get(BASE + "/assessments", headers=slalom_admin).status_code == 200

    saved, error, _ = admin_accounts.save_named(
        "bootcamp-admin@slalom.com", {"bootcampManagement": True}, "SlalomNamed123!",
    )
    assert saved, error
    slalom_named = _headers(client, "bootcamp-admin@slalom.com", "SlalomNamed123!")
    assert client.get(BASE + "/assessments", headers=slalom_named).status_code == 200

    saved, error, _ = admin_accounts.save_named(
        "no-bootcamp@slalom.com", {}, "SlalomNoBoot123!",
    )
    assert saved, error
    slalom_no_permission = _headers(client, "no-bootcamp@slalom.com", "SlalomNoBoot123!")
    assert client.get(BASE + "/assessments", headers=slalom_no_permission).status_code == 403

    saved, error, _ = admin_accounts.save_named(
        "infinite-bootcamp@infinite.com", {"bootcampManagement": True}, "InfiniteBoot123!",
    )
    assert saved, error
    infinite_admin = _headers(client, "infinite-bootcamp@infinite.com", "InfiniteBoot123!")
    assert admin_accounts.has_permission("infinite-bootcamp@infinite.com", "bootcampManagement") is True
    assert admin_accounts.can_manage_slalom_bootcamp("infinite-bootcamp@infinite.com") is False
    assert client.get(BASE + "/assessments", headers=infinite_admin).status_code == 403
    assert client.get(BASE + "/assessments").status_code == 401


def test_slalom_bootcamp_config_is_tenant_scoped(client, store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    slalom_admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")

    assessment = _create_assessment(client, slalom_admin)
    with tenancy.client_context("slalom"):
        assert [item["title"] for item in bootcamp.list_assessments()] == ["Security Foundations"]
    with tenancy.client_context("infinite"):
        assert bootcamp.list_assessments() == []
        assert store.first("bootcamp_assessments", {"id": assessment["id"]}) is None
    with tenancy.unscoped():
        assert store.first("bootcamp_assessments", {"id": assessment["id"]})["client_id"] == "slalom"


def test_slalom_assignment_uses_assessment_overrides_in_delivery_tables(client, store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    slalom_admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")
    _seed_employee_and_bank(store)

    assessment = _create_assessment(client, slalom_admin)
    cohort = _create_cohort(client, slalom_admin)
    result = _assign_bootcamp(client, slalom_admin, assessment["id"], cohort["id"])

    assert result["assigned"] == 1
    assignment = result["assignments"][0]
    assert assignment["assessmentId"] == assessment["id"]
    assert assignment["cohortId"] == cohort["id"]
    assert assignment["testId"]

    with tenancy.client_context("slalom"):
        test = store.first("tests", {"id": assignment["testId"]})
        questions = store.select("test_questions", {"test_id": assignment["testId"]})
        assert test["topic_id"] == f"resource-product-assessment:{assessment['id']}"
        assert test["topic_title"] == "Security Foundations"
        assert test["subject_title"] == "Bootcamp Assessment"
        assert test["total_questions"] == 3
        assert test["time_limit_seconds"] == 600
        assert len(questions) == 3
        assert {row["question_text"] for row in questions} <= {
            f"Bootcamp question {index}" for index in range(5)
        }
        assert store.count("bootcamp_assignments", {"assessment_id": assessment["id"]}) == 1
    with tenancy.client_context("infinite"):
        assert store.first("tests", {"id": assignment["testId"]}) is None
        assert store.count("bootcamp_assignments") == 0


def test_slalom_content_overrides_are_tenant_scoped(client, store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    slalom_admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")

    # This is the exact payload shape submitted by SlalomBootcampManager.tsx.
    source = client.post(BASE + "/content", headers=slalom_admin, json={
        "type": "article",
        "key": "security-foundations",
        "title": "Slalom Security Guide",
        "body": "Slalom-only security learning content.",
        "source": "Internal",
        "metadata": {"sourceUrl": "https://slalom.example/security"},
    })
    assert source.status_code == 200, source.text
    assert source.json()["contentType"] == "source"
    assert source.json()["type"] == "article"

    subject = client.post(BASE + "/content", headers=slalom_admin, json={
        "type": "subject",
        "key": "ai",
        "title": "Slalom AI Catalog",
        "body": "Tenant-specific AI curriculum",
    })
    assert subject.status_code == 200, subject.text
    assert subject.json()["contentType"] == "subject"

    patched = client.patch(
        BASE + f"/content?id={source.json()['id']}",
        headers=slalom_admin,
        json={
            "type": "article",
            "key": "security-foundations",
            "title": "Slalom Security Guide",
            "body": "Updated Slalom-only security learning content.",
            "source": "Internal",
            "metadata": {"sourceUrl": "https://slalom.example/security"},
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["type"] == "article"

    listed = client.get(BASE + "/content", headers=slalom_admin)
    assert listed.status_code == 200, listed.text
    assert {item["contentType"] for item in listed.json()["content"]} == {"source", "subject"}

    with tenancy.client_context("slalom"):
        assert "Updated Slalom-only security learning content." in topic_sources.get_topic_source(
            "security-foundations"
        )["explanation"]
        ai_subject = next(item for item in portal.portal_catalog() if item["id"] == "ai")
        assert ai_subject["title"] == "Slalom AI Catalog"
    with tenancy.client_context("infinite"):
        assert "Updated Slalom-only security learning content." not in topic_sources.get_topic_source(
            "security-foundations"
        )["explanation"]
        ai_subject = next(item for item in portal.portal_catalog() if item["id"] == "ai")
        assert ai_subject["title"] == "Artificial Intelligence"

    deleted = client.delete(
        BASE + f"/content?id={source.json()['id']}", headers=slalom_admin,
    )
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert all(item["id"] != source.json()["id"] for item in client.get(
        BASE + "/content", headers=slalom_admin,
    ).json()["content"])


def test_slalom_leaderboard_is_read_only_and_uses_three_strike_auto_submit(
    client, store, monkeypatch
):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    slalom_admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")
    _seed_employee_and_bank(store)

    assessment = _create_assessment(client, slalom_admin)
    cohort = _create_cohort(client, slalom_admin)
    result = _assign_bootcamp(client, slalom_admin, assessment["id"], cohort["id"])
    test_id = result["assignments"][0]["testId"]

    with tenancy.client_context("slalom"):
        questions = store.select("test_questions", {"test_id": test_id}, ["id"], "question_index asc")
        test = store.first("tests", {"id": test_id})
        store.update("tests", {
            "status": "completed",
            "completed_at": "2026-11-18T10:00:00+00:00",
            "score_correct": 2,
            "score_total": 3,
            "proctoring": {"warningCount": 3, "autoSubmitted": True},
        }, {"id": test_id})
        store.insert("test_attempts", [
            {
                "id": str(uuid.uuid4()), "test_id": test_id,
                "employee_id": test["employee_id"], "question_id": question["id"],
                "selected_option_index": 0, "is_correct": index < 2,
                "time_taken_seconds": 10 * (index + 1), "session_key": "session-1",
            }
            for index, question in enumerate(questions[:3])
        ])
        before = {
            table: store.count(table)
            for table in (
                "bootcamp_assessments", "bootcamp_cohorts", "bootcamp_cohort_members",
                "bootcamp_assignments", "bootcamp_content",
            )
        }

    assert employee_tests.MAX_VIOLATIONS == 3
    response = client.get(
        BASE + f"/leaderboard?assessment_id={assessment['id']}&cohort_id={cohort['id']}",
        headers=slalom_admin,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["employeeId"] == "BOOT1"
    assert entry["employeeName"] == "Bootcamp Employee"
    assert entry["assessmentName"] == "Security Foundations"
    assert entry["cohortName"] == "Cohort A"
    assert entry["scorePercent"] == 67
    assert entry["timeTakenSeconds"] == 60
    assert entry["violations"] == 3
    assert entry["autoSubmitted"] is True
    assert entry["rank"] == 1

    with tenancy.client_context("slalom"):
        assert {
            table: store.count(table)
            for table in (
                "bootcamp_assessments", "bootcamp_cohorts", "bootcamp_cohort_members",
                "bootcamp_assignments", "bootcamp_content",
            )
        } == before


def test_question_bank_parser_keeps_topic_and_difficulty():
    content = (
        b"Question,Option A,Option B,Option C,Option D,Correct Answer,Explanation,Topic,Difficulty\n"
        b"What is SSO?,One sign-on,Two keys,Three ports,Four tokens,A,Identity control,Security,easy\n"
    )
    questions = employee_portal_admin.parse_question_bank(content, "bank.csv")
    assert len(questions) == 1
    assert questions[0]["topic"] == "Security"
    assert questions[0]["difficulty"] == "easy"


def test_assessment_topics_and_difficulty_mix_filter_delivered_questions(client, store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    slalom_admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")

    def question(text: str, topic: str, difficulty: str) -> dict:
        return {
            "question_text": text, "options": ["Correct", "Wrong A", "Wrong B", "Wrong C"],
            "correct_option_index": 0, "explanation": "", "difficulty": difficulty, "topic": topic,
        }

    with tenancy.client_context("slalom"):
        employee_portal_admin.save_question_bank("Topic Bank", 4, [
            question("Security easy", "Security", "easy"),
            question("Security hard", "Security", "hard"),
            question("Security medium", "Security", "medium"),
            question("Cloud easy", "Cloud", "easy"),
        ])
        store.insert("employees", {
            "id": str(uuid.uuid4()), "employee_id": "BOOT2", "full_name": "Topic Employee",
            "email": "boot2@example.com", "designation": "Engineer", "department": "Engineering",
            "role": "employee", "product": "Topic Bank", "product_qb_eligible": True,
            "assessment_only": True, "is_first_login": False,
        })

    created = client.post(BASE + "/assessments", headers=slalom_admin, json={
        "name": "Topic Assessment", "bankKey": "Topic Bank", "questionCount": 2,
        "durationSeconds": 600, "topics": ["Security"],
        "difficultyMix": {"easy": 1, "medium": 0, "hard": 1}, "status": "published",
    })
    assert created.status_code == 200, created.text
    assessment_id = created.json()["id"]
    cohort = _create_cohort(client, slalom_admin, "Topic Cohort")
    members = client.post(
        BASE + f"/cohorts/{cohort['id']}/members/bulk",
        headers=slalom_admin, json={"employeeIds": ["BOOT2"]},
    )
    assert members.status_code == 200, members.text
    assigned = client.post(
        BASE + f"/assessments/{assessment_id}/assign",
        headers=slalom_admin, json={"cohortId": cohort["id"]},
    )
    assert assigned.status_code == 200, assigned.text
    test_id = assigned.json()["assignments"][0]["testId"]

    with tenancy.client_context("slalom"):
        texts = {row["question_text"] for row in store.select(
            "test_questions", {"test_id": test_id}, ["question_text"]
        )}
    assert texts == {"Security easy", "Security hard"}


def test_difficulty_mix_cannot_exceed_question_count(client, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    slalom_admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")

    response = client.post(BASE + "/assessments", headers=slalom_admin, json={
        "name": "Invalid Mix", "questionCount": 2, "durationSeconds": 600,
        "difficultyMix": {"easy": 1, "medium": 1, "hard": 1}, "status": "published",
    })
    assert response.status_code == 400, response.text
    assert "cannot exceed the question count" in response.text


def test_cohort_flow_creates_accounts_under_the_cohort_and_auto_assigns(client, store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")
    _seed_employee_and_bank(store)
    assessment = _create_assessment(client, admin)
    cohort = _create_cohort(client, admin, "Wave 1")

    empty = client.get(BASE + f"/cohorts/{cohort['id']}/overview", headers=admin).json()
    assert empty["stage"] == "learners" and empty["summary"]["learners"] == 0

    first = client.post(BASE + f"/cohorts/{cohort['id']}/learners", headers=admin, json={"learners": [
        {"employeeId": "NEW1", "name": "Asha Rao", "email": "asha@example.com", "role": "Analyst"},
        {"employeeId": "NEW2", "name": "Ben Lee", "email": "not-an-email"},
        {"employeeId": "new1", "name": "Dup", "email": "dup@example.com"},
    ]})
    assert first.status_code == 200, first.text
    body = first.json()
    assert [item["employeeId"] for item in body["enrolled"]] == ["NEW1"]
    assert body["enrolled"][0]["created"] is True and body["enrolled"][0]["firstLoginPassword"] == "NEW1"
    assert {item["row"] for item in body["failed"]} == {2, 3}

    with tenancy.client_context("slalom"):
        assert store.first("employees", {"employee_id": "NEW1"}) is not None
        assert store.count("bootcamp_cohort_members", {"cohort_id": cohort["id"]}) == 1

    staged = client.get(BASE + f"/cohorts/{cohort['id']}/overview", headers=admin).json()
    assert staged["stage"] == "assessments" and staged["summary"]["learners"] == 1

    assigned = client.post(BASE + f"/assessments/{assessment['id']}/assign", headers=admin, json={"cohortId": cohort["id"]})
    assert assigned.status_code == 200 and assigned.json()["assigned"] == 1

    late = client.post(BASE + f"/cohorts/{cohort['id']}/learners", headers=admin, json={"learners": [
        {"employeeId": "LATE1", "name": "Chen Wu", "email": "chen@example.com"},
    ]}).json()
    assert late["enrolled"][0]["assigned"] == 1

    overview = client.get(BASE + f"/cohorts/{cohort['id']}/overview", headers=admin).json()
    assert overview["stage"] == "progress"
    assert overview["summary"]["learners"] == 2 and overview["summary"]["assigned"] == 2
    assert {row["employeeId"]: row["assigned"] for row in overview["learners"]} == {"NEW1": 1, "LATE1": 1}
    assert overview["assessments"][0]["name"] == "Security Foundations"


def test_cohort_learner_upload_creates_accounts(client, store, monkeypatch):
    monkeypatch.setenv("SLALOM_ADMIN_PASSWORD", "SlalomBoot123!")
    admin_accounts.invalidate_cache()
    admin = _headers(client, "admin@slalom.com", "SlalomBoot123!")
    cohort = _create_cohort(client, admin, "Upload cohort")
    csv_bytes = b"Emp ID,Name,Email,Role\nUP1,Dana Fox,dana@example.com,Engineer\nUP2,Eli Roe,eli@example.com,Analyst\n"
    response = client.post(BASE + f"/cohorts/{cohort['id']}/learners/upload", headers=admin,
                           files={"file": ("roster.csv", csv_bytes, "text/csv")})
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 2 and response.json()["failed"] == []
    bad = client.post(BASE + f"/cohorts/{cohort['id']}/learners/upload", headers=admin,
                      files={"file": ("roster.txt", b"x", "text/plain")})
    assert bad.status_code == 400
