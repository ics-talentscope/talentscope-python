import json
import re
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from core import request_context
from core.db import get_store
from core.logger import logger

_EMAIL_RE = re.compile(r"^[^@\s]{1,254}@[^@\s]{1,253}$")
_SECRET_TEXT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_EMAIL_TEXT_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_SAFE_PAYLOAD_KEYS = {
    "action", "category", "count", "created", "deleted", "enabled", "limit",
    "offset", "page", "role", "status", "success", "total", "type", "updated",
}
_SENSITIVE_VALUE_RE = re.compile(r"(?i)password|secret|token|api[_-]?key|authorization|email|resume|candidate|prompt|answer|credential")


def safe_payload(value: Any) -> dict | list | None:
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if str(key).lower() in _SAFE_PAYLOAD_KEYS and _safe_value(item) is not None
        } or None
    if isinstance(value, list):
        safe_items = [_safe_value(item) for item in value[:20]]
        safe_items = [item for item in safe_items if isinstance(item, (dict, list))]
        return safe_items or None
    return None


def _safe_value(value: Any):
    if isinstance(value, dict):
        return safe_payload(value)
    if isinstance(value, list):
        return safe_payload(value)
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and len(value) <= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", value) and not _SENSITIVE_VALUE_RE.search(value):
        return value
    return None


def redact_text(value: str, limit: int = 2000) -> str:
    text = _SECRET_TEXT_RE.sub(r"\1\2[REDACTED]", str(value))
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    return _EMAIL_TEXT_RE.sub("[REDACTED_EMAIL]", text)[:limit]


def record_log(entry: dict, persist: bool = True) -> dict:
    record = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "log_level": str(entry.get("log_level") or "INFO").upper()[:16],
        "module": str(entry.get("module") or "application")[:80],
        "action": str(entry.get("action") or "event")[:120],
        "user_email": str(entry.get("user_email") or "")[:320] or None,
        "user_role": str(entry.get("user_role") or "")[:80] or None,
        "ip_address": str(entry.get("ip_address") or "")[:64] or None,
        "user_agent": str(entry.get("user_agent") or "")[:512] or None,
        "request_id": str(entry.get("request_id") or request_context.request_id())[:128] or None,
        "endpoint": str(entry.get("endpoint") or "")[:512] or None,
        "db_operation": str(entry.get("db_operation") or "")[:16] or None,
        "table_name": str(entry.get("table_name") or "")[:128] or None,
        "record_id": str(entry.get("record_id") or "")[:128] or None,
        "model": str(entry.get("model") or "")[:120] or None,
        "execution_time": entry.get("execution_time"),
        "input_tokens": entry.get("input_tokens"),
        "output_tokens": entry.get("output_tokens"),
        "status": str(entry.get("status") or "success")[:32],
        "message": redact_text(entry.get("message") or ""),
        "error_message": redact_text(entry.get("error_message") or "") or None,
        "stack_trace": redact_text(entry.get("stack_trace") or "", 16000) or None,
        "request_payload": json.dumps(safe_payload(entry["request_payload"]), ensure_ascii=False)
        if entry.get("request_payload") is not None and safe_payload(entry["request_payload"]) is not None else None,
        "response_payload": json.dumps(safe_payload(entry["response_payload"]), ensure_ascii=False)
        if entry.get("response_payload") is not None and safe_payload(entry["response_payload"]) is not None else None,
    }
    if persist and record["table_name"] != "system_logs":
        try:
            get_store().insert("system_logs", record)
        except Exception:
            logger.bind(module="logging", action="persist_failed", request_id=record["request_id"]).exception(
                "Could not persist system log entry"
            )
    return record


def record_business_event(module: str, action: str, status: str, message: str, actor: str = "") -> dict:
    context = request_context.log_context()
    actor_email = actor.strip().lower() if _EMAIL_RE.fullmatch(actor.strip()) else context.get("user_email")
    level = "ERROR" if status.lower() in {"failed", "error"} else "INFO"
    safe_message = f"{action} ({status})" if module in {"candidate-processing", "interview", "employee", "ai"} else redact_text(message)
    logger.bind(
        module=module,
        action=action,
        status=status,
        user_email=actor_email,
        user_role=context.get("user_role"),
        ip_address=context.get("ip_address"),
        request_id=context.get("request_id"),
    ).log(level, safe_message)
    return record_log({
        "module": module,
        "action": action,
        "log_level": level,
        "user_email": actor_email,
        "user_role": context.get("user_role"),
        "ip_address": context.get("ip_address"),
        "request_id": context.get("request_id"),
        "status": status,
        "message": safe_message,
    })


def list_logs(
    limit: int = 500,
    module: str = "",
    user: str = "",
    level: str = "",
    status: str = "",
    from_date: str = "",
    to_date: str = "",
    search: str = "",
) -> list[dict]:
    conditions = []
    if module:
        conditions.append(("module", "eq", module))
    if user:
        conditions.append(("user_email", "like", f"%{user}%"))
    if level:
        conditions.append(("log_level", "eq", level.upper()))
    if status:
        conditions.append(("status", "eq", status))
    if from_date:
        conditions.append(("timestamp", "gte", from_date))
    if to_date:
        if len(to_date) == 10:
            to_date = f"{to_date}T23:59:59.999999+00:00"
        conditions.append(("timestamp", "lte", to_date))
    rows = get_store().select("system_logs", where=conditions, order="timestamp desc", limit=max(1, min(limit, 2000)))
    if search:
        needle = search.casefold()
        rows = [row for row in rows if needle in " ".join(str(row.get(k) or "") for k in (
            "message", "action", "endpoint", "request_id", "error_message",
            "user_email", "table_name", "db_operation"
        )).casefold()]
    return rows


def metrics(from_date: str = "", to_date: str = "") -> dict:
    store = get_store()
    conditions = []
    if from_date:
        conditions.append(("timestamp", "gte", from_date))
    if to_date:
        if len(to_date) == 10:
            to_date = f"{to_date}T23:59:59.999999+00:00"
        conditions.append(("timestamp", "lte", to_date))

    def count(*extra) -> int:
        return store.count("system_logs", conditions + list(extra))

    rows = store.select(
        "system_logs",
        where=conditions + [("user_email", "not_null", None)],
        columns=["user_email"],
        limit=2000,
    )
    return {
        "totalRequests": count(("module", "eq", "http")),
        "successfulRequests": count(("module", "eq", "http"), ("status", "like", "2%")),
        "failedRequests": count(("module", "eq", "http"), ("status", "like", "4%"))
        + count(("module", "eq", "http"), ("status", "like", "5%")),
        "slowApis": count(("module", "eq", "http"), ("execution_time", "gt", 1000)),
        "databaseErrors": count(("module", "eq", "database"), ("log_level", "in", ["ERROR", "CRITICAL"])),
        "aiServiceErrors": count(("module", "eq", "ai"), ("log_level", "in", ["ERROR", "CRITICAL"])),
        "securityEvents": count(("module", "eq", "security")),
        "activeUsers": len({row.get("user_email") for row in rows if row.get("user_email")}),
    }


def prune_logs(retention_days: int = 365) -> int:
    store = get_store()
    now = datetime.now(timezone.utc)
    removed = 0
    for level, days in (("DEBUG", 7), ("INFO", 30), ("WARNING", 30), ("ERROR", 90), ("CRITICAL", min(365, retention_days))):
        cutoff = (now - timedelta(days=days)).isoformat()
        removed += store.delete("system_logs", [("log_level", "eq", level), ("timestamp", "lt", cutoff)])
    return removed
