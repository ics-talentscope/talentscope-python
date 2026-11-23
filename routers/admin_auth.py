from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from core.auth import acting_admin_email, admin_email, require_admin
from core.errors import ApiError, json_error, public_error
from core.security import get_client_ip, is_rate_limited, is_rate_limited_any, rate_limited_response
from core.tokens import sign_token
from core.validation import as_email, as_password
from services import admin_accounts, audit

router = APIRouter(prefix="/api/admin/auth", tags=["admin-auth"])

ADMIN_HANDOFF_COOKIE = "ms_admin_sso_handoff"


@router.post("/login")
def login(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"auth:ip:{ip}"], 8, 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many login attempts. Please try again later.")
    try:
        email = as_email(body.get("email"))
        password = as_password(body.get("password"), allow_empty=True)
        if password is None:
            return json_error(400, "Invalid request")
        if not email or not admin_accounts.is_admin_email(email):
            audit.add_login_log(email or "unknown", "ADMIN_LOGIN_FAILURE", "Admin Console", "Unauthorized email domain extension", ip)
            return json_error(400, "Unauthorized domain. Use your @infinite.com or @slalom.com email.")
        account_limit = is_rate_limited_any([f"auth:acct:{email}"], 5, 60_000)
        if account_limit.limited:
            return rate_limited_response(account_limit, "Too many login attempts. Please try again later.")
        try:
            access = admin_accounts.authenticate(email, password)
        except admin_accounts.AdminPasswordNotSetup:
            audit.add_login_log(email, "ADMIN_LOGIN_FAILURE", "Admin Console", admin_accounts.ADMIN_PASSWORD_NOT_SETUP, ip)
            return json_error(503, admin_accounts.ADMIN_PASSWORD_NOT_SETUP)
        if not access:
            audit.add_login_log(email, "ADMIN_LOGIN_FAILURE", "Admin Console", "Invalid password provided", ip)
            return json_error(401, "Invalid Password")
        token = sign_token("admin", 60 * 60 * 1000, email=access["email"], client=access["client"])
        audit.add_login_log(
            access["email"], "ADMIN_LOGIN_SUCCESS", "Admin Console",
            f"{access['client']} admin session generated successfully" if access["canViewEmployeePortal"]
            else f"{access['client']} admin session generated without Employee Portal access", ip,
        )
        return {"status": "ok", "token": token, "email": access["email"], "client": access["client"],
                "isSuperAdmin": access["isSuperAdmin"], "permissions": access["permissions"],
                "canViewEmployeePortal": access["canViewEmployeePortal"],
                "canChangePassword": access["canChangePassword"]}
    except Exception as err:
        return public_error(err, "Unable to sign in")


@router.post("/refresh")
def refresh(request: Request, _: str = Depends(require_admin)):
    """Swap a still-valid admin token for a fresh one so active admins are not signed out mid-task."""
    email = admin_email(request)
    if not email:
        return json_error(401, "Unauthorized")
    access = admin_accounts.get_access(email)
    if not any(access["permissions"].values()):
        return json_error(401, "Unauthorized")
    token = sign_token("admin", 60 * 60 * 1000, email=access["email"], client=access["client"])
    return {"status": "ok", "token": token, "client": access["client"], "isSuperAdmin": access["isSuperAdmin"],
            "permissions": access["permissions"], "canViewEmployeePortal": access["canViewEmployeePortal"],
            "canChangePassword": access["canChangePassword"]}


@router.get("/validate")
def validate(request: Request, _: str = Depends(require_admin)):
    email = acting_admin_email(request)
    if not email:
        return {"status": "ok"}
    access = admin_accounts.get_access(email)
    return {"status": "ok", "email": access["email"], "client": access["client"],
            "isSuperAdmin": access["isSuperAdmin"], "permissions": access["permissions"],
            "canViewEmployeePortal": access["canViewEmployeePortal"], "canChangePassword": access["canChangePassword"]}


