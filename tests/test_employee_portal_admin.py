import csv
import io
import re

from openpyxl import Workbook

from core.tokens import sign_token
from services import accounts, admin_accounts, corp_pool, question_bank


def _admin(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "AdminPw1!")
    token = client.post("/api/admin/auth/login", json={
        "email": "admin@infinite.com", "password": "AdminPw1!",
    }).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _bank_csv(prefix="Q"):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Question", "Option A", "Option B", "Option C", "Option D", "Correct Answer"])
    writer.writerow([f"{prefix}1", "A1", "B1", "C1", "D1", "A"])
    writer.writerow([f"{prefix}2", "A2", "B2", "C2", "D2", "B"])
    writer.writerow([f"{prefix}3", "A3", "B3", "C3", "D3", "C"])
    return output.getvalue().encode()


def test_ai_question_generator_uses_model_output_and_falls_back_with_warning(monkeypatch, caplog):
    model_questions = [{
        "question": f"Model question {index}",
        "options": ["Choice A", "Choice B", "Choice C", "Choice D"],
        "correctIndex": 2,
        "explanation": "Choice C is correct.",
        "difficulty": "high",
    } for index in range(10)]
    monkeypatch.setattr(question_bank.llm, "complete_json", lambda *_args, **_kwargs: model_questions)
    generated = question_bank.generate_ai_questions("AI", "Neural networks", "high")
    assert generated[0]["question"] == "Model question 0"
    assert generated[0]["correctIndex"] == 2
    assert generated[0]["difficulty"] == "advanced"

    def unavailable(*_args, **_kwargs):
        raise question_bank.llm.LlmError("LLM unavailable")

    fallback = [{"question": "Fallback", "options": ["A", "B", "C", "D"], "correctIndex": 0}]
    monkeypatch.setattr(question_bank.llm, "complete_json", unavailable)
    monkeypatch.setattr(question_bank, "fetch_questions", lambda *_args, **_kwargs: fallback)
    assert question_bank.generate_ai_questions("AI", "Neural networks", "basic") == fallback
    assert "using the built-in topic question fallback" in caplog.text


def test_ai_questionnaire_defaults_without_product_and_supports_selection(client, store, monkeypatch):
    admin = _admin(client, monkeypatch)
    default_user = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "AI_DEFAULT", "name": "AI Default Employee", "email": "ai-default@example.com",
        "designation": "Analyst", "product": "Ignored product without a question source",
    })
    assert default_user.status_code == 200, default_user.text
    assert default_user.json()["questionSource"] == "ai"
    employee = store.first("employees", {"employee_id": "AI_DEFAULT"})
    assert employee["product"] == "AI Questionnaire"
    assert employee["assessment_only"] is False

    explicit_ai = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "AI_HIGH", "name": "AI High Employee", "email": "ai-high@example.com",
        "designation": "Engineer", "questionType": "AI Questionnaire", "aiDifficulty": "high",
    })
    assert explicit_ai.status_code == 200, explicit_ai.text
    assert explicit_ai.json()["questionSource"] == "ai"
    assert store.first("employees", {"employee_id": "AI_HIGH"})["product"] == "AI Questionnaire"

    generated = [{
        "question": f"Generated question {index}",
        "options": ["Wrong A", "Correct", "Wrong C", "Wrong D"],
        "correctIndex": 1,
        "explanation": "The selected option is correct.",
        "difficulty": "beginner",
    } for index in range(10)]
    monkeypatch.setattr("routers.employee_portal.question_bank.generate_ai_questions", lambda *_args, **_kwargs: generated)
    auth = {"Authorization": "Bearer " + sign_token("AI_DEFAULT")}
    catalog = client.get("/api/employee/catalog", headers=auth).json()
    assert "resource-product-assessment" not in {subject["id"] for subject in catalog}
    ai_topics = next(subject for subject in catalog if subject["id"] == "ai")
    levels = {topic["difficulty"] for module in ai_topics["modules"] for topic in module["topics"]}
    assert {"beginner", "intermediate", "advanced"} <= levels

    quiz = client.post("/api/employee/learning/ai-fundamentals/ai-quiz", headers=auth, json={})
    assert quiz.status_code == 200, quiz.text
    assigned = store.select("test_questions", {"test_id": quiz.json()["test_id"]})
    assert len(assigned) == 10
    assert assigned[0]["correct_option_index"] == 1


