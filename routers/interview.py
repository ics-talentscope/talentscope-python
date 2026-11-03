import base64
from html import escape
import logging
import re
from datetime import datetime, timezone

import anyio
from fastapi import APIRouter, Body, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from core.auth import bearer_token, is_admin_request
from core.db import get_store
from core.errors import ApiError, json_error, public_error
from core.security import get_client_ip, inspect_upload, is_rate_limited_any, rate_limited_response
from core.tokens import sign_interview_token, verify_interview_token
from core.validation import as_email, as_uuid
from services.structured_log import write_log
from services import ai, audit, identity, interviews, llm, mailer, question_queue, resumes, sessions, storage

log = logging.getLogger("talentscope")
router = APIRouter(prefix="/api/interview", tags=["interview"])

VIDEO_MAX_BYTES = 120 * 1024 * 1024
WEBM_SIGNATURE = bytes.fromhex("1A45DFA3")
NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, proxy-revalidate", "Pragma": "no-cache", "Expires": "0"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bad_id() -> JSONResponse:
    return json_error(400, "Invalid interview id", status="error")


def _auth_error(err: ApiError, **extra) -> JSONResponse:
    return json_error(getattr(err, "status", 401), getattr(err, "message", None) or "Unauthorized", **extra)


def _require_interview_access(request: Request, interview_id: str) -> str:
    """Interview session token (candidate) or admin bearer (review)."""
    resume_id = as_uuid(interview_id)
    if not resume_id:
        raise ApiError(400, "Invalid interview id")
    token = bearer_token(request)
    if verify_interview_token(token, resume_id):
        return resume_id
    if is_admin_request(request):
        return resume_id
    raise ApiError(401, "Interview session required")


@router.post("/access/request-code")
def request_access_code(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"interview:code:ip:{ip}"], 3, 10 * 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many attempts. Please try again later.")
    try:
        email = as_email(body.get("email"))
        if not email:
            return JSONResponse({"success": False, "message": "Email address is required"}, status_code=400)
        account_limit = is_rate_limited_any([f"interview:code:acct:{email}"], 3, 10 * 60_000)
        if account_limit.limited:
            return rate_limited_response(account_limit, "Too many attempts. Please try again later.")
        session = sessions.get_session_by_email(email)
        challenge = sessions.issue_access_code(email) if session else None
        if not challenge:
            return {"success": True, "message": "If this email is eligible, a verification code will be sent."}
        code, challenge_value = challenge
        if not resumes.get_resume(session["resumeId"]):
            sessions.clear_access_code(email, challenge_value)
            return {"success": True, "message": "If this email is eligible, a verification code will be sent."}
        html = (
            "<p>Your TalentScope interview verification code is:</p>"
            f"<p style=\"font-size:24px;font-weight:bold;letter-spacing:5px\">{escape(code)}</p>"
            f"<p>This code expires in {sessions.ACCESS_CODE_TTL_SECONDS // 60} minutes and can be used once.</p>"
            "<p>If you did not request this code, you can ignore this email.</p>"
        )
        status, send_error = mailer.dispatch(email, "TalentScope interview verification code", html)
        if status != "sent":
            sessions.clear_access_code(email, challenge_value)
            log.error("Interview verification email failed for %s: %s", email, send_error or status)
            return json_error(503, "Unable to send a verification code. Please contact HR.")
        return {"success": True, "message": "If this email is eligible, a verification code will be sent."}
    except Exception as err:
        log.error("interview/access/request-code: %s", err)
        return public_error(err, "Unable to send a verification code")