@router.post("/change-password")
def change_password(request: Request, body: dict = Body(default_factory=dict), _: str = Depends(require_admin)):
    ip = get_client_ip(request)
    limit = is_rate_limited(f"admin_change_password_{ip}", 5, 60_000)
    if limit.limited:
        return rate_limited_response(limit, "Too many attempts. Please try again later.")
    try:
        email = admin_email(request) or ""
        current = as_password(body.get("currentPassword") if isinstance(body.get("currentPassword"), str) else "", True)
        new = as_password(body.get("newPassword") if isinstance(body.get("newPassword"), str) else "", True)
        if current is None or new is None:
            return json_error(400, "Invalid request")
        if not email or not admin_accounts.can_change_password(email):
            audit.add_log(email or "unknown", "ADMIN_PASSWORD_CHANGE_DENIED", "Admin Console",
                          "Account is not allowed to change password", ip)
            return json_error(403, "This account cannot change its password here.")
        try:
            ok, error = admin_accounts.change_password(email, current, new)
        except admin_accounts.AdminPasswordNotSetup:
            return json_error(503, admin_accounts.ADMIN_PASSWORD_NOT_SETUP)
        if not ok:
            audit.add_log(email, "ADMIN_PASSWORD_CHANGE_FAILURE", "Admin Console", error, ip)
            return json_error(400, error)
        audit.add_log(email, "ADMIN_PASSWORD_CHANGE_SUCCESS", "Admin Console", "Named admin password updated", ip)
        return {"status": "ok"}
    except Exception as err:
        return public_error(err, "Failed to change password")


@router.post("/verify-supervisor")
def verify_supervisor(request: Request, body: dict = Body(default_factory=dict), _: str = Depends(require_admin)):
    ip = get_client_ip(request)
    limit = is_rate_limited(f"admin_supervisor_auth_{ip}", 10, 60_000)
    if limit.limited:
        return rate_limited_response(limit, "Too many supervisor authorization attempts. Please try again later.")
    password = str(body.get("password") or "").strip()
    if not password:
        return json_error(400, "Supervisor password is required.")
    try:
        actor = admin_email(request) or "unknown"
        if not admin_accounts.verify_supervisor(password):
            audit.add_log(actor, "ADMIN_SUPERVISOR_AUTH_FAILURE", "Admin Console", "Invalid supervisor password provided", ip)
            return json_error(401, "Invalid supervisor password.")
        audit.add_log(actor, "ADMIN_SUPERVISOR_AUTH_SUCCESS", "Admin Console", "Supervisor action authorized", ip)
        return {"status": "ok", "valid": True}
    except admin_accounts.AdminPasswordNotSetup as err:
        return json_error(503, str(err))
    except Exception as err:
        return public_error(err, "Failed to verify supervisor password")



@router.get("/saml/handoff")
def admin_sso_handoff(request: Request):
    from core.tokens import token_claims

    token = request.cookies.get(ADMIN_HANDOFF_COOKIE) or ""
    claims = token_claims(token)
    ok = bool(claims and claims.get("employee_id") == "ADMIN" and claims.get("email"))
    if ok:
        access = admin_accounts.get_access(claims["email"])
        body = {"status": "ok", "token": token, "email": access["email"], "client": access["client"],
                "isSuperAdmin": access["isSuperAdmin"], "permissions": access["permissions"],
                "canViewEmployeePortal": access["canViewEmployeePortal"],
                "canChangePassword": access["canChangePassword"]}
    else:
        body = {"error": "Microsoft sign-in session expired. Please try again."}
    response = JSONResponse(body, status_code=200 if ok else 401, headers={"Cache-Control": "no-store"})
    response.delete_cookie(ADMIN_HANDOFF_COOKIE, path="/")
    return response


@router.get("/recent-logins")
def recent_logins(request: Request, _: str = Depends(require_admin)):
    """Who signed into the admin console recently (password + Microsoft SSO)."""
    try:
        raw = request.query_params.get("limit")
        limit = int(raw) if raw not in (None, "") else 25
    except (TypeError, ValueError):
        limit = 25
    return {"logins": audit.recent_admin_logins(limit)}


@router.get("/sso/check")
def sso_check(request: Request, _: str = Depends(require_admin)):
    from services import saml

    cfg = saml.get_config()
    return {"enabled": cfg["enabled"], "configured": cfg["configured"], "missing": cfg["missing"], "adminSso": cfg["admin_sso"],
            "idpInitiatedAllowed": cfg["allow_idp_initiated"], "issuer": cfg["issuer"], "callbackUrl": cfg["callback_url"],
            "idpEntityId": cfg["idp_entity_id"], "metadataUrl": cfg["metadata_url"], "metadataError": cfg["metadata_error"], "signedRequests": bool(cfg["sp_private_key"]), "certificates": saml.cert_expiry(cfg),
            "spMetadataUrl": f"{cfg['app_url']}/api/employee/auth/saml/metadata"}
