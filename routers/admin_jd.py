import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from core.auth import acting_admin_email, require_permission
from core.db import get_store
from core.errors import json_error, public_error
from core.security import get_client_ip, inspect_upload
from services import admin_accounts, audit, deleted_requirements as deleted_req, jd_to_br, question_gen, question_queue, resumes
from services.dashboard_sync import bump_sync
from services.structured_log import write_log

log = logging.getLogger("talentscope")
router = APIRouter(prefix="/api/admin", tags=["admin-jd"], dependencies=[Depends(require_permission("requirements"))])

JD_EXTS = ("pdf", "doc", "docx", "txt", "html", "htm")
JD_MAX = 10 * 1024 * 1024
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _created_at(raw: str, fallback: str) -> str:
    value = (raw or "").strip()
    if not value:
        return fallback
    try:
        if len(value) == 10:
            return datetime.fromisoformat(value).replace(hour=12, tzinfo=timezone.utc).isoformat()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()
    except ValueError:
        return fallback


def _map(row: dict) -> dict:
    return {"id": row["id"], "jdText": row.get("jd_text"), "rmEmail": row.get("rm_email"),
            "fileName": row.get("file_name") or "Pasted Job Description", "createdAt": row.get("created_at"),
            "uploadBatch": row.get("created_at")}


def _ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


@router.get("/jd")
def list_jds(request: Request):
    try:
        email = acting_admin_email(request) or ""
        deleted = deleted_req.load()
        rows = get_store().select("job_descriptions", order="created_at desc")
        jds = [_map(r) for r in rows if not deleted_req.is_deleted(deleted, r["id"], r.get("file_name"))]
        groups: dict = {}
        for jd in jds:
            name = jd["fileName"]
            br = name.split(" | ")[0] if " | " in name else ""
            key = f"br_{br}" if br else f"file_{name}_text_{(jd['jdText'] or '').strip()[:500]}"
            groups.setdefault(key, []).append(jd)
        unique = []
        for items in groups.values():
            master = items[0]
            master["duplicateIds"] = [j["id"] for j in items]
            unique.append(master)
        unique.sort(key=lambda j: _ts(j["createdAt"]), reverse=True)
        if not email or admin_accounts.can_view_org_screening_data(email):
            return {"jds": unique}
        return {"jds": [j for j in unique if (j["rmEmail"] or "").lower().strip() in (email, admin_accounts.default_admin_email(), "")]}
    except Exception as err:
        log.error("Failed to read JDs: %s", err)
        return json_error(500, "Failed to read Job Descriptions")


@router.post("/jd")
async def save_jd(request: Request):
    ip = get_client_ip(request)
    try:
        jd_text, rm_email, file_name, jd_id, requested = "", admin_accounts.default_admin_email(), "Pasted Job Description", "", ""
        is_update = False
        store = get_store()
        if "multipart/form-data" in (request.headers.get("content-type") or ""):
            form = await request.form()
            upload = form.get("file")
            rm_email = str(form.get("rmEmail") or admin_accounts.default_admin_email()).lower().strip()
            if upload is None or not hasattr(upload, "read"):
                return json_error(400, "No file provided")
            data = await upload.read()
            ext = (upload.filename or "").rsplit(".", 1)[-1].lower()
            if ext not in JD_EXTS:
                return json_error(400, "Invalid file type. Only PDF, Word, Text, and HTML files are allowed.")
            ok, detail = inspect_upload(data, upload.filename or "", JD_MAX, JD_EXTS)
            if not ok:
                return json_error(400, detail)
            file_name, jd_text = detail, resumes.extract_text(data, detail)
        else:
            body = await request.json()
            jd_text = body.get("jd") or ""
            rm_email = body.get("rmEmail") or admin_accounts.default_admin_email()
            jd_id = body.get("jdId") or ""
            requested = body["createdAt"].strip() if isinstance(body.get("createdAt"), str) else ""
            if jd_id:
                is_update = True
                existing = store.first("job_descriptions", {"id": jd_id})
                if existing:
                    file_name = existing.get("file_name") or file_name
                    requested = requested or str(existing.get("created_at") or "")
            if body.get("fileName"):
                file_name = body["fileName"]
        if not isinstance(jd_text, str) or not jd_text.strip():
            return json_error(400, "Job description text cannot be empty")
        if not is_update and not deleted_req.file_name_has_br_id(file_name):
            labels = [str(r.get("file_name") or "") for r in store.select("job_descriptions", columns=["file_name"])]
            file_name = f"{deleted_req.next_synthetic_br_id(labels)} | {file_name}"
        new_id = jd_id or str(uuid.uuid4())
        created = _created_at(requested, datetime.now(timezone.utc).isoformat())
        rm = rm_email.lower().strip()
        store.upsert("job_descriptions", {"id": new_id, "jd_text": jd_text.strip(), "rm_email": rm, "file_name": file_name,
                                          "created_at": created}, "id")
        audit.add_log(rm_email or admin_accounts.default_admin_email(), "ADMIN_UPDATE_JD" if is_update else "ADMIN_CREATE_JD", file_name or new_id,
                      f"JD ID: {new_id}. Associated RM: {rm_email}", ip)
        write_log("requirements", "UPDATE_JD" if is_update else "CREATE_JD", "success",
                  f"Successfully {'updated' if is_update else 'created'} JD ID: {new_id} ({file_name}). Associated RM: {rm_email}")
        bump_sync("jd_update" if is_update else "jd_create")
        for key in question_gen.bank_keys_for(jd_text):
            if question_gen.bank_size(key) < question_gen.BANK_TARGET:
                question_queue.enqueue_bank(key)
        return {"success": True, "jd": {"id": new_id, "jdText": jd_text.strip(), "rmEmail": rm, "fileName": file_name,
                                        "createdAt": created, "uploadBatch": None if is_update else created}}
    except Exception as err:
        write_log("requirements", "SAVE_JD_FAILED", "failed", "Failed to save Job Description")
        return public_error(err, "Failed to save Job Description")


