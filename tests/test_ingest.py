import io
import zipfile

import pytest
from openpyxl import Workbook

from services import corp_pool, docs_storage, llm, resumes
from services import automation as au


@pytest.fixture()
def admin(client, monkeypatch, tmp_path):
    for target in ("services.storage", "services.structured_log"):
        monkeypatch.setattr(f"{target}.UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("ADMIN_PASSWORD", "AdminPw1!")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (_ for _ in ()).throw(llm.LlmError("down")))
    token = client.post("/api/admin/auth/login", json={"email": "boss@infinite.com", "password": "AdminPw1!"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def xlsx(rows) -> bytes:
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ROSTER = xlsx([["Emp No", "Emp Name", "Skills", "Grade", "Designation", "Official Mail Id"],
               ["1001", "Alice A", "Java, Spring Boot, SQL", "E2", "Senior Engineer", "alice@x.com"],
               ["1002", "Bob B", "Python, React", "E1", "QA Engineer", ""],
               ["", "", "", "", "", ""]])
BR_BOOK = xlsx([["Auto req ID", "Designation", "Mandatory Skills", "Job Description", "Recruiter"],
                ["50001BR", "Java Developer", "Java, Spring", "Build services " * 10, "rm1@infinite.com"],
                ["bad", "x", "y", "z", "q"]])


def test_requires_admin(client):
    assert client.post("/api/admin/upload_unified").status_code == 401 and client.post("/api/admin/refresh?type=employees").status_code == 401


def test_corp_pool_excel_upload_then_merge(client, store, admin):
    res = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("Corp Pool.xlsx", ROSTER)}, data={"category": "employee"})
    body = res.json()
    assert res.status_code == 200 and body["category"] == "employee" and body["refreshResult"]["loaded"] == 2 and body["refreshResult"]["added"] == 2
    roster = {e["employee_id"]: e for e in corp_pool.load_roster()}
    assert roster["1001"]["email"] == "alice@x.com" and roster["1002"]["email"] == "1002@example.com" and roster["1001"]["source_file"] == "Corp Pool.xlsx"
    corp_pool.save_roster([{**roster["1001"], "shortlisted": True, "score_override": 77, "manually_edited": True, "full_name": "Alice Edited"}, roster["1002"]])
    again = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("Corp Pool.xlsx", ROSTER)}).json()["refreshResult"]
    assert again["updated"] == 2 and again["added"] == 0
    merged = {e["employee_id"]: e for e in corp_pool.load_roster()}
    assert merged["1001"]["shortlisted"] is True and merged["1001"]["full_name"] == "Alice Edited" and merged["1001"]["score"] == 77
    assert docs_storage.list_files("Corp Pool") == ["Corp Pool.xlsx"]


def test_deleted_employee_returns_on_reupload_but_not_on_scan(client, store, admin):
    client.post("/api/admin/upload_unified", headers=admin, files={"file": ("Corp Pool.xlsx", ROSTER)})
    client.request("DELETE", "/api/admin/employees", headers=admin, json={"ids": ["1002"]})
    scan = client.post("/api/admin/refresh?type=employees", headers=admin).json()
    assert scan["success"] and [e["employee_id"] for e in corp_pool.load_roster()] == ["1001"]
    client.post("/api/admin/upload_unified", headers=admin, files={"file": ("Corp Pool.xlsx", ROSTER)})
    assert sorted(e["employee_id"] for e in corp_pool.load_roster()) == ["1001", "1002"]


def test_csv_and_zip_and_cv_roster_ingest(client, store, admin, monkeypatch):
    csv_bytes = "emp no;emp name;skills\n2001;Carl C;Go, Docker\n".encode()
    res = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("pool list.csv", csv_bytes)}).json()
    assert res["category"] == "employee" and res["refreshResult"]["added"] == 1
    cv = "Dana Doe\nEmployee ID: 3003344\nSenior Engineer\nSkills: Python, AWS, Docker\ndana@x.com\n"
    monkeypatch.setattr(resumes, "extract_text", lambda data, name=None: cv)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cvs/dana.pdf", b"%PDF-1.4 x")
        z.writestr("__MACOSX/._dana.pdf", b"%PDF")
        z.writestr("readme.png", b"x")
    zipped = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("pool.zip", buf.getvalue())}, data={"category": "employee"})
    assert zipped.status_code == 200 and zipped.json()["refreshResult"]["added"] == 1
    people = {e["employee_id"]: e for e in corp_pool.load_roster()}
    assert people["3003344"]["full_name"] == "Dana Doe" and people["3003344"]["email"] == "dana@x.com" and "python" in people["3003344"]["matchingSkills"]
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("a.png", b"x")
    assert client.post("/api/admin/upload_unified", headers=admin, files={"file": ("empty.zip", empty.getvalue())}, data={"category": "employee"}).status_code == 400


