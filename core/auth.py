from typing import Optional

from fastapi import Depends, Request

from core import tenancy
from core.errors import ApiError
from core.tokens import token_claims, token_email, verify_token


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    return header[7:] if header.startswith("Bearer ") else header


def admin_token(request: Request) -> str:
    token = bearer_token(request)
    if token:
        return token
    # Legacy inline token support for GET-only asset downloads (video, files).
    # Never accept query tokens for state-changing requests.
    if str(getattr(request, "method", "")).upper() == "GET":
        return str((getattr(request, "query_params", {}) or {}).get("token") or "")
    return ""


def is_admin_request(request: Request) -> bool:
    token = admin_token(request)
    return bool(token) and verify_token(token) == "ADMIN"


def require_admin(request: Request) -> str:
    if not is_admin_request(request):
        raise ApiError(401, "Unauthorized")
    email = admin_email(request)
    if not email:
        raise ApiError(401, "Unauthorized")
    claims = token_claims(admin_token(request)) or {}
    expected = tenancy.client_for_email(email)
    claimed = claims.get("client")
    # Reject stale/unknown identities and client claims (e.g. tokens issued
    # before a client rename) instead of silently falling back to Infinite.
    if not expected:
        raise ApiError(401, "Unauthorized")
    if claimed is not None and (claimed not in tenancy.CLIENTS or tenancy.normalize_client(claimed) != expected):
        raise ApiError(401, "Unauthorized")
    # The signed token is authoritative for the request's client environment.
    tenancy.set_client(expected)
    return email


def admin_email(request: Request) -> Optional[str]:
    return token_email(admin_token(request))


def acting_admin_email(request: Request) -> Optional[str]:
    """Return the admin identity used for per-admin data filtering and audit.

    The JWT remains authoritative for authentication and authorization. A
    super admin may use the legacy ``?email=`` parameter to act as one of their
    own named/screening admins for filtered views. This override is
    downward-only: a restricted token can never escalate to another identity.
    """
    token_email = admin_email(request)
    override = str((getattr(request, "query_params", {}) or {}).get("email") or "").strip().lower()
    if not token_email or not override:
        return token_email
    from services import admin_accounts

    if not admin_accounts.is_super_admin(token_email):
        return token_email
    if tenancy.client_for_email(override) != tenancy.client_for_email(token_email):
        return token_email
    allowed = (
        admin_accounts.is_super_admin(override)
        or admin_accounts.get_named_record(override) is not None
        or admin_accounts.find_named_by_alias(override) is not None
        or admin_accounts.is_screening_only(override)
    )
    return override if allowed else token_email


def require_permission(permission: str):
    def check(request: Request, email: str = Depends(require_admin)) -> str:
        from services import admin_accounts

        if not admin_accounts.has_permission(email, permission):
            raise ApiError(403, "You do not have access to this section.")
        return email

    return check


def require_any_permission(*permissions: str):
    def check(request: Request, email: str = Depends(require_admin)) -> str:
        from services import admin_accounts

        if not any(admin_accounts.has_permission(email, p) for p in permissions):
            raise ApiError(403, "You do not have access to this section.")
        return email

    return check


def require_slalom_bootcamp_admin(request: Request, email: str = Depends(require_admin)) -> str:
    """Require an authenticated Slalom admin with bootcamp management access."""
    from services import admin_accounts

    if not admin_accounts.can_manage_slalom_bootcamp(email):
        raise ApiError(403, "Slalom bootcamp management access is required.")
    if tenancy.current_client() != "slalom":
        raise ApiError(403, "Slalom bootcamp management access is required.")
    return email


def employee_id_from_request(request: Request) -> Optional[str]:
    return verify_token(bearer_token(request))


def require_employee(request: Request) -> dict:
    from services import accounts

    employee_id = employee_id_from_request(request)
    if not employee_id or employee_id == "ADMIN" or employee_id == "INTERVIEW":
        raise ApiError(401, "Unauthorized")
    employee = accounts.get_account(employee_id)
    if not employee:
        raise ApiError(401, "Unauthorized")
    return employee
