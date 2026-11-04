import logging
from typing import Optional
import re
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from core.auth import bearer_token, require_employee
from core.config import env
from core.errors import json_error, public_error
from core.security import get_client_ip, is_rate_limited, is_rate_limited_any, rate_limited_response
from core.tokens import sign_token, verify_token
from core.validation import as_email, as_employee_id, as_password, as_trimmed
from services import accounts, admin_accounts, audit, employee_otp, saml
from services.products import format_product_display_name

log = logging.getLogger("talentscope")
router = APIRouter(prefix="/api/employee/auth", tags=["employee-auth"])

HANDOFF_COOKIE = "ms_sso_handoff"
ADMIN_HANDOFF_COOKIE = "ms_admin_sso_handoff"
SSO_FAILED = "Microsoft sign-in failed validation. Contact IT if this continues."


def _strong(password: str) -> bool:
    return bool(
        len(password) >= 8
        and re.search(r"[A-Z]", password)
        and re.search(r"[a-z]", password)
        and re.search(r"[0-9]", password)
        and re.search(r"[^A-Za-z0-9]", password)
    )


def _cookie_secure() -> bool:
    return env("NODE_ENV").lower() == "production"


def _employee_payload(account: dict) -> dict:
    return {"employee_id": account["employee_id"], "full_name": account["full_name"]}


@router.post("/login")
def login(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"auth:ip:{ip}"], 10, 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many login attempts. Please try again later.")
    try:
        raw_login = body.get("employee_id")
        employee_id = as_employee_id(raw_login)
        login_email = as_email(raw_login) if isinstance(raw_login, str) else None
        raw_password = body.get("password")
        password = as_password(raw_password.strip() if isinstance(raw_password, str) else raw_password, allow_empty=True)
        if not employee_id and not login_email:
            return json_error(400, "Enter a valid Employee ID or registered email address.")
        if password is None:
            return json_error(400, "Password must be provided as text and cannot exceed 256 characters.")
        rate_key = (employee_id or login_email or "").upper()
        account_limit = is_rate_limited_any([f"auth:acct:{rate_key}"], 8, 60_000)
        if account_limit.limited:
            return rate_limited_response(account_limit, "Too many login attempts. Please try again later.")
        employee = accounts.get_account(employee_id) if employee_id else None
        if not employee and login_email:
            employee = accounts.get_account_by_email(login_email)
            if employee:
                employee_id = employee["employee_id"]
        if not employee:
            # Same message as bad password to avoid account enumeration.
            return json_error(401, "Employee ID or password is incorrect. Check both fields and try again.")
        has_pw = accounts.has_password(employee)
        hash_valid = has_pw and accounts.verify_password(password, employee.get("password_salt") or "", employee.get("password_hash") or "")
        first_login = employee["is_first_login"] if employee.get("is_first_login") is not None else not has_pw
        actor = employee["email"] or employee["employee_id"]
        # Invites set temporary password = Employee ID. Allow that (and matching hash)
        # on first login, then prompt to keep or change via first_time_modal.
        is_emp_id_pw = bool(password) and password.upper() == employee["employee_id"].strip().upper()
        if not password:
            audit.add_log(actor, "EMPLOYEE_LOGIN_FAILURE", "Employee Portal", "Blank password rejected", ip)
            return json_error(401, "Enter your employee ID and password.")
        allowed_initial = first_login and is_emp_id_pw
        if not hash_valid and not allowed_initial:
            if first_login and not has_pw:
                audit.add_log(actor, "EMPLOYEE_LOGIN_FAILURE", "Employee Portal", "No initial employee password is configured", ip)
                return json_error(401, "No password is configured for this account yet. Sign in with Microsoft or ask HR to set your initial password.")
            audit.add_log(actor, "EMPLOYEE_LOGIN_FAILURE", "Employee Portal", "Invalid password provided", ip)
            return json_error(401, "Employee ID or password is incorrect. Check both fields and try again.")
        # Emp-ID temporary password always forces the keep/change modal.
        force_password_prompt = first_login or (is_emp_id_pw and hash_valid)
        token = sign_token(employee["employee_id"])
        accounts.sync_account(employee)
        if force_password_prompt:
            audit.add_log(actor, "EMPLOYEE_LOGIN_FIRST_TIME_SUCCESS", "Employee Portal",
                          "First time / initial password login. Prompting to keep or change password.", ip)
            return {"status": "first_time_modal", "token": token, "employee": _employee_payload(employee)}
        audit.add_log(actor, "EMPLOYEE_LOGIN_SUCCESS", "Employee Portal", "Employee logged in and synced successfully", ip)
        return {"status": "ok", "token": token, "employee": _employee_payload(employee)}
    except Exception as err:
        return public_error(err, "Employee sign-in could not be completed. Retry; contact support if it continues.")


