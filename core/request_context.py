import re
import time
from contextvars import ContextVar
from typing import Optional

from fastapi import Request

from core.tokens import token_claims

_current: ContextVar[Optional[dict]] = ContextVar("request_context", default=None)
_request_id: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
_names: dict[str, tuple[float, str]] = {}
NAME_TTL_S = 300
CANDIDATE_PATH = re.compile(r"^/api/interview/([0-9a-fA-F-]{36})(?:/|$)")


def bind(request: Request, ip: str) -> None:
    header = request.headers.get("authorization") or ""
    token = header[7:] if header.startswith("Bearer ") else header
    client = re.sub(r"[^A-Za-z0-9-]", "", request.headers.get("x-client-id") or "")[:64]
    _current.set({
        "ip": ip,
        "token": token,
        "path": request.url.path,
        "client": client,
        "request_id": getattr(request.state, "request_id", None) or request_id(),
        "user_agent": request.headers.get("user-agent", "")[:512],
    })


def set_request_id(value: str):
    return _request_id.set(value)


def reset_request_id(token) -> None:
    _request_id.reset(token)


def request_id() -> Optional[str]:
    context = _current.get()
    return (context.get("request_id") if context else None) or _request_id.get()


def log_context() -> dict:
    context = _current.get() or {}
    claims = token_claims(context.get("token", "")) if context.get("token") else None
    email = claims.get("email") if claims else None
    role = (claims.get("role") or claims.get("user_role")) if claims else None
    if claims and claims.get("employee_id") == "ADMIN":
        role = role or "admin"
    return {
        "request_id": context.get("request_id") or _request_id.get(),
        "user_email": str(email)[:320] if email else None,
        "user_role": str(role)[:80] if role else None,
        "ip_address": context.get("ip"),
        "user_agent": context.get("user_agent"),
    }


def client_id() -> str:
    context = _current.get()
    return context.get("client", "") if context else ""


def ip() -> Optional[str]:
    context = _current.get()
    return context["ip"] if context else None


def _cached(key: str, load) -> str:
    hit = _names.get(key)
    if hit and time.monotonic() - hit[0] < NAME_TTL_S:
        return hit[1]
    try:
        value = load()
    except Exception:
        value = ""
    _names[key] = (time.monotonic(), value)
    return value


def _employee_label(employee_id: str) -> str:
    from services import accounts

    def load() -> str:
        employee = accounts.get_account(employee_id)
        if not employee:
            return f"employee {employee_id}"
        return f"{employee.get('full_name') or employee_id} <{employee.get('email')}>" if employee.get("email") else f"employee {employee_id}"

    return _cached(f"emp:{employee_id}", load)


def _candidate_label(resume_id: str) -> str:
    from core.db import get_store
    import json

    def load() -> str:
        row = get_store().first("resumes", {"id": resume_id}, ["parsed"])
        parsed = row.get("parsed") if row else None
        parsed = json.loads(parsed) if isinstance(parsed, str) else (parsed or {})
        email = (parsed.get("personal") or {}).get("email")
        return f"candidate <{email}>" if email else f"candidate {resume_id}"

    return _cached(f"cand:{resume_id}", load)


def actor() -> str:
    context = _current.get()
    if not context:
        return "system"
    claims = token_claims(context["token"])
    if claims:
        subject = claims.get("employee_id")
        if subject == "ADMIN":
            return claims.get("email") or "admin (session started before upgrade)"
        if subject:
            return _employee_label(subject)
    match = CANDIDATE_PATH.match(context["path"])
    if match:
        return _candidate_label(match.group(1))
    return "public"
