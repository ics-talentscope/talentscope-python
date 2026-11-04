import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.db import get_store

log = logging.getLogger("talentscope")


def _map(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "actorEmail": row.get("actor_email"),
        "action": row.get("action"),
        "target": row.get("target"),
        "details": row.get("details"),
        "ipAddress": row.get("ip_address"),
        "createdAt": row.get("created_at") or datetime.now(timezone.utc).isoformat(),
    }


def add_log(actor_email: str, action: str, target: str, details: str, ip_address: str = "") -> dict:
    record = {
        "id": str(uuid.uuid4()),
        "actor_email": (actor_email or "").lower().strip(),
        "action": action,
        "target": target,
        "details": details,
        "ip_address": ip_address,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        get_store().insert("audit_logs", record)
    except Exception as err:
        log.warning("Failed to write audit log: %s", err)
    from services.system_logs import record_business_event

    failed = any(marker in action.upper() for marker in ("FAIL", "DENIED", "ERROR", "LOCKOUT"))
    module = "security" if failed or "ACCESS" in action.upper() or "PERMISSION" in action.upper() else "auth"
    record_business_event(module, action, "failed" if failed else "success", f"{target}: {details}", actor_email)
    return _map(record)


def get_logs(limit: Optional[int] = None) -> list[dict]:
    try:
        rows = get_store().select("audit_logs", order="created_at desc", limit=limit)
    except Exception as err:
        log.warning("Failed to fetch audit logs: %s", err)
        return []
    return [_map(r) for r in rows]


ADMIN_LOGIN_ACTIONS = ("ADMIN_LOGIN_SUCCESS", "ADMIN_SAML_SSO_SUCCESS")


def recent_admin_logins(limit: int = 25) -> list[dict]:
    """Latest successful admin console sign-ins (password + Microsoft SSO)."""
    limit = max(1, min(int(limit or 25), 100))
    try:
        rows = get_store().select(
            "audit_logs",
            where={"action": list(ADMIN_LOGIN_ACTIONS)},
            order="created_at desc",
            limit=limit,
        )
    except Exception as err:
        log.warning("Failed to fetch admin login audit: %s", err)
        # Fallback: scan recent rows if the store rejects `in` filters.
        rows = []
        try:
            for row in get_store().select("audit_logs", order="created_at desc", limit=max(limit * 20, 200)):
                if row.get("action") in ADMIN_LOGIN_ACTIONS:
                    rows.append(row)
                    if len(rows) >= limit:
                        break
        except Exception as err2:
            log.warning("Admin login audit fallback failed: %s", err2)
            return []
    out = []
    for row in rows:
        mapped = _map(row)
        method = "microsoft" if mapped.get("action") == "ADMIN_SAML_SSO_SUCCESS" else "password"
        out.append({
            "email": mapped.get("actorEmail") or "",
            "method": method,
            "ipAddress": mapped.get("ipAddress") or "",
            "at": mapped.get("createdAt"),
            "details": mapped.get("details") or "",
        })
    return out


def clear_logs() -> None:
    try:
        get_store().delete("audit_logs", None)
    except Exception as err:
        log.warning("Failed to clear audit logs: %s", err)