@router.get("/validate")
def validate(employee: dict = Depends(require_employee)):
    return {
        "employee": {
            "employee_id": employee["employee_id"],
            "full_name": employee["full_name"],
            "email": employee["email"],
            "department": employee["department"],
            "role": employee["role"],
            "is_first_login": employee["is_first_login"],
            "product": format_product_display_name(employee.get("product")),
            "product_qb_eligible": accounts.is_product_qb_employee(employee),
            "assessment_only": accounts.is_assessment_only_employee(employee),
        }
    }


@router.post("/set-password")
def set_password(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"auth:ip:{ip}"], 8, 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many attempts. Please try again later.")
    try:
        # Must already hold a first-login session token (from password login or SSO).
        session = None
        try:
            from core.auth import require_employee
            session = require_employee(request)
        except Exception:
            return json_error(401, "Sign in first, then set your password.")
        employee_id = as_employee_id(body.get("employee_id")) or session["employee_id"]
        if employee_id.upper() != session["employee_id"].upper():
            return json_error(403, "Token does not match this employee.")
        password = as_password(body.get("password"))
        if not password or not _strong(password):
            return json_error(400, "Password does not meet the strength requirements")
        if password.upper() == session["employee_id"].strip().upper():
            return json_error(400, "Password cannot be the same as your Employee ID.")
        account_limit = is_rate_limited_any([f"auth:acct:{employee_id.upper()}"], 5, 60_000)
        if account_limit.limited:
            return rate_limited_response(account_limit, "Too many attempts. Please try again later.")
        employee = accounts.get_account(employee_id)
        if not employee:
            return json_error(404, "Employee not found")
        if not employee["is_first_login"]:
            return json_error(400, "Password setup has already been completed")
        if not accounts.save_password(employee["employee_id"], password):
            return json_error(500, "Failed to save password")
        return {"status": "ok", "token": sign_token(employee["employee_id"]), "employee": _employee_payload(employee)}
    except Exception as err:
        return public_error(err, "Unable to set password")


@router.post("/confirm-password")
def confirm_password(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"auth:ip:{ip}"], 8, 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many attempts. Please try again later.")
    employee_id = verify_token(bearer_token(request))
    employee = accounts.get_account(employee_id) if employee_id else None
    if not employee:
        return json_error(401, "Unauthorized access or expired session.")
    try:
        action = (as_trimmed(body.get("action"), 20) or "keep").lower()
        token = bearer_token(request)
        if action == "keep":
            accounts.complete_first_login(employee["employee_id"])
            return {"status": "ok", "message": "Initial password retained.", "token": token, "employee": _employee_payload(employee)}
        if action == "change":
            new_password = as_password(body.get("password"))
            otp = as_trimmed(body.get("otp"), 6)
            if not new_password:
                return json_error(400, "Please enter a new password.")
            if not _strong(new_password):
                return json_error(400, "Password must be at least 8 characters long, contain uppercase, lowercase, number, and special character.")
            if not re.fullmatch(r"\d{6}", otp) or not employee_otp.verify_code(employee, "change", otp):
                return json_error(400, "The verification code is invalid or expired.")
            if not accounts.save_password(employee["employee_id"], new_password):
                return json_error(500, "Failed to update password.")
            return {"status": "ok", "message": "Password updated successfully.", "token": token, "employee": _employee_payload(employee)}
        return json_error(400, "Invalid action type.")
    except Exception as err:
        return public_error(err, "Failed to process request.")


