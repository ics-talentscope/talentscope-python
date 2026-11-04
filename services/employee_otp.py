import html
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from core.db import get_store
from core.tokens import otp_digest
from services import accounts, mailer

log = logging.getLogger("talentscope")
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


def send_code(employee: dict, purpose: str) -> bool:
    if purpose not in ("reset", "change"):
        raise ValueError("Invalid OTP purpose.")
    email = str(employee.get("email") or "").strip()
    if not email:
        raise ValueError("No email address is registered for this account.")
    employee_uuid = accounts.employee_uuid(employee["employee_id"])
    if not employee_uuid:
        raise RuntimeError("Could not resolve employee account.")
    store = get_store()
    pending = store.select("employee_auth_otps", {"employee_id": employee_uuid, "purpose": purpose, "consumed": False})
    for challenge in pending:
        store.update("employee_auth_otps", {"consumed": True}, {"id": challenge["id"]})
    challenge_id = str(uuid.uuid4())
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = datetime.now(timezone.utc)
    store.insert("employee_auth_otps", {
        "id": challenge_id,
        "employee_id": employee_uuid,
        "purpose": purpose,
        "email": email,
        "code_hash": otp_digest(code, challenge_id, purpose),
        "expires_at": (now + timedelta(minutes=OTP_TTL_MINUTES)).isoformat(),
        "attempts": 0,
        "consumed": False,
        "created_at": now.isoformat(),
    })
    action = "reset your password" if purpose == "reset" else "change your password"
    subject = "TalentScope password verification code"
    html_body = (
        f"<p>Hello {html.escape(employee.get('full_name') or employee['employee_id'])},</p>"
        f"<p>Use this verification code to {action} in the TalentScope Employee Portal:</p>"
        f"<p style=\"font-size:24px;font-weight:bold;letter-spacing:5px\">{code}</p>"
        f"<p>This code expires in {OTP_TTL_MINUTES} minutes. If you did not request it, you can ignore this email.</p>"
    )
    status, error = mailer.dispatch(email, subject, html_body)
    if status != "sent":
        store.update("employee_auth_otps", {"consumed": True}, {"id": challenge_id})
        log.error("Employee password OTP delivery failed for %s: %s", employee["employee_id"], error or status)
        return False
    return True


def verify_code(employee: dict, purpose: str, code: str) -> bool:
    employee_uuid = accounts.employee_uuid(employee["employee_id"])
    if not employee_uuid:
        return False
    challenges = get_store().select(
        "employee_auth_otps",
        {"employee_id": employee_uuid, "purpose": purpose, "consumed": False},
        order="created_at desc",
        limit=1,
    )
    if not challenges:
        return False
    challenge = challenges[0]
    try:
        expires_at = challenge["expires_at"]
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        get_store().update("employee_auth_otps", {"consumed": True}, {"id": challenge["id"]})
        log.error("Invalid OTP expiry value for challenge %s", challenge["id"])
        return False
    if expiry <= datetime.now(timezone.utc) or int(challenge.get("attempts") or 0) >= OTP_MAX_ATTEMPTS:
        get_store().update("employee_auth_otps", {"consumed": True}, {"id": challenge["id"]})
        return False
    if str(challenge.get("email") or "").strip().casefold() != str(employee.get("email") or "").strip().casefold():
        get_store().update("employee_auth_otps", {"consumed": True}, {"id": challenge["id"]})
        return False
    valid = secrets.compare_digest(
        str(challenge.get("code_hash") or ""),
        otp_digest(code, challenge["id"], purpose),
    )
    if not valid:
        attempts = int(challenge.get("attempts") or 0) + 1
        get_store().update(
            "employee_auth_otps",
            {"attempts": attempts, "consumed": attempts >= OTP_MAX_ATTEMPTS},
            {"id": challenge["id"]},
        )
        return False
    get_store().update("employee_auth_otps", {"consumed": True}, {"id": challenge["id"]})
    get_store().update("employees", {"email_verified": True}, {"id": employee_uuid})
    return True
