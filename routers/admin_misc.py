import logging
import csv
import io
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, Response

from core.auth import acting_admin_email, require_admin, require_permission
from core.db import get_store
from core.errors import json_error, public_error
from core.security import get_client_ip
from services import admin_accounts, audit, reset_logs, settings, structured_log, system_logs
from services.dashboard_sync import bump_sync, read_sync
from services.structured_log import write_log

log = logging.getLogger("talentscope")
public = APIRouter(prefix="/api", tags=["portal"])
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])



@public.get("/portal_settings")
def portal_settings():
    try:
        return settings.get_portal_features()
    except Exception as err:
        return public_error(err, "Failed to get settings")


@router.post("/portal_settings")
def update_portal_settings(request: Request, body: dict = Body(default_factory=dict)):
    actor = acting_admin_email(request) or ""
    if not actor or not admin_accounts.can_manage_portal_config(actor):
        return json_error(403, "You do not have Portal Config permission.")
    new_settings = body.get("settings")
    if not isinstance(new_settings, dict):
        return json_error(400, "Invalid settings object")
    try:
        updated = settings.update_portal_features(new_settings)
        details = f"Updated portal settings: SystemLogsViewer={updated['showSystemLogsViewer']}"
        audit.add_log(actor or "admin@infinite.com", "ADMIN_UPDATE_PORTAL_SETTINGS", "portal-settings", details, get_client_ip(request))
        write_log("employee", "UPDATE_PORTAL_SETTINGS", "success", details)
        return {"success": True, "settings": updated}
    except Exception as err:
        write_log("employee", "UPDATE_PORTAL_SETTINGS_FAILED", "failed", str(err))
        return public_error(err, "Failed to update settings")


@router.get("/interview_defaults")
def interview_defaults():
    return settings.get_interview_defaults()


@router.post("/interview_defaults")
def update_interview_defaults(request: Request, body: dict = Body(default_factory=dict)):
    actor = acting_admin_email(request) or "unknown"
    try:
        updated = settings.update_interview_defaults(body)
        details = f"Default interview duration set to {updated['durationMinutes']} minutes"
        audit.add_log(actor, "ADMIN_UPDATE_INTERVIEW_DEFAULTS", "interview-defaults",
                      details, get_client_ip(request))
        write_log("audit", "UPDATE_INTERVIEW_DEFAULTS", "success", details)
        return {"success": True, **updated}
    except Exception as err:
        return public_error(err, "Failed to update interview defaults")


@router.get("/sync")
def sync():
    return read_sync()


def _display_log(entry: dict) -> dict:
    status = str(entry.get("status") or "success").lower()
    raw_status = status
    if status.isdigit():
        status = "failed" if int(status) >= 400 else "success"
    if status not in ("success", "failed", "error", "warning"):
        status = "success"
    timestamp = str(entry.get("timestamp") or "")
    if timestamp in ("", "None"):
        timestamp = datetime.now(timezone.utc).isoformat()
    display = {
        "timestamp": timestamp,
        "module": str(entry.get("module") or "audit"),
        "action": str(entry.get("action") or "UNKNOWN"),
        "status": status,
        "details": str(entry.get("details") or ""),
        "actor": str(entry.get("actor") or ""),
        "ip": str(entry.get("ip") or ""),
    }
    for key in (
        "request_id", "user_role", "user_agent", "endpoint", "execution_time",
        "db_operation", "table_name", "record_id", "model", "input_tokens", "output_tokens",
        "level", "error_message", "stack_trace", "request_payload", "response_payload",
    ):
        value = entry.get(key)
        if value is not None:
            display[key] = value
    if raw_status.isdigit():
        display["status_code"] = int(raw_status)
    return display


@router.get("/logs/metrics", dependencies=[Depends(require_permission("systemLogs"))])
def get_log_metrics(request: Request):
    try:
        return system_logs.metrics(
            request.query_params.get("from") or "",
            request.query_params.get("to") or "",
        )
    except Exception as err:
        return public_error(err, "Could not load log metrics")