@router.post("/reset-password")
def reset_password(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"auth:ip:{ip}"], 8, 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many attempts. Please try again later.")
    try:
        employee_id = as_employee_id(body.get("employee_id"))
        otp = as_trimmed(body.get("otp"), 6)
        password = as_password(body.get("password"))
        if not employee_id:
            return json_error(400, "Employee ID is required")
        if password is None:
            return json_error(400, "Invalid request")
        if not re.fullmatch(r"\d{6}", otp):
            return json_error(400, "Enter the 6-digit verification code.")
        account_limit = is_rate_limited_any([f"auth:acct:{employee_id.upper()}"], 5, 60_000)
        if account_limit.limited:
            return rate_limited_response(account_limit, "Too many attempts. Please try again later.")
        employee = accounts.get_account(employee_id)
        if not employee:
            return json_error(404, "Employee ID not found")
        if not _strong(password):
            return json_error(400, "Password does not meet the strength requirements (Min 8 chars, 1 uppercase, 1 lowercase, 1 number, 1 special char)")
        if password.upper() == employee["employee_id"].strip().upper():
            return json_error(400, "Password cannot be the same as your Employee ID.")
        if not employee_otp.verify_code(employee, "reset", otp):
            return json_error(400, "The verification code is invalid or expired.")
        if not accounts.save_password(employee["employee_id"], password):
            return json_error(500, "Unable to reset password")
        return {"status": "ok", "message": "Password reset successful"}
    except Exception as err:
        return public_error(err, "Unable to reset password")


@router.post("/request-password-reset-otp")
def request_password_reset_otp(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"auth:ip:{ip}"], 8, 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many attempts. Please try again later.")
    try:
        employee_id = as_employee_id(body.get("employee_id"))
        email = as_email(body.get("email"))
        if not employee_id or not email:
            return json_error(400, "Employee ID and registered email are required.")
        account_limit = is_rate_limited_any([f"auth:otp:{employee_id.upper()}"], 3, 10 * 60_000)
        if account_limit.limited:
            return rate_limited_response(account_limit, "Too many verification requests. Please try again later.")
        employee = accounts.get_account(employee_id)
        if not employee or employee.get("email", "").lower().strip() != email:
            return json_error(400, "Employee ID or registered email could not be verified.")
        if not employee_otp.send_code(employee, "reset"):
            return json_error(503, "Unable to deliver the verification email. Please contact your administrator.")
        audit.add_log(email, "EMPLOYEE_PASSWORD_RESET_OTP_SENT", "Employee Portal", "Password reset verification code sent", ip)
        return {"status": "ok", "message": "A verification code has been sent to your registered email."}
    except Exception as err:
        return public_error(err, "Unable to send verification code")


@router.post("/request-password-change-otp")
def request_password_change_otp(request: Request, employee: dict = Depends(require_employee)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"auth:ip:{ip}"], 8, 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many attempts. Please try again later.")
    account_limit = is_rate_limited_any([f"auth:otp:{employee['employee_id'].upper()}"], 3, 10 * 60_000)
    if account_limit.limited:
        return rate_limited_response(account_limit, "Too many verification requests. Please try again later.")
    try:
        if not employee_otp.send_code(employee, "change"):
            return json_error(503, "Unable to deliver the verification email. Please contact your administrator.")
        audit.add_log(employee.get("email") or employee["employee_id"], "EMPLOYEE_PASSWORD_CHANGE_OTP_SENT",
                      "Employee Portal", "Password change verification code sent", ip)
        return {"status": "ok", "message": "A verification code has been sent to your registered email."}
    except ValueError as err:
        return json_error(400, str(err))
    except Exception as err:
        return public_error(err, "Unable to send verification code")


