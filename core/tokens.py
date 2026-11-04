import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from core.config import env

DEV_SECRET = "dev-employee-auth-secret"


def _secret() -> bytes:
    return (env("EMPLOYEE_AUTH_SECRET") or DEV_SECRET).encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def sign_token(employee_id: str, expires_in_ms: int = 7 * 24 * 60 * 60 * 1000, email: Optional[str] = None) -> str:
    claims = {"employee_id": employee_id.strip().upper(), "exp": int(time.time() * 1000) + expires_in_ms}
    if email:
        claims["email"] = email.strip().lower()
    payload = json.dumps(claims, separators=(",", ":"))
    encoded = _b64url_encode(payload.encode("utf-8"))
    signature = hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def sign_interview_token(resume_id: str, expires_in_ms: int = 4 * 60 * 60 * 1000) -> str:
    """Short-lived token bound to one interview/resume UUID."""
    claims = {
        "employee_id": "INTERVIEW",
        "resume_id": str(resume_id).strip().lower(),
        "exp": int(time.time() * 1000) + expires_in_ms,
    }
    payload = json.dumps(claims, separators=(",", ":"))
    encoded = _b64url_encode(payload.encode("utf-8"))
    signature = hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_interview_token(token: Optional[str], resume_id: str) -> bool:
    claims = token_claims(token)
    if not claims or claims.get("employee_id") != "INTERVIEW":
        return False
    expected = str(resume_id or "").strip().lower()
    return bool(expected) and str(claims.get("resume_id") or "").strip().lower() == expected


def token_claims(token: Optional[str]) -> Optional[dict]:
    if not token or not isinstance(token, str) or "." not in token:
        return None
    encoded, _, signature = token.partition(".")
    if not encoded or not signature:
        return None
    expected = hmac.new(_secret(), encoded.encode("ascii", "ignore"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
        return None
    try:
        data = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except Exception:
        return None
    if time.time() * 1000 > float(data.get("exp", 0)):
        return None
    return data


def verify_token(token: Optional[str]) -> Optional[str]:
    claims = token_claims(token)
    return claims.get("employee_id") if claims else None


def token_email(token: Optional[str]) -> Optional[str]:
    claims = token_claims(token)
    return (claims or {}).get("email") or None


def require_secret_in_production() -> None:
    if env("NODE_ENV").lower() == "production" and not env("EMPLOYEE_AUTH_SECRET"):
        raise RuntimeError("EMPLOYEE_AUTH_SECRET must be set in production")


def random_hex(nbytes: int = 16) -> str:
    return os.urandom(nbytes).hex()


def otp_digest(code: str, challenge_id: str, purpose: str) -> str:
    payload = f"{challenge_id}:{purpose}:{code}".encode("utf-8")
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()