@router.post("/access")
def access(request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    ip_limit = is_rate_limited_any([f"interview:verify:ip:{ip}"], 10, 10 * 60_000)
    if ip_limit.limited:
        return rate_limited_response(ip_limit, "Too many attempts. Please try again later.")
    email = as_email(body.get("email"))
    code = body.get("code")
    password = body.get("password")
    if not email:
        return json_error(400, "Enter the email address used for your assessment invitation.")
    if password is None and (not isinstance(code, str) or not re.fullmatch(r"\d{6}", code)):
        return json_error(400, "Enter the temporary password from your invitation email.")
    if password is not None and (not isinstance(password, str) or not password or len(password) > 256):
        return json_error(400, "Enter the temporary password from your invitation email.")
    account_limit = is_rate_limited_any([f"interview:verify:acct:{email}"], 5, 10 * 60_000)
    if account_limit.limited:
        return rate_limited_response(account_limit, "Too many sign-in attempts. Wait a few minutes before trying again.")
    try:
        session = (
            sessions.verify_candidate_password(email, password)
            if password is not None
            else sessions.verify_access_code(email, code)
        )
    except Exception as err:
        log.error("Candidate sign-in could not verify stored credentials for %s: %s", email, err)
        return public_error(err, "Candidate sign-in could not be completed. Please contact HR.", http_status=500)
    if not session:
        if password is not None:
            return json_error(401, "Sign-in failed. Check your email and temporary password, or ask HR to resend the invitation.")
        return json_error(401, "The verification code is invalid or expired. Request a new code and try again.")
    resume_id = session["resumeId"]
    if not resumes.get_resume(resume_id):
        return json_error(404, "Associated resume record was not found. Please contact HR.")
    return {"success": True, "resumeId": resume_id, "interviewToken": sign_interview_token(resume_id)}


@router.get("/{interview_id}/questions")
async def questions(interview_id: str, request: Request):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err)
    try:
        if not await anyio.to_thread.run_sync(resumes.get_resume, resume_id):
            return json_error(404, "Resume not found")
        result = await question_queue.ensure_async(resume_id)
        if not result:
            return json_error(500, "Failed to generate interview questions")
        return result
    except LookupError:
        return json_error(404, "Resume not found")
    except Exception as err:
        log.error("GET questions: %s", err)
        return json_error(500, "Failed to fetch interview questions")


@router.get("/{interview_id}/answers")
def answers(interview_id: str, request: Request):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err)
    try:
        rows = get_store().select("interview_attempts", {"resume_id": resume_id}, ["question_index", "candidate_answer"])
        return [{"question_index": r["question_index"], "answer": r["candidate_answer"]} for r in rows]
    except Exception as err:
        return public_error(err, "Failed to fetch answers")


@router.get("/{interview_id}/timing")
def timing(interview_id: str, request: Request):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err, status="error")
    try:
        return interviews.timing(resume_id)
    except Exception as err:
        return public_error(err, "Failed to load interview timing", status="error")


@router.post("/{interview_id}/start")
def start(interview_id: str, request: Request):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err, status="error")
    try:
        data = interviews.mark_started(resume_id)
        write_log("candidate-processing", "INTERVIEW_STARTED", "success", f"Interview {resume_id} started")
        return {"status": "success", "data": data}
    except Exception as err:
        return public_error(err, "Failed to start interview", status="error")


@router.post("/{interview_id}/submit_answer")
def submit_answer(interview_id: str, request: Request, body: dict = Body(default_factory=dict)):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err, status="error")
    try:
        try:
            index = int(body.get("question_index"))
        except (TypeError, ValueError):
            return json_error(400, "question_index must be an integer", status="error")
        question = body.get("question")
        if not question or not isinstance(question, str) or len(question) > 20000:
            return json_error(400, "question text is required", status="error")
        answer = body.get("answer") or ""
        if not isinstance(answer, str):
            answer = str(answer)
        if len(answer) > 100000:
            return json_error(400, "Answer is too long", status="error")
        if interviews.is_expired(resume_id):
            return json_error(409, "The interview time limit has passed", status="error")
        interviews.evaluate_answer(resume_id, index, question, answer)
        return {"status": "success"}
    except Exception as err:
        return public_error(err, "Submit failed", status="error")


@router.post("/{interview_id}/conclude")
def conclude(interview_id: str, request: Request):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err)
    resume = resumes.get_resume(resume_id, True) if resume_id else None
    if not resume:
        return json_error(404, "Resume record not found")
    try:
        email = (resume.get("parsed") or {}).get("personal", {}).get("email")
        if email:
            sessions.mark_email_session_used(email)
        write_log("candidate-processing", "INTERVIEW_FINISHED", "success", f"Interview {resume_id} finished")
        return {"success": True}
    except Exception as err:
        return public_error(err, "Conclude failed")


@router.post("/{interview_id}/proctor_violation")
def proctor_violation(interview_id: str, request: Request, body: dict = Body(default_factory=dict)):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err)
    violation_type, warning_count = body.get("violationType"), body.get("warningCount")
    if not violation_type or not isinstance(warning_count, (int, float)) or isinstance(warning_count, bool):
        return json_error(400, "Missing required fields: violationType, warningCount")
    resume = resumes.get_resume(resume_id, True) if resume_id else None
    if not resume:
        return json_error(404, "Candidate record not found")
    try:
        report = resume.get("report") or {}
        proctoring = report.setdefault("proctoring", {"warningCount": 0, "violations": [], "autoSubmitted": False, "eventCount": 0})
        silent = bool(body.get("silent"))
        if not silent:
            proctoring["warningCount"] = warning_count
        proctoring["eventCount"] = (proctoring.get("eventCount") or 0) + 1
        num = lambda key: body.get(key) if isinstance(body.get(key), (int, float)) and not isinstance(body.get(key), bool) else None
        proctoring["violations"].append({
            "type": violation_type,
            "timestamp": body.get("timestamp") or _now(),
            "warningCount": (proctoring.get("warningCount") or 0) if silent else warning_count,
            "duration": num("duration"),
            "confidence": num("confidence"),
            "description": body.get("description") or None,
            "videoTimestamp": num("videoTimestamp"),
            "silent": silent,
        })
        if not silent and warning_count >= 3:
            proctoring["autoSubmitted"] = True
        resume["report"] = report
        resumes.save_row(resume)
        return {"success": True, "proctoring": proctoring}
    except Exception as err:
        return public_error(err, "Internal server error")


