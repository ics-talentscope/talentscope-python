import logging
from html import escape

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response

from core.auth import acting_admin_email, require_permission
from core.config import app_url
from core.db import get_store
from core.errors import json_error, public_error
from core.security import get_client_ip
from core.validation import as_email, as_uuid
from services import admin_accounts, audit, mailer, question_queue, reset_logs, resumes, sessions, storage
from services.invite_mail import build_invite_html, build_password_reset_html
from services.structured_log import write_log

log = logging.getLogger("talentscope")
router = APIRouter(prefix="/api/admin/resumes", tags=["admin-resumes"], dependencies=[Depends(require_permission("candidates"))])

MIME = {"pdf": "application/pdf", "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "txt": "text/plain"}


def _format_attempt(attempt: dict) -> dict:
    index = attempt.get("question_index")
    number = "Intro" if index == -1 else "Q to HR" if index == -2 else index + 1 if isinstance(index, int) else None
    return {**attempt, "candidate_id": attempt.get("resume_id"), "question_number": number,
            "question": attempt.get("question_text"), "ai_score": attempt.get("mock_score"),
            "ai_feedback": attempt.get("mock_feedback"), "timestamp": attempt.get("created_at")}


@router.get("")
def list_resumes(request: Request):
    email = acting_admin_email(request) or ""
    try:
        items = resumes.list_resumes()
        filter_email = str(request.query_params.get("email") or "").lower().strip()
        if filter_email:
            items = [r for r in items if str((r.get("report") or {}).get("rmEmail") or "").lower().strip() == filter_email]
        elif email and not admin_accounts.can_view_org_screening_data(email):
            items = [r for r in items if str((r.get("report") or {}).get("rmEmail") or "").lower().strip() == email]
        attempts_by_resume: dict = {}
        try:
            for attempt in get_store().select("interview_attempts", order="id desc"):
                if attempt.get("resume_id"):
                    attempts_by_resume.setdefault(attempt["resume_id"], []).append(attempt)
        except Exception as err:
            log.warning("Admin resumes: interview_attempts unavailable: %s", err)
        concluded = {s["resumeId"]: s["used"] for s in sessions.get_all_sessions() if s.get("resumeId")}
        enriched = [{**r, "isConcluded": concluded.get(r["id"], False),
                     "interview_attempts": [_format_attempt(a) for a in attempts_by_resume.get(r["id"], [])]} for r in items]
        return {"resumes": enriched, "source": "mssql", "count": len(enriched)}
    except Exception as err:
        log.error("Admin resumes fetch failed: %s", err)
        return json_error(500, "Failed to load resumes", resumes=[], source="error")


@router.delete("")
def batch_delete(request: Request, body: dict = Body(default_factory=dict)):
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return json_error(400, "Candidate IDs are required")
    try:
        clean = [as_uuid(i) for i in ids]
        if not all(clean):
            return json_error(400, "Invalid candidate id")
        for resume_id in clean:
            resumes.delete_resume(resume_id)
        audit.add_log(admin_accounts.default_admin_email(), "ADMIN_BATCH_DELETE_RESUMES", ", ".join(clean),
                      f"Successfully batch deleted candidate CV IDs: {', '.join(clean)}", get_client_ip(request))
        write_log("candidate-processing", "BATCH_DELETE_CANDIDATES", "success", f"Batch deleted candidate CV IDs: {', '.join(clean)}")
        return {"success": True}
    except Exception as err:
        write_log("candidate-processing", "BATCH_DELETE_CANDIDATES_FAILED", "failed", str(err))
        return public_error(err, "Failed to delete candidate records")


@router.post("/reset_by_email")
def reset_by_email(request: Request, body: dict = Body(default_factory=dict)):
    clean = as_email(body.get("email"))
    if not clean:
        return json_error(400, "Please enter a valid candidate email address.")
    actor = acting_admin_email(request) or "unknown"
    try:
        match = next((r for r in resumes.list_resumes()
                      if str((r.get("parsed") or {}).get("personal", {}).get("email") or "").lower().strip() == clean), None)
        session = sessions.reset_session_by_email(clean, match["id"] if match else None)
        if match:
            _clear_candidate(match["id"])
        if not session and not match:
            return json_error(404, "Candidate not found. Check the email address and try again.")
        reset_logs.add_log(clean, actor, "Reset Form")
        write_log("candidate-processing", "RESET_CANDIDATE_BY_EMAIL", "success", f"Successfully reset candidate session and attempts for email {clean}")
        return {"success": True, "message": f"Session for {clean} has been reset successfully."}
    except Exception as err:
        write_log("candidate-processing", "RESET_CANDIDATE_BY_EMAIL_FAILED", "failed", str(err))
        return public_error(err, "Could not reset the candidate session and assessment. Retry; contact support if it continues.")


def _clear_candidate(resume_id: str) -> None:
    store = get_store()
    store.delete("interview_attempts", {"resume_id": resume_id})
    store.delete("interview_questions", {"resume_id": resume_id})
    question_queue.reset(resume_id)
    resume = resumes.get_resume(resume_id, True)
    if resume and resume.get("report"):
        for key in ("videoUrl", "proctoring", "videoDuration"):
            resume["report"].pop(key, None)
        resumes.save_row(resume)


@router.get("/{resume_id}")
def get_resume(resume_id: str):
    rid = as_uuid(resume_id)
    resume = resumes.get_resume(rid, True) if rid else None
    if not resume:
        return json_error(404, "Resume not found")
    attempts = get_store().select("interview_attempts", {"resume_id": rid}, order="question_index asc")
    return {"resume": {**resume, "interview_attempts": [_format_attempt(a) for a in attempts]}}


@router.delete("/{resume_id}")
def delete_resume(resume_id: str, request: Request):
    rid = as_uuid(resume_id)
    if not rid:
        return json_error(400, "Invalid resume id")
    try:
        resumes.delete_resume(rid)
        actor = acting_admin_email(request) or "unknown"
        audit.add_log(actor, "ADMIN_DELETE_RESUME", rid, f"Successfully deleted candidate resume ID: {rid}", get_client_ip(request))
        write_log("candidate-processing", "DELETE_CANDIDATE", "success", f"Successfully deleted candidate CV ID: {rid}")
        return {"success": True}
    except Exception as err:
        write_log("candidate-processing", "DELETE_CANDIDATE_FAILED", "failed", f"Failed to delete candidate CV ID: {rid}: {err}")
        return public_error(err, "Failed to delete resume")


@router.get("/{resume_id}/download")
def download(resume_id: str):
    import base64

    rid = as_uuid(resume_id)
    if not rid:
        return json_error(400, "Invalid resume id")
    stored = next((f for f in storage.bucket_dir("resumes").iterdir() if f.is_file() and f.name.startswith(f"{rid}-")), None)
    data = storage.read("resumes", stored.name) if stored else None
    if stored and data:
        name = stored.name[len(rid) + 1:]
    else:
        row = get_store().first("resumes", {"id": rid}, ["filename", "file_base64"])
        if not row or not row.get("file_base64"):
            return json_error(404, "Resume file not found in storage or database")
        data, name = base64.b64decode(row["file_base64"]), row.get("filename") or "resume"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    safe = name.replace('"', "")
    return Response(data, media_type=MIME.get(ext, "application/octet-stream"),
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@router.post("/{resume_id}/suitability")
def suitability(resume_id: str, body: dict = Body(default_factory=dict)):
    rid = as_uuid(resume_id)
    value = body.get("suitability")
    if value not in ("suitable", "unsuitable"):
        return json_error(400, "Invalid suitability value. Must be suitable or unsuitable.")
    resume = resumes.get_resume(rid) if rid else None
    if not resume:
        return json_error(404, "Resume record not found")
    try:
        active_jd = body.get("activeJdId")
        resume["report"] = {**(resume.get("report") or {}), "suitability": value, "suitabilityOverridden": True,
                            **({"jdId": active_jd} if active_jd else {})}
        resumes.save_row(resume)
        write_log("candidate-processing", "OVERRIDE_SUITABILITY", "success",
                  f"Overrode candidate ID {rid} suitability to {value} for JD {active_jd or 'default'}")
        return {"success": True, "suitability": value}
    except Exception as err:
        write_log("candidate-processing", "OVERRIDE_SUITABILITY_FAILED", "failed", str(err))
        return public_error(err, "Override failed")


@router.post("/{resume_id}/reset")
def reset(resume_id: str, request: Request, body: dict = Body(default_factory=dict)):
    rid = as_uuid(resume_id)
    resume = resumes.get_resume(rid) if rid else None
    if not resume:
        return json_error(404, "Resume record not found")
    email = as_email((resume.get("parsed") or {}).get("personal", {}).get("email"))
    if not email:
        return json_error(400, "Candidate email is missing or invalid. Update the resume contact details before resetting the session.")
    try:
        actor = acting_admin_email(request) or "unknown"
        sessions.reset_session_by_email(email, rid)
        _clear_candidate(rid)
        audit.add_log(actor, "ADMIN_RESET_RESUME", email, f"Cleared proctoring, questions, and attempts for candidate {email}", get_client_ip(request))
        write_log("candidate-processing", "RESET_CANDIDATE", "success", f"Cleared proctoring, questions, and attempts for candidate {email}")
        reset_logs.add_log(email, actor, "Candidate Card")
        return {"success": True}
    except Exception as err:
        write_log("candidate-processing", "RESET_CANDIDATE_FAILED", "failed", str(err))
        return public_error(err, "Could not reset the candidate session and assessment. Retry; contact support if it continues.")


@router.post("/{resume_id}/invite")
def invite(resume_id: str, request: Request, body: dict = Body(default_factory=dict)):
    rid = as_uuid(resume_id)
    resume = resumes.get_resume(rid) if rid else None
    if not resume:
        return json_error(404, "Resume record not found")
    email = as_email((resume.get("parsed") or {}).get("personal", {}).get("email"))
    if not email:
        return json_error(400, "Candidate email is missing or invalid. Update the resume contact details before sending an invitation.")
    try:
        config = body.get("interviewConfig") if isinstance(body.get("interviewConfig"), dict) else None
        if config:
            resume["report"] = {**(resume.get("report") or {}), "interviewConfig": config}
            resumes.save_row(resume)
            try:
                get_store().delete("interview_questions", {"resume_id": rid})
                question_queue.reset(rid)
            except Exception as err:
                log.error("Failed to delete existing interview questions on config update: %s", err)
        candidate_password = sessions.generate_candidate_password()
        if not question_queue.existing_questions(rid):
            question_queue.enqueue(rid)
        kind = (config or {}).get("interviewType") or "technical"
        label = {"both": "combined technical and non-technical", "non-technical": "non-technical"}.get(kind, "technical")
        full_name = (resume.get("parsed") or {}).get("personal", {}).get("fullName") or "Candidate"
        prefix = {"non-technical": "TalentScope — Interview Assessment",
                  "both": "TalentScope — Technical & Behavioral Assessment"}.get(kind, "TalentScope — Technical Interview Assessment")
        subject = f"{prefix} - {full_name.upper()}"
        html = build_invite_html(
            full_name, f"Login email address: <strong>{escape(email)}</strong>",
            app_url(), label, candidate_password,
        )
        status, send_error = mailer.dispatch(email, subject, html)
        rm_email = (resume.get("report") or {}).get("rmEmail") or admin_accounts.default_admin_email()
        if status == "failed_real_fallback_simulated":
            mailer.record(
                email, full_name, subject,
                "<p>SMTP delivery failed. Candidate credentials were not changed.</p>",
                status, rm_email,
            )
            audit.add_log(rm_email, "ADMIN_INVITE_CANDIDATE_FAILED", email,
                          f"Invitation email was not delivered. Status: {status}. Subject: {subject}", get_client_ip(request))
            write_log("email", "INVITE_CANDIDATE", "failed",
                      f"Failed to deliver invitation email to {email}. Status: {status}.")
            return json_error(
                503,
                send_error or "Could not deliver the candidate invitation.",
                success=False,
                email=email,
                fullName=full_name,
                status=status,
            )
        sessions.save_candidate_password(email, rid, candidate_password)
        mailer.record(email, full_name, subject, html, status, rm_email)
        if status == "sent":
            resume["report"] = {**(resume.get("report") or {}), "inviteEmailStatus": "sent"}
            resumes.save_row(resume)
        audit.add_log(rm_email, "ADMIN_INVITE_CANDIDATE", email,
                      f"Dispatched assessment invitation email. Status: {status}. Subject: {subject}", get_client_ip(request))
        write_log("email", "INVITE_CANDIDATE", "success", f"Dispatched assessment invitation email to {email}. Status: {status}. Subject: {subject}")
        return {"success": True, "email": email, "fullName": full_name, "status": status, "error": send_error}
    except Exception as err:
        write_log("email", "INVITE_CANDIDATE_FAILED", "failed", f"Failed to send invite email to candidate ID {rid}")
        return public_error(err, "Could not prepare or send the candidate invitation. Verify the candidate email and mail service.")


@router.post("/{resume_id}/reset-password")
def reset_candidate_password(resume_id: str, request: Request):
    rid = as_uuid(resume_id)
    resume = resumes.get_resume(rid) if rid else None
    if not resume:
        return json_error(404, "Resume record not found")
    email = as_email((resume.get("parsed") or {}).get("personal", {}).get("email"))
    if not email:
        return json_error(400, "Candidate email is missing or invalid.")

    store = get_store()
    try:
        delivered_invite = (resume.get("report") or {}).get("inviteEmailStatus") == "sent"
        if not delivered_invite:
            delivered_invite = bool(store.first(
                "simulated_emails",
                {"candidate_email": email, "status": "sent"},
            ))
        if not delivered_invite:
            return json_error(409, "Send a successful invitation email before resetting this candidate's password.")

        session = store.first("candidate_sessions", {"email": email})
        if not session or session.get("resume_id") != rid:
            return json_error(409, "The candidate does not have an active assessment invitation.")
        if session.get("used"):
            return json_error(409, "This candidate has already completed the assessment.")

        password = sessions.generate_candidate_password()
        full_name = (resume.get("parsed") or {}).get("personal", {}).get("fullName") or "Candidate"
        subject = "TalentScope — Assessment Password Reset"
        html = build_password_reset_html(full_name, email, app_url(), password)
        status, send_error = mailer.dispatch(email, subject, html)
        if status != "sent":
            actor = acting_admin_email(request) or "unknown"
            audit.add_log(actor, "ADMIN_RESET_CANDIDATE_PASSWORD_FAILED", email,
                          f"Password reset email was not delivered. Status: {status}.", get_client_ip(request))
            write_log("email", "RESET_CANDIDATE_PASSWORD", "failed",
                      f"Password reset email was not delivered to {email}. Status: {status}.")
            return json_error(503, send_error or "Could not deliver the password reset email.")

        sessions.save_candidate_password(email, rid, password)
        rm_email = (resume.get("report") or {}).get("rmEmail") or admin_accounts.default_admin_email()
        mailer.record(email, full_name, subject, html, status, rm_email)
        actor = acting_admin_email(request) or "unknown"
        audit.add_log(actor, "ADMIN_RESET_CANDIDATE_PASSWORD", email,
                      "Delivered a new temporary assessment password.", get_client_ip(request))
        write_log("email", "RESET_CANDIDATE_PASSWORD", "success",
                  f"Delivered a new assessment password to {email}.")
        return {"success": True, "email": email, "status": status}
    except Exception as err:
        write_log("email", "RESET_CANDIDATE_PASSWORD_FAILED", "failed",
                  f"Failed to reset assessment password for candidate {rid}.")
        return public_error(err, "Could not reset the candidate password.")
