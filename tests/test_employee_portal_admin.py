import csv
import io
import re

from openpyxl import Workbook

from core.tokens import sign_token
from services import accounts, admin_accounts, corp_pool, employee_portal_admin, question_bank


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


def test_parse_marked_product_bank_sheets():
    workbook = Workbook()
    cmg = workbook.active
    cmg.title = "CMG"
    cmg.append(["Domain", "Product", "Question", "A", "B"])
    cmg.append(["PACO", "CMG", "Which CMG check validates service?", "Successful data session", "Correct"])
    cmg.append(["PACO", "CMG", "Which CMG check validates service?", "Device alarm tone", "Wrong"])
    cmg.append(["PACO", "CMG", "Which CMG check validates service?", "SIM storage", "Wrong"])
    cmg.append(["PACO", "CMG", "Which CMG check validates service?", "Time sync", "Wrong"])

    nrd = workbook.create_sheet("NRD")
    nrd.append(["Domain", "Product", "Question", "Options", "B"])
    nrd.append(["PACO", "NRD", "Why use NRD?", "Discovery and resilience", "Correct"])
    nrd.append(["PACO", "NRD", "Why use NRD?", "Replace all application servers", "Wrong"])
    nrd.append(["PACO", "NRD", "Why use NRD?", "Remove automation", "Wrong"])
    nrd.append(["PACO", "NRD", "Why use NRD?", "Eliminate service interfaces", "Wrong"])

    content = io.BytesIO()
    workbook.save(content)
    parsed_cmg = employee_portal_admin.parse_question_bank(content.getvalue(), "banks.xlsx", "CMG")
    parsed_nrd = employee_portal_admin.parse_question_bank(content.getvalue(), "banks.xlsx", "NRD")

    assert len(parsed_cmg) == len(parsed_nrd) == 1
    assert parsed_cmg[0]["options"] == ["Successful data session", "Device alarm tone", "SIM storage", "Time sync"]
    assert parsed_cmg[0]["correct_option_index"] == 0
    assert parsed_nrd[0]["options"] == [
        "Discovery and resilience", "Replace all application servers", "Remove automation", "Eliminate service interfaces",
    ]
    assert parsed_nrd[0]["correct_option_index"] == 0