@router.post("/{interview_id}/run_code")
def run_code(interview_id: str, request: Request, body: dict = Body(default_factory=dict)):
    try:
        _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err)
    question, language, code = body.get("question"), body.get("language"), body.get("code")
    if not question or not language or not code:
        return json_error(400, "Missing required fields: question, language, code")
    prompt = f"""You are an elite automated code runner, compiler simulator, and grading engine.
Compile and run the candidate's code against exactly 3 distinct test cases (normal inputs and edge cases) for the coding challenge, strictly simulating execution in the specified language.

Coding Challenge:
"{question}"

Programming Language:
"{language}"

Candidate's Code Solution:
```{language}
{code}
```

STEPS:
1. SYNTAX & COMPILATION CHECK: the code must use the syntax of "{language}". If it belongs to a different language (for example Python indentation or "def" in a JavaScript/TypeScript/Java/C++ environment, or braces and "const/function" in Python), set "compiles" to false and put a compiler-like message in "error". Also fail on ordinary syntax errors. When compilation fails, every test case has "passed": false and "actual" shows the error.
2. TEST CASES: create exactly 3: standard input, slightly different bounds or parameters, and an edge case (empty input, null, zero, single item, boundary, case sensitivity).
3. DRY RUN: if compilation succeeded, trace the code against each test case, record the exact return value or stdout as "actual", and set "passed" by comparing with "expected".
4. SCORE 0 to 10 from how many cases pass and general correctness (all pass = 10, partial = 4-8, compilation failure = 0).

Return ONLY a JSON object, no markdown:
{{"compiles": true, "error": null, "testCases": [{{"name": "Test Case 1: ...", "input": "...", "expected": "...", "actual": "...", "passed": true}}], "score": 10}}"""
    try:
        result = ai.generate_json(prompt, max_tokens=1536, timeout_s=300)
        if not isinstance(result, dict):
            raise llm.LlmError("Unexpected response from the code evaluator")
        return result
    except llm.LlmError as err:
        return json_error(503, f"Code evaluation is unavailable: {err}", compiles=False, testCases=[], score=0)
    except Exception as err:
        return public_error(err, "Compiler Sandbox Error", compiles=False, testCases=[], score=0)


def _decode_image(value: str) -> tuple[str, bytes]:
    match = re.match(r"^data:([^;]+);base64,(.+)$", value, re.S)
    mime, data = (match.group(1), match.group(2)) if match else ("image/jpeg", value)
    return mime, base64.b64decode(data, validate=False)


