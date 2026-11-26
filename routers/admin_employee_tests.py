import hashlib
import re

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response

from core.auth import require_admin, require_permission
from core.db import get_store
from core.errors import json_error, public_error
from core.validation import as_uuid
from services import assessment_attempts, employee_tests as et, test_backup, test_video
from services.structured_log import write_log

router = APIRouter(prefix="/api/admin/employee-tests", tags=["admin-employee-tests"], dependencies=[Depends(require_permission("employeePortal"))])

NAME_RE = re.compile(r"^[\w.\- ()]+$")
HEADERS = {"Content-Type": "video/webm; codecs=vp8", "Accept-Ranges": "bytes", "Cache-Control": "private, no-cache"}


@router.get("/{test_id}/attempts")
def attempts(test_id: str):
    tid = as_uuid(test_id)
    if not tid:
        return json_error(400, "Test ID is required")
    try:
        test = get_store().first("tests", {"id": tid}, ["id", "status"])
        if not test:
            return json_error(404, "Test not found")
        return {"success": True, "testId": tid, "status": test["status"], "questions": et.question_attempts_batch([tid], include_unanswered=True).get(tid, [])}
    except Exception as err:
        return public_error(err, "Failed to load test attempts")


@router.get("/{test_id}/backup")
def backup(test_id: str):
    tid = as_uuid(test_id)
    if not tid:
        return json_error(400, "Test ID is required")
    saved = test_backup.read_latest(tid)
    if not saved:
        return json_error(404, "No backup saved for this test")
    return {"success": True, "testId": tid, "backup": saved}


@router.get("/{test_id}/video")
def video(test_id: str, request: Request):
    tid = as_uuid(test_id)
    data = test_video.read(tid) if tid else None
    if not data:
        return json_error(404, "Recording not found or invalid. The test may have finished before video was saved — ask the employee to retake after confirming camera access.")
    name = request.query_params.get("filename") or ""
    safe = name if NAME_RE.match(name) and name.lower().endswith(".webm") else f"{tid}.webm"
    disposition = f'{"inline" if request.query_params.get("inline") == "1" else "attachment"}; filename="{safe}"'
    size = len(data)
    etag = f'"{hashlib.md5(data).hexdigest()}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": HEADERS["Cache-Control"]})
    base = {**HEADERS, "ETag": etag, "Content-Disposition": disposition}
    header = request.headers.get("range")
    if header:
        m = re.match(r"bytes=(\d*)-(\d*)", header)
        start = int(m.group(1)) if m and m.group(1) else 0
        end = min(int(m.group(2)) if m and m.group(2) else size - 1, size - 1)
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        return Response(data[start:end + 1], status_code=206,
                        headers={**base, "Content-Range": f"bytes {start}-{end}/{size}"})
    return Response(data, headers=base)


@router.post("/attempts/{attempt_id}/authorize-retake")
def authorize_retake(attempt_id: str, body: dict = Body(default_factory=dict),
                     admin: str = Depends(require_admin)):
    """Administrator authorizes a retake for a completed assessment attempt."""
    tid = as_uuid(attempt_id)
    if not tid:
        return json_error(400, "Invalid attempt id")
    ok = assessment_attempts.authorize_retake(tid, admin or "")
    if not ok:
        return json_error(404, "Assessment attempt not found")
    write_log("admin", "ASSESSMENT_RETAKE_AUTHORIZED", "success",
              f"{admin} authorized a retake for attempt {attempt_id}")
    return {"success": True, "attemptId": attempt_id, "isRetakeAuthorized": True}