def test_product_assignments_sample_independently_and_keep_correct_options(store, monkeypatch):
    questions = [
        {
            "question_text": f"Question {index}",
            "options": [f"{index}-{letter}" for letter in "ABCD"],
            "correct_option_index": index,
            "explanation": "",
            "difficulty": "medium",
        }
        for index in range(3)
    ]
    config = employee_portal_admin.save_question_bank("Product A", 2, questions)
    samples = iter(([0, 1], [2, 0]))

    class SampleRng:
        def __init__(self):
            self.indices = next(samples)

        def sample(self, population, count):
            assert count == len(self.indices)
            return [population[index] for index in self.indices]

    monkeypatch.setattr(employee_portal_admin.random, "SystemRandom", SampleRng)
    assigned = []
    for suffix in ("1", "2"):
        employee_id = f"SAMPLE{suffix}"
        store.insert("employees", {
            "id": f"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa{suffix}",
            "employee_id": employee_id, "email": f"{employee_id.lower()}@example.com",
            "full_name": employee_id, "role": "employee", "product": "Product A",
        })
        employee = store.first("employees", {"employee_id": employee_id})
        result = employee_portal_admin.assign_product_test(employee, config)
        rows = store.select("test_questions", {"test_id": result["testId"]}, order="question_index asc")
        assigned.append([row["question_text"] for row in rows])
        assert all(
            row["options"][row["correct_option_index"]]
            == questions[int(row["question_text"].split()[-1])]["options"][
                questions[int(row["question_text"].split()[-1])]["correct_option_index"]
            ]
            for row in rows
        )

    assert assigned == [["Question 0", "Question 1"], ["Question 2", "Question 0"]]


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
    product_banks = {
        "CSD": "CSD",
        "NPC": "NPC",
        "NCC": "NCC",
        "AAA": "AAA",
    }
    for product, prefix in product_banks.items():
        response = client.post(
            "/api/admin/employee-portal/question-banks",
            headers=admin,
            data={"product": product, "question_count": "1"},
            files={"file": ("bank.csv", _bank_csv(prefix), "text/csv")},
        )
        assert response.status_code == 200, response.text

    store.insert("employees", {
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "employee_id": "EMP54",
        "full_name": "Existing Employee",
        "email": "existing@example.com",
        "department": "general",
        "role": "employee",
        "product": "CSD",
        "designation": "Analyst",
        "product_qb_eligible": True,
        "assessment_only": True,
        "is_first_login": False,
    })
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Emp ID", "Name", "Email", "Role", "Product"])
    sheet.append(["EMP50", "Bulk Employee", "bulk@example.com", "Analyst", "CSD"])
    sheet.append(["EMP53", "Bulk Employee B", "bulk-b@example.com", "Analyst", "NPC"])
    sheet.append(["EMP55", "Bulk Employee C", "bulk-c@example.com", "Analyst", "NCC"])
    sheet.append(["EMP56", "Bulk Employee D", "bulk-d@example.com", "Analyst", "AAA"])
    sheet.append(["EMP54", "Existing Employee", "existing@example.com", "Analyst", "CSD"])
    sheet.append(["EMP51", "Unknown Product", "unknown@example.com", "Analyst", "Unknown Product"])
    content = io.BytesIO()
    workbook.save(content)
    result = client.post(
        "/api/admin/employee-portal/users/bulk",
        headers=admin,
        data={"question_type": "bank"},
        files={"file": ("roster.xlsx", content.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert result.status_code == 200
    assert result.json()["created"] == 4 and result.json()["updated"] == 1 and result.json()["failed"] == 1
    assert result.json()["results"][5]["row"] == 7
    for employee_id, expected_product in (
        ("EMP50", "CSD"),
        ("EMP53", "NPC"),
        ("EMP55", "NCC"),
        ("EMP56", "AAA"),
        ("EMP54", "CSD"),
    ):
        employee = store.first("employees", {"employee_id": employee_id})
        assert employee["designation"] == "Analyst"
        assert employee["product"] == expected_product
        test = store.first("tests", {"employee_id": employee["id"]})
        assert test is not None
        questions = store.select("test_questions", {"test_id": test["id"]})
        assert len(questions) == 1
        assert questions[0]["question_text"].startswith(expected_product)

        auth = {"Authorization": "Bearer " + sign_token(employee_id)}
        visible_test = client.get(f"/api/employee/tests/{test['id']}", headers=auth)
        assert visible_test.status_code == 200
        assert visible_test.json()["questions"][0]["question_text"].startswith(expected_product)

    configured_products = client.get("/api/admin/employee-portal", headers=admin).json()["products"]
    assert {product["product"] for product in configured_products} == set(product_banks)
    assert all(product["bankQuestionCount"] == 3 for product in configured_products)

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


def test_bulk_roster_accepts_product_name_header():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Emp ID", "Name", "Email", "Role", "Product Name"])
    sheet.append(["EMP57", "Roster Employee", "roster@example.com", "Analyst", "CSD"])
    content = io.BytesIO()
    workbook.save(content)

    parsed = employee_portal_admin.parse_employee_upload(content.getvalue(), "roster.xlsx", "bank")
    assert parsed[0]["product"] == "CSD"


def test_bulk_delete_portal_users_removes_accounts_and_linked_data(client, store, monkeypatch):
    admin = _admin(client, monkeypatch)
    employee_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    test_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    question_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    store.insert("employees", {
        "id": employee_uuid,
        "employee_id": "PORTAL_DELETE",
        "full_name": "Portal Delete",
        "email": "portal-delete@example.com",
        "role": "employee",
        "product": "CSD",
        "product_qb_eligible": True,
    })
    store.insert("tests", {"id": test_id, "employee_id": employee_uuid, "status": "pending"})
    store.insert("test_questions", {
        "id": question_id, "test_id": test_id, "question_index": 0, "question_text": "Question",
        "options": ["A", "B"], "correct_option_index": 0,
    })
    store.insert("test_attempts", {
        "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "test_id": test_id,
        "employee_id": employee_uuid, "question_id": question_id, "is_correct": True,
    })
    store.insert("assessment_attempts", {
        "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        "employee_id": "PORTAL_DELETE",
        "employee_uuid": employee_uuid,
        "topic_id": "resource-product-assessment",
        "test_id": test_id,
        "attempt_number": 1,
        "status": "in_progress",
    })
    deleted_videos = []
    deleted_backups = []
    monkeypatch.setattr(employee_portal_admin.test_video, "delete", deleted_videos.append)
    monkeypatch.setattr(
        employee_portal_admin.storage, "remove_prefix",
        lambda bucket, prefix: deleted_backups.append((bucket, prefix)) or 1,
    )

    response = client.request(
        "DELETE",
        "/api/admin/employees/portal",
        headers=admin,
        json={"employeeIds": ["PORTAL_DELETE"]},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"success": True, "deleted": 1}
    assert deleted_videos == [test_id]
    assert deleted_backups == [("backups", f"{test_id}-")]
    assert store.count("employees", {"employee_id": "PORTAL_DELETE"}) == 0
    assert store.count("tests", {"id": test_id}) == 0
    assert store.count("test_questions", {"test_id": test_id}) == 0
    assert store.count("test_attempts", {"test_id": test_id}) == 0
    assert store.count("assessment_attempts", {"test_id": test_id}) == 0


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
