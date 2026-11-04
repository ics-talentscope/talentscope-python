import hashlib
import hmac
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.db import get_store

ACCESS_CODE_TTL_SECONDS = 600
ACCESS_CODE_MAX_ATTEMPTS = 5
CANDIDATE_PASSWORD_ITERATIONS = 180_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _map(row: dict) -> dict:
    code = row.get("code") or None
    return {
        "code": None if str(code or "").startswith("otp:") else code,
        "email": row.get("email") or None,
        "createdAt": row.get("created_at") or _now(),
        "used": bool(row.get("used")),
        "resumeId": row.get("resume_id") or None,
        "usedAt": row.get("used_at") or None,
    }


def create_session() -> dict:
    code = str(uuid.uuid4()).split("-")[0].upper()
    row = get_store().insert("candidate_sessions", {"code": code, "used": False, "created_at": _now()})[0]
    return _map(row)


def get_session(code: str) -> Optional[dict]:
    row = get_store().first("candidate_sessions", {"code": code.upper().strip()})
    return _map(row) if row else None


def get_session_by_email(email: str) -> Optional[dict]:
    row = get_store().first("candidate_sessions", {"email": email.lower().strip()})
    return _map(row) if row else None


def mark_session_used(code: str, resume_id: str) -> dict:
    store = get_store()
    clean = code.upper().strip()
    if not store.update("candidate_sessions", {"used": True, "resume_id": resume_id, "used_at": _now()}, {"code": clean}):
        raise RuntimeError("Failed to update session: Not found")
    return get_session(clean)


def create_candidate_session(email: str, resume_id: str) -> dict:
    clean = email.lower().strip()
    row = get_store().upsert(
        "candidate_sessions",
        {"email": clean, "resume_id": resume_id, "code": None, "used": False, "created_at": _now()},
        "email",
    )
    return _map(row)


def issue_candidate_password(email: str, resume_id: str) -> str:
    clean = email.lower().strip()
    password = secrets.token_urlsafe(12)
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), CANDIDATE_PASSWORD_ITERATIONS
    ).hex()
    row = get_store().upsert(
        "candidate_sessions",
        {
            "email": clean,
            "resume_id": resume_id,
            "code": None,
            "used": False,
            "used_at": None,
            "password_hash": digest,
            "password_salt": salt,
            "created_at": _now(),
        },
        "email",
    )
    if not row:
        raise RuntimeError("Could not save candidate login credentials.")
    return password


def verify_candidate_password(email: str, password: str) -> Optional[dict]:
    row = get_store().first("candidate_sessions", {"email": email.lower().strip()})
    if not row or row.get("used") or not row.get("resume_id"):
        return None
    stored_hash, salt = row.get("password_hash"), row.get("password_salt")
    if not stored_hash or not salt:
        return None
    actual_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), CANDIDATE_PASSWORD_ITERATIONS
    ).hex()
    return _map(row) if hmac.compare_digest(stored_hash, actual_hash) else None


def issue_access_code(email: str) -> Optional[tuple[str, str]]:
    store = get_store()
    row = store.first("candidate_sessions", {"email": email.lower().strip()})
    if not row or row.get("used") or not row.get("resume_id"):
        return None
    code = f"{secrets.randbelow(1_000_000):06d}"
    digest = hashlib.sha256(code.encode("ascii")).hexdigest()
    expires_at = int(time.time()) + ACCESS_CODE_TTL_SECONDS
    challenge = f"otp:{digest}:{expires_at}:0"
    if not store.update("candidate_sessions", {"code": challenge}, {"id": row["id"]}):
        raise RuntimeError("Could not save interview verification challenge.")
    return code, challenge


def clear_access_code(email: str, challenge: str) -> None:
    get_store().update(
        "candidate_sessions",
        {"code": None},
        {"email": email.lower().strip(), "code": challenge},
    )


def verify_access_code(email: str, submitted_code: str) -> Optional[dict]:
    store = get_store()
    row = store.first("candidate_sessions", {"email": email.lower().strip()})
    if not row or row.get("used") or not row.get("resume_id"):
        return None
    challenge = str(row.get("code") or "")
    parts = challenge.split(":")
    if len(parts) != 4 or parts[0] != "otp":
        return None
    _, expected_digest, raw_expiry, raw_attempts = parts
    try:
        expires_at, attempts = int(raw_expiry), int(raw_attempts)
    except ValueError:
        store.update("candidate_sessions", {"code": None}, {"id": row["id"], "code": challenge})
        return None
    if expires_at <= int(time.time()) or attempts >= ACCESS_CODE_MAX_ATTEMPTS:
        store.update("candidate_sessions", {"code": None}, {"id": row["id"], "code": challenge})
        return None

    actual_digest = hashlib.sha256(submitted_code.encode("ascii", errors="ignore")).hexdigest()
    if not secrets.compare_digest(expected_digest, actual_digest):
        next_attempts = attempts + 1
        updated_challenge = None if next_attempts >= ACCESS_CODE_MAX_ATTEMPTS else (
            f"otp:{expected_digest}:{expires_at}:{next_attempts}"
        )
        store.update(
            "candidate_sessions",
            {"code": updated_challenge},
            {"id": row["id"], "code": challenge},
        )
        return None

    if not store.update("candidate_sessions", {"code": None}, {"id": row["id"], "code": challenge}):
        return None
    return _map({**row, "code": None})


def reset_session_by_email(email: str, resume_id: Optional[str] = None) -> Optional[dict]:
    store = get_store()
    clean = email.lower().strip()
    if store.update("candidate_sessions", {"used": False, "used_at": None, "code": None}, {"email": clean}):
        return get_session_by_email(clean)
    return create_candidate_session(clean, resume_id) if resume_id else None


def mark_email_session_used(email: str) -> dict:
    store = get_store()
    clean = email.lower().strip()
    if not store.update("candidate_sessions", {"used": True, "used_at": _now()}, {"email": clean}):
        raise RuntimeError("Failed to mark email session used: Not found")
    return get_session_by_email(clean)


def delete_session_by_resume_id(resume_id: str) -> None:
    get_store().delete("candidate_sessions", {"resume_id": resume_id})


def get_all_sessions() -> list[dict]:
    try:
        return [_map(r) for r in get_store().select("candidate_sessions", order="created_at desc")]
    except Exception:
        return []