@router.delete("/jd")
async def delete_jd(request: Request):
    ip = get_client_ip(request)
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        single = request.query_params.get("id")
        raw = body.get("ids") if isinstance(body, dict) and isinstance(body.get("ids"), list) else ([single] if single else [])
        ids = list(dict.fromkeys(str(v or "").strip() for v in raw if str(v or "").strip()))
        if not ids:
            return json_error(400, "JD ID is required")
        store = get_store()
        rows = store.select("job_descriptions", columns=["id", "file_name", "jd_text"])
        selected = {i.lower() for i in ids}
        picked_br = {deleted_req.extract_br_id(r.get("file_name")) for r in rows if str(r["id"]).lower() in selected} - {""}
        matching = [r for r in rows if str(r["id"]).lower() in selected or deleted_req.extract_br_id(r.get("file_name")) in picked_br]
        deleted_req.mark_deleted([{"id": r["id"], "brId": deleted_req.extract_br_id(r.get("file_name")), "fileName": r.get("file_name")}
                                  for r in matching])
        to_delete = list(dict.fromkeys([*ids, *[str(r["id"]) for r in matching]]))
        store.delete("job_descriptions", {"id": to_delete})
        text = f"Successfully deleted JD ID{'s' if len(ids) > 1 else ''}: {', '.join(ids)}"
        audit.add_log(admin_accounts.default_admin_email(), "ADMIN_BATCH_DELETE_JD" if len(ids) > 1 else "ADMIN_DELETE_JD", ", ".join(ids), text, ip)
        write_log("requirements", "BATCH_DELETE_JD" if len(ids) > 1 else "DELETE_JD", "success", text)
        bump_sync("jd_delete")
        return {"success": True, "deletedCount": len(ids)}
    except Exception as err:
        write_log("requirements", "DELETE_JD_FAILED", "failed", "Failed to delete Job Description")
        return public_error(err, "Failed to delete Job Description")


@router.post("/jd-to-br")
async def jd_to_br_route(jds: list[UploadFile] = File(default=[]), template: UploadFile = File(None),
                         jdReqIdsMapping: str = Form("{}")):
    if not jds:
        return json_error(400, "Missing Job Description files (.jds)")
    if template is None:
        return json_error(400, "Missing Excel template file")
    try:
        try:
            mapping = json.loads(jdReqIdsMapping or "{}")
        except ValueError:
            mapping = {}
        files = [(f.filename or "jd", await f.read()) for f in jds]
        result = jd_to_br.build_workbook(await template.read(), files, mapping if isinstance(mapping, dict) else {})
        return Response(result, media_type=XLSX_MIME,
                        headers={"Content-Disposition": f'attachment; filename="updated_{template.filename}"'})
    except Exception as err:
        return public_error(err, "Spreadsheet processing error")
