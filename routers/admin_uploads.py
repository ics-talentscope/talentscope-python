import io
import logging
import zipfile

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from core.auth import require_permission
from core.db import get_store
from core.errors import json_error, public_error
from core.security import UPLOAD_ALLOWED_EXTS, get_client_ip, inspect_upload, is_rate_limited, validate_file_signature
from services import admin_accounts, audit, hr_files, resumes, sessions
from services.structured_log import write_log

log = logging.getLogger("talentscope")
router = APIRouter(prefix="/api/admin", tags=["admin-uploads"], dependencies=[Depends(require_permission("adminControls"))])

CV_TYPES = {"application/pdf", "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
ZIP_EXTS = ("pdf", "doc", "docx", "txt")
MAX_CV, MAX_ZIP, MAX_ENTRY, MAX_ENTRIES = 10 * 1024 * 1024, 50 * 1024 * 1024, 10 * 1024 * 1024, 300


def _jd_text(form_text: str, jd_id: str, rm_email: str) -> str:
    if form_text:
        return form_text
    store = get_store()
    try:
        if jd_id and jd_id != "all":
            row = store.first("job_descriptions", {"id": jd_id}, ["jd_text"])
            if row and row.get("jd_text"):
                return row["jd_text"]
        if rm_email:
            row = store.first("job_descriptions", {"rm_email": rm_email.lower().strip()}, ["jd_text"], "created_at desc")
            if row and row.get("jd_text"):
                return row["jd_text"]
    except Exception as err:
        log.error("JD lookup failed: %s", err)
    return ""


def _register(resume: dict) -> None:
    email = (resume.get("parsed") or {}).get("personal", {}).get("email")
    if email:
        sessions.create_candidate_session(email, resume["id"])


def _zip_entries(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist()[:MAX_ENTRIES]:
            name = info.filename.replace("\\", "/")
            if info.is_dir() or ".." in name or name.startswith("/") or name.startswith("__MACOSX") or "/." in name or name.startswith("."):
                continue
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in ZIP_EXTS or info.file_size == 0 or info.file_size > MAX_ENTRY:
                continue
            payload = archive.read(info)
            base = name.split("/")[-1]
            if validate_file_signature(payload, base):
                yield base, payload


@router.post("/resumes/upload_bulk")
async def upload_bulk(request: Request, file: UploadFile = File(None), jdId: str = Form(None), rmEmail: str = Form(None),
                      jdText: str = Form(""), forceReplace: str = Form("false")):
    ip = get_client_ip(request)
    if is_rate_limited(f"upload_bulk_{ip}", 10, 3_600_000).limited:
        return json_error(429, "Too many bulk uploads. Maximum 10 bulk upload attempts per hour allowed.")
    try:
        if file is None:
            return json_error(400, "No file provided")
        data = await file.read()
        name = file.filename or "upload"
        is_zip = name.lower().endswith(".zip") or file.content_type in ("application/zip", "application/x-zip-compressed")
        if len(data) > (MAX_ZIP if is_zip else MAX_CV):
            return json_error(400, f"File too large (max {'50MB' if is_zip else '10MB'})")
        if is_zip:
            if not validate_file_signature(data, name if name.lower().endswith(".zip") else "bundle.zip"):
                return json_error(400, "ZIP file verification failed: invalid magic signature")
        elif file.content_type not in CV_TYPES or not validate_file_signature(data, name):
            return json_error(400, "File verification failed: invalid format or magic signature")
        force = forceReplace == "true"
        text = _jd_text(jdText or "", jdId or "", rmEmail or "")
        actor = rmEmail or admin_accounts.default_admin_email()
        if is_zip:
            processed = []
            for base, payload in _zip_entries(data):
                try:
                    resume = resumes.process_resume(base, payload, text, jdId or None, rmEmail or None, force, wait=True)
                    if resume and resume.get("status") != "failed":
                        _register(resume)
                        processed.append(resume)
                except Exception as err:
                    log.error("Failed to process zip entry %s: %s", base, err)
            audit.add_log(actor, "ADMIN_BULK_UPLOAD_ZIP", name,
                          f"Successfully uploaded bulk ZIP with {len(processed)} processed nested CVs. ForceReplace: {force}", ip)
            write_log("candidate-processing", "BULK_UPLOAD_ZIP", "success",
                      f"Successfully uploaded bulk ZIP {name} with {len(processed)} processed nested CVs.")
            return {"success": True, "isZip": True, "resumes": processed}
        resume = resumes.process_resume(name, data, text, jdId or None, rmEmail or None, force, wait=True)
        if resume.get("status") == "failed":
            audit.add_log(actor, "ADMIN_SINGLE_UPLOAD_FAILED", name, f"AI analysis failed: {resume.get('error') or 'unknown error'}", ip)
            write_log("candidate-processing", "SINGLE_UPLOAD_FAILED", "failed", f"AI analysis failed for CV {name}: {resume.get('error') or 'unknown error'}")
            return json_error(500, resume.get("error") or "AI analysis failed")
        email = (resume.get("parsed") or {}).get("personal", {}).get("email")
        _register(resume)
        audit.add_log(actor, "ADMIN_SINGLE_UPLOAD_SUCCESS", name,
                      f"Successfully uploaded and analyzed single CV. Candidate: {email or 'unknown email'}. ForceReplace: {force}", ip)
        write_log("candidate-processing", "SINGLE_UPLOAD_SUCCESS", "success",
                  f"Successfully uploaded and analyzed single CV {name} for candidate: {email or 'unknown email'}.")
        return {"success": True, "resume": resume}
    except Exception as err:
        write_log("candidate-processing", "UPLOAD_ROUTE_ERROR", "failed", "Bulk upload route error")
        return public_error(err, "Upload and parsing failed", success=False)


@router.post("/files/llm-place")
async def llm_place(file: UploadFile = File(None)):
    if file is None:
        return json_error(400, "No file provided")
    try:
        data = await file.read()
        ok, detail = inspect_upload(data, file.filename or "", 25 * 1024 * 1024, UPLOAD_ALLOWED_EXTS)
        if not ok:
            return json_error(400, detail)
        lower = detail.lower()
        if lower.endswith((".xlsx", ".xls")):
            preview = hr_files.excel_preview(data)
        elif lower.endswith((".pdf", ".docx", ".doc")):
            preview = str(resumes.extract_text(data, detail) or "")[:6500]
        else:
            preview = data.decode("utf-8", errors="replace")[:6500]
        return {"success": True, "placement": hr_files.classify_with_llm(file.filename or detail, preview)}
    except Exception as err:
        return public_error(err, "Could not classify file")