def test_create_employee_and_assign_random_product_questions(client, store, monkeypatch):
    admin = _admin(client, monkeypatch)
    bank = client.post(
        "/api/admin/employee-portal/question-banks",
        headers=admin,
        data={"product": "Product A", "question_count": "2"},
        files={"file": ("bank.csv", _bank_csv(), "text/csv")},
    )
    assert bank.status_code == 200, bank.text
    created = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "EMP42", "name": "Employee One", "email": "employee@example.com",
        "designation": "Engineer", "product": " product a ", "questionType": "bank",
    })
    assert created.status_code == 200
    assert created.json()["firstLoginPassword"] == "EMP42"
    assert created.json()["questionCount"] == 2
    employee = store.first("employees", {"employee_id": "EMP42"})
    assert employee["product"] == "Product A"
    assert employee["designation"] == "Engineer"
    assert employee["is_first_login"] is False
    login = client.post("/api/employee/auth/login", json={"employee_id": "EMP42", "password": "EMP42"})
    assert login.status_code == 200 and login.json()["status"] == "ok"
    test = store.first("tests", {"employee_id": employee["id"]})
    assigned = store.select("test_questions", {"test_id": test["id"]})
    assert test["topic_title"] == "Product A"
    assert len(assigned) == 2
    assert {row["question_text"] for row in assigned} <= {"Q1", "Q2", "Q3"}
    original_questions = {row["id"] for row in assigned}
    original_hash = employee["password_hash"]
    repeated = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "EMP42", "name": "Employee One Updated", "email": "employee@example.com",
        "designation": "Engineer", "product": "Product A", "questionType": "bank",
    })
    assert repeated.status_code == 200 and repeated.json()["created"] is False
    assert store.first("employees", {"employee_id": "EMP42"})["password_hash"] == original_hash
    assert {row["id"] for row in store.select("test_questions", {"test_id": test["id"]})} == original_questions

    renewed_bank = client.post(
        "/api/admin/employee-portal/question-banks",
        headers=admin,
        data={"product": "Product A", "question_count": "2"},
        files={"file": ("bank.csv", _bank_csv("Renewed"), "text/csv")},
    )
    assert renewed_bank.status_code == 200
    reimported = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "EMP42", "name": "Employee One Updated", "email": "employee@example.com",
        "designation": "Engineer", "product": "Product A", "questionType": "bank",
    })
    assert reimported.status_code == 200
    assert {row["question_text"] for row in store.select("test_questions", {"test_id": test["id"]})} <= {
        "Renewed1", "Renewed2", "Renewed3",
    }

    second_bank = client.post(
        "/api/admin/employee-portal/question-banks",
        headers=admin,
        data={"product": "Product B", "question_count": "2"},
        files={"file": ("bank.csv", _bank_csv("B"), "text/csv")},
    )
    assert second_bank.status_code == 200
    second = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "EMP43", "name": "Employee Two", "email": "two@example.com", "product": "Product B",
        "questionType": "bank",
    })
    assert second.status_code == 200
    second_test = store.first("tests", {"employee_id": store.first("employees", {"employee_id": "EMP43"})["id"]})
    assert {row["question_text"] for row in store.select("test_questions", {"test_id": second_test["id"]})} <= {"B1", "B2", "B3"}
    assert client.get("/api/admin/employee-portal", headers=admin).json()["employees"][0]["product"] == "Product A"