@router.get("/logs", dependencies=[Depends(require_permission("systemLogs"))])
def get_logs(request: Request):
    module = request.query_params.get("module") or "all"
    status = request.query_params.get("status") or "all"
    level = (request.query_params.get("level") or "").upper()
    user = request.query_params.get("user") or ""
    from_date = request.query_params.get("from") or ""
    to_date = request.query_params.get("to") or ""
    endpoint = request.query_params.get("endpoint") or ""
    request_id = request.query_params.get("request_id") or ""
    search = (request.query_params.get("search") or "").lower()
    try:
        entries = [_display_log(entry) for entry in structured_log.merged_logs(module, {
            "user": user,
            "level": level,
            "from_date": from_date,
            "to_date": to_date,
            "search": search,
        })]
    except Exception as err:
        return public_error(err, "Could not read system logs")
    if status != "all":
        entries = [e for e in entries if e["status"] == status.lower()]
    if level:
        entries = [e for e in entries if str(e.get("level") or "").upper() == level]
    if user:
        entries = [e for e in entries if user.casefold() in str(e.get("actor") or "").casefold()]
    if from_date:
        entries = [e for e in entries if str(e.get("timestamp") or "") >= from_date]
    if to_date:
        entries = [e for e in entries if str(e.get("timestamp") or "")[:10] <= to_date[:10]]
    if endpoint:
        entries = [e for e in entries if endpoint.casefold() in str(e.get("endpoint") or "").casefold()]
    if request_id:
        entries = [e for e in entries if request_id.casefold() in str(e.get("request_id") or "").casefold()]
    if search:
        fields = ("action", "details", "module", "actor", "ip", "request_id", "endpoint", "error_message")
        entries = [e for e in entries if any(search in str(e.get(f) or "").lower() for f in fields)]

    export_format = (request.query_params.get("format") or "").lower()
    if export_format in ("json", "csv", "xlsx") or request.query_params.get("download") == "true":
        if module != "all" and not structured_log.MODULE_RE.match(module):
            return json_error(404, "Log file not found")
        if export_format == "csv":
            output = io.StringIO()
            fields = [
                "timestamp", "level", "module", "action", "status", "status_code", "actor",
                "user_role", "ip", "request_id", "endpoint", "execution_time", "details",
                "db_operation", "table_name", "record_id", "model", "input_tokens", "output_tokens",
                "error_message", "stack_trace", "request_payload", "response_payload",
            ]
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(entries)
            content, media_type, extension = output.getvalue(), "text/csv", "csv"
        elif export_format == "json":
            content, media_type, extension = json.dumps(entries, ensure_ascii=False), "application/json", "json"
        elif export_format == "xlsx":
            from openpyxl import Workbook

            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "System Logs"
            columns = [
                "timestamp", "level", "module", "action", "status", "status_code", "actor",
                "user_role", "ip", "request_id", "endpoint", "execution_time", "details",
                "db_operation", "table_name", "record_id", "model", "input_tokens", "output_tokens",
                "error_message", "stack_trace", "request_payload", "response_payload",
            ]
            sheet.append(columns)
            for entry in entries:
                sheet.append([
                    json.dumps(entry.get(column), ensure_ascii=False)
                    if isinstance(entry.get(column), (dict, list))
                    else entry.get(column)
                    for column in columns
                ])
            output = io.BytesIO()
            workbook.save(output)
            content, media_type, extension = output.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
        else:
            content = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)
            media_type, extension = "text/plain", "log"
        name = f"system_logs.{extension}" if module == "all" else f"{module}.{extension}"
        return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return {"logs": entries[:1500], "total": len(entries)}


@router.delete("/logs", dependencies=[Depends(require_permission("systemLogs"))])
def clear_logs(request: Request):
    actor = acting_admin_email(request) or "unknown"
    if not admin_accounts.can_manage_portal_config(actor):
        return json_error(403, "Only Portal Config admins can clear system logs.")
    structured_log.clear_all()
    audit.clear_logs()
    try:
        get_store().delete("system_logs", None)
    except Exception as err:
        return public_error(err, "Could not clear persisted system logs")
    write_log("audit", "CLEAR_SYSTEM_LOGS", "success", f"Cleared system and audit logs by {actor}")
    audit.add_log(actor, "CLEAR_SYSTEM_LOGS", "System Logs", "Cleared system and audit logs", get_client_ip(request))
    return {"success": True}


@router.get("/reset_logs")
def get_reset_logs():
    return {"logs": reset_logs.get_logs()}


@router.delete("/reset_logs")
def clear_reset_logs():
    reset_logs.clear_logs()
    write_log("candidate-processing", "CLEAR_RESET_LOGS", "success", "Cleared all candidate reset logs")
    return {"success": True, "logs": []}


def _map_email(row: dict) -> dict:
    return {"id": row["id"], "to": row.get("candidate_email"), "fullName": row.get("full_name") or "", "subject": row.get("subject") or "",
            "htmlBody": row.get("body") or "", "dispatchedAt": row.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "status": row.get("status") or "simulated", "rmEmail": row.get("rm_email")}


def _emails_for(email: str | None) -> list[dict]:
    rows = [_map_email(r) for r in get_store().select("simulated_emails")]
    if email and not admin_accounts.can_view_org_screening_data(email):
        rows = [r for r in rows if str(r.get("rmEmail") or "").lower().strip() == email]
    return sorted(rows, key=lambda r: r["dispatchedAt"], reverse=True)


@router.get("/emails", dependencies=[Depends(require_permission("emailOutbox"))])
def get_emails(request: Request):
    try:
        email = acting_admin_email(request)
        return {"emails": _emails_for(email)}
    except Exception as err:
        log.error("Failed to read emails outbox: %s", err)
        return json_error(500, "Failed to read Outbox Logs")


@router.delete("/emails", dependencies=[Depends(require_permission("emailOutbox"))])
async def delete_emails(request: Request):
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        query_ids = request.query_params.get("ids")
        raw = body.get("ids") if isinstance(body, dict) and isinstance(body.get("ids"), list) else (
            [i.strip() for i in query_ids.split(",") if i.strip()] if query_ids else None)
        ids = list(dict.fromkeys(str(i).strip() for i in raw if str(i).strip())) if raw else None
        store = get_store()
        if not ids:
            store.delete("simulated_emails", None)
            write_log("email", "CLEAR_EMAIL_LOGS", "success", "Cleared all email outbox logs")
            return {"success": True, "emails": [], "deletedCount": None}
        store.delete("simulated_emails", {"id": ids})
        write_log("email", "DELETE_EMAIL_LOGS", "success", f"Deleted email logs for IDs: {', '.join(ids)}")
        email = acting_admin_email(request)
        return {"success": True, "emails": _emails_for(email), "deletedCount": len(ids)}
    except Exception as err:
        write_log("email", "DELETE_EMAIL_LOGS_FAILED", "failed", str(err))
        return public_error(err, "Failed to clear Outbox Logs")