@router.post("/profile")
def update_profile(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"auth:ip:{ip}"], 8, 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many attempts. Please try again later.")
    employee_id = verify_token(bearer_token(request))
    employee = accounts.get_account(employee_id) if employee_id else None
    if not employee:
        return json_error(401, "Unauthorized access or expired session.")
    account_limit = is_rate_limited_any([f"auth:acct:{employee['employee_id'].upper()}"], 8, 60_000)
    if account_limit.limited:
        return rate_limited_response(account_limit, "Too many attempts. Please try again later.")
    try:
        full_name = as_trimmed(body.get("full_name") or body.get("fullName"), 120)
        requested_id = as_trimmed(body.get("employee_id") or body.get("employeeId"), 40)
        if not full_name:
            return json_error(400, "Name is required.")
        updated, err = accounts.update_profile(employee["employee_id"], full_name, requested_id)
        if err or not updated:
            status = 400 if err and ("cannot be changed" in err.lower() or "already" in err.lower() or "required" in err.lower()) else 500
            return json_error(status, err or "Failed to update profile.")
        actor = updated.get("email") or updated["employee_id"]
        audit.add_log(
            actor,
            "EMPLOYEE_PROFILE_UPDATE",
            "Employee Portal",
            f"Profile name updated to {updated['full_name']}.",
            ip,
        )
        token = sign_token(updated["employee_id"])
        return {
            "status": "ok",
            "message": "Profile updated.",
            "token": token,
            "employee": _employee_payload(updated),
        }
    except Exception as err:
        return public_error(err, "Failed to update profile.")


@router.post("/change-password")
def change_password(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"auth:ip:{ip}"], 8, 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many attempts. Please try again later.")
    employee_id = verify_token(bearer_token(request))
    employee = accounts.get_account(employee_id) if employee_id else None
    if not employee:
        return json_error(401, "Unauthorized access or expired session.")
    account_limit = is_rate_limited_any([f"auth:acct:{employee['employee_id'].upper()}"], 5, 60_000)
    if account_limit.limited:
        return rate_limited_response(account_limit, "Too many attempts. Please try again later.")
    try:
        new_password = as_password(body.get("newPassword"))
        otp = as_trimmed(body.get("otp"), 6)
        if not new_password or not re.fullmatch(r"\d{6}", otp):
            return json_error(400, "A new password and 6-digit verification code are required.")
        actor = employee["email"] or employee["employee_id"]
        if not _strong(new_password):
            return json_error(400, "Password must be at least 8 characters long, contain uppercase, lowercase, number, and special character.")
        if new_password.upper() == employee["employee_id"].strip().upper():
            return json_error(400, "Password cannot be the same as your Employee ID.")
        if accounts.has_password(employee) and accounts.verify_password(
            new_password, employee.get("password_salt") or "", employee.get("password_hash") or ""
        ):
            return json_error(400, "New password must be different from the current one.")
        if not employee_otp.verify_code(employee, "change", otp):
            audit.add_log(actor, "EMPLOYEE_PASSWORD_CHANGE_FAILURE", "Employee Portal", "Invalid or expired OTP", ip)
            return json_error(400, "The verification code is invalid or expired.")
        if not accounts.save_password(employee["employee_id"], new_password):
            return json_error(500, "Failed to update password.")
        audit.add_log(actor, "EMPLOYEE_PASSWORD_CHANGE_SUCCESS", "Employee Portal", "Employee changed their password", ip)
        return {"status": "ok", "message": "Password updated successfully."}
    except Exception as err:
        return public_error(err, "Failed to update password.")


@router.get("/microsoft/handoff")
def handoff(request: Request):
    token = request.cookies.get(HANDOFF_COOKIE) or ""
    ok = bool(token) and verify_token(token) is not None
    response = JSONResponse(
        {"status": "ok", "token": token} if ok else {"error": "Microsoft sign-in session expired. Please try again.", "message": "Microsoft sign-in session expired. Please try again."},
        status_code=200 if ok else 401,
    )
    response.delete_cookie(HANDOFF_COOKIE, path="/")
    return response