def test_bulk_upload_reports_row_errors(client, store, monkeypatch):
    admin = _admin(client, monkeypatch)
    client.post(
        "/api/admin/employee-portal/question-banks",
        headers=admin,
        data={"product": "Product A", "question_count": "1"},
        files={"file": ("bank.csv", _bank_csv(), "text/csv")},
    )
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Emp ID", "Name", "Email", "Role", "Product"])
    sheet.append(["EMP50", "Bulk Employee", "bulk@example.com", "Analyst", "Product A"])
    sheet.append(["EMP51", "Unknown Product", "unknown@example.com", "Analyst", "Product B"])
    content = io.BytesIO()
    workbook.save(content)
    result = client.post(
        "/api/admin/employee-portal/users/bulk",
        headers=admin,
        data={"question_type": "bank"},
        files={"file": ("roster.xlsx", content.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert result.status_code == 200
    assert result.json()["created"] == 1 and result.json()["failed"] == 1
    assert result.json()["results"][1]["row"] == 3
    employee = store.first("employees", {"employee_id": "EMP50"})
    assert employee["designation"] == "Analyst" and employee["product"] == "Product A"
    assert store.count("tests", {"employee_id": employee["id"]}) == 1

    ai_workbook = Workbook()
    ai_sheet = ai_workbook.active
    ai_sheet.append(["Emp ID", "Name", "Email", "Role"])
    ai_sheet.append(["EMP52", "AI Bulk Employee", "ai-bulk@example.com", "Analyst"])
    ai_content = io.BytesIO()
    ai_workbook.save(ai_content)
    ai_result = client.post(
        "/api/admin/employee-portal/users/bulk",
        headers=admin,
        data={"question_type": "ai"},
        files={"file": ("ai-roster.xlsx", ai_content.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert ai_result.status_code == 200
    assert ai_result.json()["created"] == 1 and ai_result.json()["failed"] == 0
    ai_employee = store.first("employees", {"employee_id": "EMP52"})
    assert ai_employee["product"] == "AI Questionnaire"
    assert store.count("tests", {"employee_id": ai_employee["id"]}) == 0


def test_employee_portal_edit_and_password_reset_notify_user(client, store, monkeypatch):
    admin = _admin(client, monkeypatch)
    bank = client.post(
        "/api/admin/employee-portal/question-banks",
        headers=admin,
        data={"product": "Product A", "question_count": "1"},
        files={"file": ("bank.csv", _bank_csv(), "text/csv")},
    )
    assert bank.status_code == 200
    created = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "EMP60", "name": "Original Name", "email": "original@example.com", "product": "Product A",
        "questionType": "bank",
    })
    assert created.status_code == 200
    original = store.first("employees", {"employee_id": "EMP60"})

    updated = client.patch("/api/admin/employee-portal/users/EMP60", headers=admin, json={
        "name": "Updated Name", "email": "updated@example.com", "designation": "Engineer", "product": "Product A",
    })
    assert updated.status_code == 200
    employee = store.first("employees", {"employee_id": "EMP60"})
    assert employee["full_name"] == "Updated Name"
    assert employee["email"] == "updated@example.com"
    assert employee["designation"] == "Engineer"
    assert employee["password_hash"] == original["password_hash"]
    assert employee["email_verified"] is False

    messages = []

    def dispatch(to, subject, body):
        messages.append((to, subject, body))
        return "sent", None

    monkeypatch.setattr("services.mailer.dispatch", dispatch)
    reset = client.post("/api/admin/employee-portal/users/EMP60/reset-password", headers=admin)
    assert reset.status_code == 200
    assert reset.json()["success"] is True and reset.json()["emailSent"] is True
    assert messages and messages[0][0] == "updated@example.com"
    assert "EMP60" in messages[0][2]
    employee = store.first("employees", {"employee_id": "EMP60"})
    assert employee["is_first_login"] is False
    assert accounts.verify_password("EMP60", employee["password_salt"], employee["password_hash"])

    login = client.post("/api/employee/auth/login", json={"employee_id": "EMP60", "password": "EMP60"})
    assert login.status_code == 200
    assert login.json()["status"] == "ok"
    assert "recommend changing it" in messages[0][2]
    verified_row = client.get("/api/admin/employee-portal", headers=admin).json()["employees"][0]
    assert verified_row["emailVerified"] is False

    otp_messages = []

    def dispatch_otp(to, subject, body):
        otp_messages.append(re.search(r">(\d{6})</p>", body).group(1))
        return "sent", None

    monkeypatch.setattr("services.mailer.dispatch", dispatch_otp)
    requested = client.post("/api/employee/auth/request-password-reset-otp", json={
        "employee_id": "EMP60", "email": "updated@example.com",
    })
    assert requested.status_code == 200
    verified = client.post("/api/employee/auth/reset-password", json={
        "employee_id": "EMP60", "otp": otp_messages[-1], "password": "New!StrongPass2",
    })
    assert verified.status_code == 200
    verified_row = client.get("/api/admin/employee-portal", headers=admin).json()["employees"][0]
    assert verified_row["emailVerified"] is True


def test_employee_portal_password_reset_surfaces_email_delivery_failure(client, store, monkeypatch):
    admin = _admin(client, monkeypatch)
    client.post(
        "/api/admin/employee-portal/question-banks",
        headers=admin,
        data={"product": "Product A", "question_count": "1"},
        files={"file": ("bank.csv", _bank_csv(), "text/csv")},
    )
    created = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "EMP61", "name": "Employee", "email": "employee@example.com", "product": "Product A",
        "questionType": "bank",
    })
    assert created.status_code == 200
    monkeypatch.setattr("services.mailer.dispatch", lambda *_: ("simulated", None))
    reset = client.post("/api/admin/employee-portal/users/EMP61/reset-password", headers=admin)
    assert reset.status_code == 200
    assert reset.json()["success"] is True and reset.json()["emailSent"] is False
    employee = store.first("employees", {"employee_id": "EMP61"})
    assert employee["is_first_login"] is False
    assert accounts.verify_password("EMP61", employee["password_salt"], employee["password_hash"])


def test_employee_portal_profile_edit_preserves_assessment_score(client, store, monkeypatch):
    admin = _admin(client, monkeypatch)
    bank = client.post(
        "/api/admin/employee-portal/question-banks",
        headers=admin,
        data={"product": "Product A", "question_count": "1"},
        files={"file": ("bank.csv", _bank_csv(), "text/csv")},
    )
    assert bank.status_code == 200
    created = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "EMP63", "name": "Portal User", "email": "portal@example.com",
        "designation": "Engineer", "product": "Product A", "questionType": "bank",
    })
    assert created.status_code == 200
    test_id = created.json()["testId"]
    store.update("tests", {"status": "completed", "score_correct": 1, "score_percent": 100}, {"id": test_id})

    updated = client.patch("/api/admin/employee-portal/users/EMP63", headers=admin, json={
        "name": "Updated Portal User", "email": "updated@example.com",
        "designation": "Senior Engineer", "department": "Engineering",
        "score": 0,
    })

    assert updated.status_code == 200, updated.text
    employee = store.first("employees", {"employee_id": "EMP63"})
    assert employee["full_name"] == "Updated Portal User"
    assert employee["email"] == "updated@example.com"
    assert employee["designation"] == "Senior Engineer"
    assert employee["department"] == "Engineering"
    assert employee["email_verified"] is False
    result = store.first("tests", {"id": test_id})
    assert result["score_correct"] == 1 and result["score_percent"] == 100