@router.post("/{interview_id}/verify_id")
def verify_id(interview_id: str, request: Request, body: dict = Body(default_factory=dict)):
    ip = get_client_ip(request)
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err)
    resume = resumes.get_resume(resume_id, True) if resume_id else None
    if not resume:
        return json_error(404, "Resume record not found")
    try:
        id_image, selfie_image, id_type = body.get("idImage"), body.get("selfieImage"), body.get("idType")
        if not id_image or not selfie_image or not isinstance(id_image, str) or not isinstance(selfie_image, str):
            return json_error(400, "ID image and Selfie snapshot are required")
        if not identity.is_government_id_type(id_type):
            return json_error(400, "Select a valid government ID type (Aadhar Card, Driving License, PAN Card, or Voter ID).",
                              failureCode="invalid_id_type")
        _, id_bytes = _decode_image(id_image)
        _, selfie_bytes = _decode_image(selfie_image)
        if len(id_bytes) > identity.ID_UPLOAD_MAX_BYTES or len(selfie_bytes) > identity.ID_UPLOAD_MAX_BYTES:
            return json_error(413, f"Image too large. Upload an ID photo of {identity.ID_UPLOAD_MAX_LABEL} or smaller.")
        if len(id_bytes) < 2000 or len(selfie_bytes) < 2000:
            return json_error(400, "Image appears empty or corrupted. Please recapture and try again.")
        storage.save("verifications", f"{resume_id}_id.jpg", id_bytes)
        storage.save("verifications", f"{resume_id}_selfie.jpg", selfie_bytes)
        result = identity.verify_candidate_identity(
            id_image, selfie_image, id_type,
            body.get("idDescriptor") if isinstance(body.get("idDescriptor"), list) else None,
            body.get("selfieDescriptor") if isinstance(body.get("selfieDescriptor"), list) else None,
        )
        system_error = bool(result.get("isSystemError"))
        resume["report"] = {**(resume.get("report") or {}), "verification": {
            "status": "system_error" if system_error else "verified" if result["matched"] else "failed",
            "matched": result["matched"], "confidence": result["confidence"], "reason": result["reason"],
            "selectedIdType": result["selectedIdType"], "detectedIdType": result["detectedIdType"],
            "idTypeMatched": result["idTypeMatched"], "faceMatched": result["faceMatched"],
            "failureCode": result.get("failureCode"), "engine": result["engine"], "verifiedAt": _now(),
            "idImageUrl": f"/api/interview/{resume_id}/verification/id",
            "selfieImageUrl": f"/api/interview/{resume_id}/verification/selfie",
            "systemError": system_error,
        }}
        resumes.save_row(resume)
        label = identity.id_type_label(id_type)
        audit.add_log(
            (resume.get("parsed") or {}).get("personal", {}).get("email") or f"candidate_{resume_id}",
            "CANDIDATE_IDENTITY_SYSTEM_ERROR" if system_error else "CANDIDATE_IDENTITY_VERIFIED" if result["matched"] else "CANDIDATE_IDENTITY_FAILED",
            resume_id,
            f"Face service unavailable. ID ({label}) and Selfie saved for manual review." if system_error else
            f"Type={id_type} typeOk={result['idTypeMatched']} faceOk={result['faceMatched']} confidence={result['confidence']}% engine={result['engine']}. {result['reason']}",
            ip,
        )
        return {
            "success": True, "matched": result["matched"], "confidence": result["confidence"], "reason": result["reason"],
            "selectedIdType": result["selectedIdType"], "detectedIdType": result["detectedIdType"],
            "idTypeMatched": result["idTypeMatched"], "faceMatched": result["faceMatched"],
            "failureCode": result.get("failureCode"), "engine": result["engine"], "isSystemError": system_error,
        }
    except Exception as err:
        return public_error(err, "Verification processing failed")


def _verification_image(request: Request, interview_id: str, kind: str):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err)
    for ext, mime in (("jpg", "image/jpeg"), ("jpeg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp")):
        data = storage.read("verifications", f"{resume_id}_{kind}.{ext}")
        if data is not None:
            return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})
    return json_error(404, "Verification image not found")


@router.get("/{interview_id}/verification/id")
def verification_id_image(interview_id: str, request: Request):
    return _verification_image(request, interview_id, "id")


@router.get("/{interview_id}/verification/selfie")
def verification_selfie_image(interview_id: str, request: Request):
    return _verification_image(request, interview_id, "selfie")


@router.post("/{interview_id}/upload_video")
def upload_video(interview_id: str, request: Request, video: UploadFile = File(None), duration: str = Form(None)):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err)
    if video is None:
        return json_error(400, "No video file provided")
    try:
        resume = resumes.get_resume(resume_id, True)
        if not resume:
            return json_error(404, "Associated resume record not found")
        try:
            storage.save_stream("recordings", f"{resume_id}.webm", video.file, VIDEO_MAX_BYTES, WEBM_SIGNATURE)
        except ValueError as err:
            return json_error(400, str(err))
        try:
            seconds = float(duration) if duration else None
        except ValueError:
            seconds = None
        resume["report"] = {**(resume.get("report") or {}), "videoUrl": f"/api/interview/{resume_id}/video", "videoDuration": seconds}
        resumes.save_row(resume)
        return {"success": True, "videoUrl": resume["report"]["videoUrl"]}
    except Exception as err:
        return public_error(err, "Upload video failed")


@router.get("/{interview_id}/video")
def video(interview_id: str, request: Request):
    try:
        resume_id = _require_interview_access(request, interview_id)
    except ApiError as err:
        return _auth_error(err)
    path = storage.resolve("recordings", f"{resume_id}.webm") if resume_id else None
    if path is None:
        return json_error(404, "Video file not found")
    return FileResponse(path, media_type="video/webm", headers={**NO_CACHE, "Accept-Ranges": "bytes"})