def _sso_error(app: str, message: str, status: int = 307, target: str = "employee") -> RedirectResponse:
    return RedirectResponse(f"{app}/{target}?sso_error={quote(message, safe='')}", status_code=status)


@router.get("/saml/status")
def saml_status():
    cfg = saml.get_config()
    return JSONResponse({"enabled": cfg["enabled"], "adminSso": cfg["enabled"] and cfg["admin_sso"]}, headers={"Cache-Control": "no-store"})


@router.post("/logout")
def logout(request: Request):
    """Clear TalentScope session cookies and return a Microsoft sign-out URL.

    Browser clients should clear local tokens, then navigate to microsoftLogoutUrl
    so the Entra SSO cookie is ended and the next Infinite Login prompts again.
    """
    cfg = saml.get_config()
    reason = str(request.query_params.get("reason") or "").strip()
    return_path = "/employee"
    if reason:
        return_path = f"/employee?reason={quote(reason, safe='')}"
    payload = {
        "status": "ok",
        "microsoftLogoutUrl": saml.idp_logout_url(return_path, cfg) if cfg.get("enabled") else "",
        "returnTo": f"{cfg['app_url'].rstrip('/')}{return_path}",
    }
    response = JSONResponse(payload, headers={"Cache-Control": "no-store"})
    response.delete_cookie(HANDOFF_COOKIE, path="/")
    response.delete_cookie(ADMIN_HANDOFF_COOKIE, path="/")
    return response


@router.get("/saml")
def saml_start(request: Request):
    cfg = saml.get_config()
    ip = get_client_ip(request)
    target = "admin" if request.query_params.get("target") == "admin" else "employee"
    if is_rate_limited(f"employee_saml_login_{ip}", 12, 60_000).limited:
        return _sso_error(cfg["app_url"], "Too many sign-in attempts. Try again shortly.", target=target)
    if not cfg["enabled"]:
        return _sso_error(cfg["app_url"], saml.not_configured_message(cfg["missing"]) if cfg["missing"] else "Microsoft sign-in is turned off.", target=target)
    if target == "admin" and not cfg["admin_sso"]:
        return _sso_error(cfg["app_url"], "Microsoft sign-in is not enabled for the admin console.", target=target)
    try:
        return RedirectResponse(saml.start_login(target, f"/{target}", cfg), status_code=302)
    except Exception as err:
        log.error("SAML login redirect error: %s", err)
        return _sso_error(cfg["app_url"], "Could not start Microsoft sign-in.", target=target)


def _admin_identity(emails: list[str]) -> Optional[str]:
    for email in emails:
        if not email.endswith("@infinite.com"):
            continue
        if (
            admin_accounts.is_super_admin(email)
            or admin_accounts.get_named_record(email)
            or admin_accounts.is_screening_only(email)
            or admin_accounts.find_named_by_alias(email)
        ):
            # Prefer the canonical named-account email when matched via alias.
            named = admin_accounts.get_named_record(email) or admin_accounts.find_named_by_alias(email)
            return (named and named.get("_email")) or email
    return None


def _finish_admin(cfg: dict, profile: dict, emails: list[str], ip: str):
    email = _admin_identity(emails)
    if not email:
        tried = ", ".join(emails) if emails else str(profile.get("nameID") or "unknown")
        audit.add_log(emails[0] if emails else str(profile.get("nameID") or "unknown"), "ADMIN_SAML_SSO_FAILURE", "Admin Console",
                      f"SAML identity has no admin access ({tried}).", ip)
        return _sso_error(
            cfg["app_url"],
            f"Microsoft signed you in as {tried}, but that email is not in Admin Panel Access. "
            "Add that exact Entra UPN under Portal Config (or as an alias on an existing admin) and try again.",
            303,
            "admin",
        )
    audit.add_log(email, "ADMIN_SAML_SSO_SUCCESS", "Admin Console", "SAML SSO login to the admin console.", ip)
    response = RedirectResponse(f"{cfg['app_url']}/admin/sso-complete", status_code=303)
    response.set_cookie(ADMIN_HANDOFF_COOKIE, sign_token("admin", 60 * 60 * 1000, email=email), max_age=120, httponly=True,
                        samesite="lax", secure=_cookie_secure(), path="/")
    return response