def test_employee_portal_table_actions_update_and_reset_managed_accounts(client, store, monkeypatch):
    admin = _admin(client, monkeypatch)
    bank = client.post(
        "/api/admin/employee-portal/question-banks",
        headers=admin,
        data={"product": "Product A", "question_count": "1"},
        files={"file": ("bank.csv", _bank_csv(), "text/csv")},
    )
    assert bank.status_code == 200
    created = client.post("/api/admin/employee-portal/users", headers=admin, json={
        "employeeId": "EMP62", "name": "Portal User", "email": "portal@example.com", "product": "Product A",
        "questionType": "bank",
    })
    assert created.status_code == 200

    updated = client.patch("/api/admin/employees/portal/EMP62", headers=admin, json={
        "updates": {
            "full_name": "Updated Portal User",
            "email": "updated-portal@example.com",
            "designation": "Senior Engineer",
            "department": "Engineering",
        },
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["success"] is True
    employee = store.first("employees", {"employee_id": "EMP62"})
    assert employee["full_name"] == "Updated Portal User"
    assert employee["email"] == "updated-portal@example.com"
    assert employee["designation"] == "Senior Engineer"
    assert employee["department"] == "Engineering"
    assert employee["email_verified"] is False

    sent = []
    monkeypatch.setattr("services.mailer.dispatch", lambda to, subject, body: (sent.append((to, subject, body)) or ("sent", None)))
    reset = client.post("/api/admin/employees/portal/EMP62/reset-password", headers=admin)
    assert reset.status_code == 200, reset.text
    assert reset.json()["success"] is True and reset.json()["emailSent"] is True
    assert sent[0][0] == "updated-portal@example.com"
    assert "recommend changing it" in sent[0][2]
    employee = store.first("employees", {"employee_id": "EMP62"})
    assert employee["is_first_login"] is False
    assert accounts.verify_password("EMP62", employee["password_salt"], employee["password_hash"])

    assert client.patch("/api/admin/employees/portal/EMP62", json={"updates": {"full_name": "No auth"}}).status_code == 401
    assert client.post("/api/admin/employees/portal/UNKNOWN/reset-password", headers=admin).status_code == 404

    saved, error, _ = admin_accounts.save_named(
        "corp-only@infinite.com", {"corpPool": True}, "CorpOnly1!",
    )
    assert saved, error
    corp_admin = client.post("/api/admin/auth/login", json={
        "email": "corp-only@infinite.com", "password": "CorpOnly1!",
    }).json()["token"]
    denied = client.patch(
        "/api/admin/employees/portal/EMP62",
        headers={"Authorization": f"Bearer {corp_admin}"},
        json={"updates": {"full_name": "Unauthorized edit"}},
    )
    assert denied.status_code == 403


def test_corp_pool_scores_cannot_be_manually_edited(client, store, monkeypatch):
    admin = _admin(client, monkeypatch)
    corp_pool.save_roster([{
        "employee_id": "CORP1", "full_name": "Corp Pool User", "score": 42,
    }])

    response = client.patch("/api/admin/employees", headers=admin, json={
        "employeeId": "CORP1", "updates": {"score": 99},
    })

    assert response.status_code == 400
    assert "calculated automatically" in response.json()["error"]
    assert corp_pool.load_roster()[0]["score"] == 42


def test_employee_portal_configuration_requires_portal_config_permission(client, monkeypatch):
    admin = _admin(client, monkeypatch)
    assert client.get("/api/admin/employee-portal", headers=admin).status_code == 200
    assert client.get("/api/admin/employee-portal").status_code == 401
