import re

from core.tokens import sign_token
from services import accounts


def seed_employee(store, **over):
    row = {"id": "e1", "employee_id": "EMP1", "email": "emp1@infinite.com", "full_name": "Emp One",
           "department": "eng", "role": "employee", "is_first_login": True}
    row.update(over)
    store.seed("employees", [row])


def test_admin_login_requires_configured_password(client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "someone@infinite.com")
    res = client.post("/api/admin/auth/login", json={"email": "someone@infinite.com", "password": "x"})
    assert res.status_code == 503
    assert res.json()["error"] == "Admin password not setup"


def test_admin_login_success_and_wrong_password(client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "someone@infinite.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "S3cret!pw")
    ok = client.post("/api/admin/auth/login", json={"email": "someone@infinite.com", "password": "S3cret!pw"})
    assert ok.status_code == 200 and ok.json()["token"]
    bad = client.post("/api/admin/auth/login", json={"email": "someone@infinite.com", "password": "nope"})
    assert bad.status_code == 401
    valid = client.get("/api/admin/auth/validate", headers={"Authorization": f"Bearer {ok.json()['token']}"})
    assert valid.status_code == 200
    assert client.get("/api/admin/auth/validate").status_code == 401


def test_screening_admin_needs_its_own_env_password(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "AdminPw1!")
    res = client.post("/api/admin/auth/login", json={"email": "ramendras@infinite.com", "password": "x"})
    assert res.status_code == 503 and res.json()["error"] == "Admin password not setup"
    monkeypatch.setenv("SCREENING_ADMIN_PASSWORD", "ScreenPw1!")
    ok = client.post("/api/admin/auth/login", json={"email": "ramendras@infinite.com", "password": "ScreenPw1!"})
    assert ok.status_code == 200 and ok.json()["canViewEmployeePortal"] is False


def test_admin_rejects_foreign_domain(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "AdminPw1!")
    res = client.post("/api/admin/auth/login", json={"email": "x@gmail.com", "password": "AdminPw1!"})
    assert res.status_code == 400


def test_employee_first_login_flow(client, store, monkeypatch):
    password_hash, password_salt = accounts.hash_password("EMP1")
    seed_employee(store, password_hash=password_hash, password_salt=password_salt)
    res = client.post("/api/employee/auth/login", json={"employee_id": "EMP1", "password": "EMP1"})
    assert res.status_code == 200 and res.json()["status"] == "first_time_modal"
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    sent = []

    def dispatch(to, subject, html):
        sent.append(re.search(r">(\d{6})</p>", html).group(1))
        return "sent", None

    monkeypatch.setattr("services.mailer.dispatch", dispatch)
    requested = client.post("/api/employee/auth/request-password-change-otp", headers=headers)
    assert requested.status_code == 200 and sent
    changed = client.post("/api/employee/auth/confirm-password", headers=headers,
                          json={"action": "change", "password": "Str0ng!Passw", "otp": sent[-1]})
    assert changed.status_code == 200
    replay = client.post("/api/employee/auth/confirm-password", headers=headers,
                         json={"action": "change", "password": "Str0ng!Passw", "otp": sent[-1]})
    assert replay.status_code == 400
    again = client.post("/api/employee/auth/login", json={"employee_id": "EMP1", "password": "Str0ng!Passw"})
    assert again.json()["status"] == "ok"
    wrong = client.post("/api/employee/auth/login", json={"employee_id": "EMP1", "password": "bad"})
    assert wrong.status_code == 401
    me = client.get("/api/employee/auth/validate", headers=headers)
    assert me.status_code == 200 and me.json()["employee"]["employee_id"] == "EMP1"


def test_employee_password_login_remains_available_when_sso_is_enabled(client, store, monkeypatch):
    password_hash, password_salt = accounts.hash_password("Current!Pass1")
    seed_employee(store, is_first_login=False, password_hash=password_hash, password_salt=password_salt)
    monkeypatch.setattr("routers.employee_auth.saml.get_config", lambda: {"enabled": True})
    response = client.post("/api/employee/auth/login", json={"employee_id": "EMP1", "password": "Current!Pass1"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_employee_unknown_id_and_weak_password(client, store):
    seed_employee(store)
    assert client.post("/api/employee/auth/login", json={"employee_id": "NOPE", "password": ""}).status_code == 401
    weak = client.post("/api/employee/auth/set-password", json={"employee_id": "EMP1", "password": "weak"})
    assert weak.status_code == 401


def test_reset_password_requires_matching_email(client, store, monkeypatch):
    seed_employee(store, is_first_login=False)
    bad = client.post("/api/employee/auth/request-password-reset-otp",
                      json={"employee_id": "EMP1", "email": "other@x.com"})
    assert bad.status_code == 400
    sent = []

    def dispatch(to, subject, html):
        sent.append(re.search(r">(\d{6})</p>", html).group(1))
        return "sent", None

    monkeypatch.setattr("services.mailer.dispatch", dispatch)
    requested = client.post("/api/employee/auth/request-password-reset-otp",
                            json={"employee_id": "EMP1", "email": "emp1@infinite.com"})
    assert requested.status_code == 200 and sent
    good = client.post("/api/employee/auth/reset-password",
                       json={"employee_id": "EMP1", "otp": sent[-1], "password": "Str0ng!Passw"})
    assert good.status_code == 200
    assert store.first("employees", {"employee_id": "EMP1"})["email_verified"] is True
    assert accounts.verify_password("Str0ng!Passw", store.first("employees", {"employee_id": "EMP1"})["password_salt"],
                                    store.first("employees", {"employee_id": "EMP1"})["password_hash"])


def test_signed_in_password_change_requires_email_otp(client, store, monkeypatch):
    password_hash, password_salt = accounts.hash_password("Current!Pass1")
    seed_employee(store, is_first_login=False, password_hash=password_hash, password_salt=password_salt)
    headers = {"Authorization": f"Bearer {sign_token('EMP1')}"}
    no_code = client.post("/api/employee/auth/change-password", headers=headers, json={"newPassword": "Next!Passw2"})
    assert no_code.status_code == 400
    sent = []

    def dispatch(to, subject, html):
        sent.append(re.search(r">(\d{6})</p>", html).group(1))
        return "sent", None

    monkeypatch.setattr("services.mailer.dispatch", dispatch)
    requested = client.post("/api/employee/auth/request-password-change-otp", headers=headers)
    assert requested.status_code == 200 and sent
    changed = client.post("/api/employee/auth/change-password", headers=headers,
                          json={"newPassword": "Next!Passw2", "otp": sent[-1]})
    assert changed.status_code == 200
    assert store.first("employees", {"employee_id": "EMP1"})["email_verified"] is True
    assert client.post("/api/employee/auth/login", json={"employee_id": "EMP1", "password": "Next!Passw2"}).json()["status"] == "ok"


def test_login_rate_limit(client, store):
    seed_employee(store)
    codes = [client.post("/api/employee/auth/login", json={"employee_id": "NOPE", "password": ""}).status_code for _ in range(9)]
    assert 429 in codes


def test_token_roundtrip_and_tamper():
    from core.tokens import verify_token

    token = sign_token("emp1")
    assert verify_token(token) == "EMP1"
    assert verify_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