def _finish_employee(cfg: dict, profile: dict, emails: list[str], ip: str):
    employee = None
    for email in emails:
        employee = accounts.get_account_by_email(email)
        if employee:
            break
    if not employee:
        local = saml.profile_local_part(profile, cfg)
        employee = accounts.get_account(local) if local else None
    if not employee:
        audit.add_log(emails[0] if emails else str(profile.get("nameID") or "unknown"), "EMPLOYEE_SAML_SSO_FAILURE",
                      "Employee Portal", "SAML identity is not linked to an Employee Portal roster email or ID.", ip)
        return _sso_error(cfg["app_url"], "No Employee Portal account is linked to this Microsoft email. Sign in with your Employee ID or ask HR to map your work email.", 303)
    accounts.sync_account(employee)
    audit.add_log(employee["email"] or (emails[0] if emails else employee["employee_id"]), "EMPLOYEE_SAML_SSO_SUCCESS",
                  "Employee Portal", f"SAML SSO login as {employee['full_name']} ({employee['employee_id']}).", ip)
    response = RedirectResponse(f"{cfg['app_url']}/employee/sso-complete", status_code=303)
    response.set_cookie(HANDOFF_COOKIE, sign_token(employee["employee_id"]), max_age=120, httponly=True, samesite="lax",
                        secure=_cookie_secure(), path="/")
    return response


@router.post("/saml/acs")
async def saml_acs(request: Request):
    cfg = saml.get_config()
    ip = get_client_ip(request)
    if is_rate_limited(f"employee_saml_acs_{ip}", 20, 60_000).limited:
        return _sso_error(cfg["app_url"], "Too many sign-in attempts. Try again shortly.", 303)
    if not cfg["enabled"]:
        return _sso_error(cfg["app_url"], saml.not_configured_message(cfg["missing"]) if cfg["missing"] else "Microsoft sign-in is turned off.", 303)
    target = "employee"
    try:
        form = await request.form()
        saml_response = str(form.get("SAMLResponse") or "")
        if not saml_response:
            return _sso_error(cfg["app_url"], "Microsoft sign-in did not return a SAML response.", 303)
        request_id = saml.response_request_id(saml_response)
        pending = saml.consume_pending(request_id)
        if pending is None and (request_id or not cfg["allow_idp_initiated"]):
            audit.add_log("unknown", "SAML_SSO_REJECTED", "SSO", "SAML response did not match a login started from Talentscope.", ip)
            return _sso_error(cfg["app_url"], "Start Microsoft sign-in from the Talentscope login page and try again.", 303)
        target = (pending or {}).get("target", "employee")
        profile = saml.validate_post_response(saml_response, cfg, expected_request_id=request_id if pending else None)
        emails = saml.profile_emails(profile, cfg)
        return _finish_admin(cfg, profile, emails, ip) if target == "admin" else _finish_employee(cfg, profile, emails, ip)
    except saml.SamlError as err:
        log.warning("SAML ACS rejected: %s", err)
        audit.add_log("unknown", "SAML_SSO_REJECTED", "SSO", str(err)[:200], ip)
        return _sso_error(cfg["app_url"], SSO_FAILED, 303, target)
    except Exception as err:
        log.error("SAML ACS error: %s", err)
        return _sso_error(cfg["app_url"], SSO_FAILED, 303, target)


@router.get("/saml/metadata")
def saml_metadata():
    cfg = saml.get_config()
    if not cfg["issuer"]:
        return json_error(503, "Set SAML_ISSUER to generate service provider metadata.")
    return Response(saml.build_metadata(cfg), media_type="application/xml; charset=utf-8", headers={"Cache-Control": "no-store"})