def test_br_workbook_creates_requirements_and_respects_deletes(client, store, admin):
    res = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("51210BR_raw.xlsx", BR_BOOK)}, data={"category": "br"})
    assert res.status_code == 200 and res.json()["category"] == "br" and res.json()["refreshResult"]["processedBRs"] == 1
    jds = client.get("/api/admin/jd", headers=admin).json()["jds"]
    assert len(jds) == 1 and jds[0]["fileName"].startswith("50001BR |") and jds[0]["rmEmail"] == "rm1@infinite.com" and "Job Title: Java Developer" in jds[0]["jdText"]
    client.request("DELETE", "/api/admin/jd", headers=admin, json={"ids": [jds[0]["id"]]})
    assert client.get("/api/admin/jd", headers=admin).json()["jds"] == []
    client.post("/api/admin/upload_unified", headers=admin, files={"file": ("51210BR_raw.xlsx", BR_BOOK)}, data={"category": "br"})
    assert len(client.get("/api/admin/jd", headers=admin).json()["jds"]) == 1
    nothing = xlsx([["Auto req ID", "Designation"], ["junk", "x"]])
    assert client.post("/api/admin/upload_unified", headers=admin, files={"file": ("2BR.xlsx", nothing)}, data={"category": "br"}).status_code == 400


def test_jd_document_upload_and_refresh_scan(client, store, admin):
    res = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("51210BR.txt", b"Job Title: Java Developer\nMandatory Skills: Java, SQL")}, data={"category": "jd"})
    assert res.status_code == 200 and res.json()["refreshResult"]["convertedJDs"] == 1
    row = store.select("job_descriptions")[0]
    assert row["file_name"] == "51210BR | 51210BR.txt" and "Mandatory Skills:" in row["jd_text"]
    plain = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("notes.txt", b"Python developer role needed here")}, data={"category": "jd"})
    assert plain.status_code == 200 and "BR" in store.select("job_descriptions")[-1]["file_name"].split("|")[0]
    assert client.post("/api/admin/refresh?type=requirements", headers=admin).json()["success"] is True
    assert client.post("/api/admin/refresh?type=bogus", headers=admin).status_code == 400
    assert client.post("/api/admin/refresh?type=sync-selected&activeJdId=x", headers=admin).json()["success"] is True


def test_candidate_scan_and_interviews_refresh(client, store, admin, monkeypatch):
    sample = "Jane Doe\nSoftware Engineer\njane@x.com\n\nSkills\nPython, SQL\n"
    monkeypatch.setattr(resumes, "extract_text", lambda data, name=None: sample)
    up = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("jane.pdf", b"%PDF-1.4 jane")}, data={"category": "resume"})
    assert up.status_code == 200 and up.json()["refreshResult"]["processed"] == 1
    assert store.count("resumes") == 1
    assert docs_storage.list_files("Resumes") == []
    again = client.post("/api/admin/refresh?type=candidates", headers=admin).json()
    assert again["duplicates"] == 0 and again["processed"] == 0
    repeat = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("jane.pdf", b"%PDF-1.4 jane")}, data={"category": "resume"})
    assert repeat.json()["refreshResult"]["duplicates"] == 1 and docs_storage.list_files("Resumes") == []
    assert store.count("resumes") == 1
    assert client.post("/api/admin/refresh?type=interviews", headers=admin).json() == {"success": True, "count": 0}


def test_portal_mapping_upload_and_credentials_blocked(client, store, admin):
    mapping = xlsx([["Emp ID", "Emp Name", "Assigned Question 1"], ["1001", "Mapped", "What is X?"]])
    res = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("Resource Question Mapping.xlsx", mapping)})
    assert res.status_code == 200 and res.json()["category"] == "portal-mapping"
    assert docs_storage.list_files("Portal Mapping") == ["Resource_Question_Mapping.xlsx"]
    assert client.get("/api/admin/employees/mapping-questions?employeeId=1001", headers=admin).json()["assigned_questions"] == ["What is X?"]
    blocked = client.post("/api/admin/upload_unified", headers=admin, files={"file": ("Employee User Credentials.xlsx", ROSTER)})
    assert blocked.status_code == 400 and "Credential" in blocked.json()["error"]
    assert client.post("/api/admin/upload_unified", headers=admin, files={"file": ("x.exe", b"MZ")}).status_code == 400
    assert client.post("/api/admin/upload_unified", headers=admin).status_code == 400


def test_cv_profile_helpers():
    text = "Employee ID: 1033925\nJithender Reddy\nSDET\nPython, Selenium, Jira\nj@x.com"
    profile = au.corp_pool_profile_from_cv("Jithender SDET.pdf", text)
    assert profile["employeeId"] == "1033925" and profile["name"] == "Jithender Reddy" and profile["email"] == "j@x.com"
    assert au.extract_employee_id_from_cv("call 9876543210", "x.pdf") == ""
    assert au.skills_from_cv_text(text) == ["python", "jira", "selenium"] or set(au.skills_from_cv_text(text)) == {"python", "selenium", "jira"}
    assert au.normalize_br_id("51210 BR") == "51210BR" and au.normalize_br_id("12345") == "12345BR" and au.normalize_br_id("abc") == ""
