import base64
import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.db import get_store

log = logging.getLogger("talentscope")

PBKDF2_ITERATIONS = 120_000
PBKDF2_KEY_LENGTH = 64
PRODUCT_ASSESSMENT_TOPIC_ID = "resource-product-assessment"
PRODUCT_ASSESSMENT_HISTORY_TOPIC_ID = "resource-product-assessment-history"

COLUMNS = [
    "employee_id", "full_name", "email", "department", "role", "is_first_login", "password_hash", "password_salt",
    "xp_points", "streak_days", "skill_level", "ai_readiness_score", "product", "designation", "product_qb_eligible",
    "assessment_only", "email_verified",
]


def hash_password(password: str) -> tuple[str, str]:
    salt = base64.b64encode(os.urandom(16)).decode("ascii")
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS, PBKDF2_KEY_LENGTH)
    return base64.b64encode(digest).decode("ascii"), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    if not salt or not expected_hash:
        return False
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS, PBKDF2_KEY_LENGTH)
    return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), expected_hash)


def _int(value, default=0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def map_row(row: dict) -> dict:
    return {
        "employee_id": str(row.get("employee_id") or ""),
        "full_name": str(row.get("full_name") or row.get("employee_id") or ""),
        "email": str(row.get("email") or ""),
        "department": str(row.get("department") or "general"),
        "role": str(row.get("role") or "employee"),
        "designation": str(row.get("designation") or ""),
        "is_first_login": bool(row.get("is_first_login") or False),
        "email_verified": bool(row.get("email_verified") or False),
        "password_hash": row.get("password_hash") or None,
        "password_salt": row.get("password_salt") or None,
        "xp_points": _int(row.get("xp_points")),
        "streak_days": _int(row.get("streak_days")),
        "skill_level": str(row.get("skill_level") or "beginner"),
        "ai_readiness_score": _int(row.get("ai_readiness_score")),
        "product": row.get("product") or None,
        "product_qb_eligible": bool(row.get("product_qb_eligible") or False),
        "assessment_only": bool(row.get("assessment_only") or False),
    }


def has_password(account: dict) -> bool:
    return bool(account.get("password_hash") and account.get("password_salt"))


def is_product_qb_employee(account: dict) -> bool:
    return account.get("role") != "admin" and account.get("product_qb_eligible") is True


def is_assessment_only_employee(account: dict) -> bool:
    return account.get("role") != "admin" and account.get("assessment_only") is True


def is_product_assessment_history_topic(topic_id: Optional[str]) -> bool:
    return str(topic_id or "").strip().startswith(PRODUCT_ASSESSMENT_HISTORY_TOPIC_ID)


def get_account(employee_id: str) -> Optional[dict]:
    row = get_store().first("employees", {"employee_id": employee_id.strip()}, COLUMNS)
    return map_row(row) if row else None


def _email_lookup_candidates(email: str) -> list[str]:
    """Exact email plus Infinite <-> Nokia.ext roster aliases used by SSO."""
    clean = (email or "").strip().lower()
    if not clean or "@" not in clean:
        return [clean] if clean else []
    local, _, domain = clean.partition("@")
    out = [clean]
    if domain == "infinite.com":
        out.append(f"{local}.ext@nokia.com")
        out.append(f"{local}@nokia.com")
    elif domain == "nokia.com":
        if local.endswith(".ext"):
            out.append(f"{local[:-4]}@infinite.com")
        else:
            out.append(f"{local}@infinite.com")
            out.append(f"{local}.ext@nokia.com")
    # Preserve order, drop dupes.
    seen: set[str] = set()
    uniq: list[str] = []
    for item in out:
        if item and item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def get_account_by_email(email: str) -> Optional[dict]:
    for candidate in _email_lookup_candidates(email):
        # Case-insensitive match (SQL Server CI collations treat LIKE as CI without wildcards).
        row = get_store().first("employees", [("email", "ilike", candidate)], COLUMNS)
        if row:
            return map_row(row)
    return None


def upsert_account(account: dict) -> bool:
    email = str(account.get("email") or f"{account['employee_id']}@nokia.com").strip().lower()
    payload = {
        "employee_id": account["employee_id"],
        "email": email,
        "full_name": account.get("full_name") or account["employee_id"],
        "department": account.get("department") or "general",
        "role": account.get("role") or "employee",
        "product": account.get("product"),
        "designation": account.get("designation") or "",
        "email_verified": bool(account.get("email_verified", False)),
        "is_first_login": bool(account.get("is_first_login", False)),
        "password_hash": account.get("password_hash"),
        "password_salt": account.get("password_salt"),
        "product_qb_eligible": bool(account.get("product_qb_eligible", False)),
        "assessment_only": bool(account.get("assessment_only", False)),
        "xp_points": account.get("xp_points") or 0,
        "streak_days": account.get("streak_days") or 0,
        "skill_level": account.get("skill_level") or "beginner",
        "ai_readiness_score": account.get("ai_readiness_score") or 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        get_store().upsert("employees", payload, "employee_id")
        return True
    except Exception as err:
        log.error("Failed to upsert employee account: %s", err)
        return False


def add_account(account: dict) -> bool:
    if get_account(account["employee_id"]):
        return False
    return upsert_account(account)


def save_password(employee_id: str, password: str) -> bool:
    account = get_account(employee_id)
    if not account:
        return False
    account["password_hash"], account["password_salt"] = hash_password(password)
    account["is_first_login"] = False
    return upsert_account(account)


def complete_first_login(employee_id: str) -> bool:
    account = get_account(employee_id)
    if not account:
        return False
    account["is_first_login"] = False
    return upsert_account(account)


def update_profile(current_employee_id: str, full_name: str, new_employee_id: str = "") -> tuple[Optional[dict], Optional[str]]:
    """Update display name only. Emp ID changes are admin/HR controlled. Returns (account, error)."""
    account = get_account(current_employee_id)
    if not account:
        return None, "Account not found."
    name = str(full_name or "").strip()[:120]
    if not name:
        return None, "Name is required."
    requested = str(new_employee_id or "").strip()
    if requested and requested.upper() != account["employee_id"].upper():
        return None, "Employee ID cannot be changed here. Ask HR or a TalentScope admin."
    store = get_store()
    now = datetime.now(timezone.utc).isoformat()
    updated = store.update(
        "employees",
        {"full_name": name, "updated_at": now},
        {"employee_id": account["employee_id"]},
    )
    if not updated:
        return None, "Failed to update profile."
    refreshed = get_account(account["employee_id"])
    return (refreshed, None) if refreshed else (None, "Failed to update profile.")


def employee_uuid(employee_id: str) -> Optional[str]:
    row = get_store().first("employees", {"employee_id": employee_id.strip()}, ["id"])
    return row["id"] if row else None


def sync_account(account: dict) -> Optional[str]:
    if not upsert_account(account):
        return None
    return employee_uuid(account["employee_id"])


def new_uuid() -> str:
    return str(uuid.uuid4())
