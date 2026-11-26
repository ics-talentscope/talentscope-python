import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, File, Form, Request, UploadFile

from core.auth import employee_id_from_request, require_employee
from core.db import get_store
from core.errors import json_error, public_error
from core.validation import as_path_id
from services import accounts, employee_tests as et, assessment_attempts, storage, test_backup, test_video
from services.products import format_topic_title_for_display
from services.structured_log import write_log

log = logging.getLogger("talentscope")
router = APIRouter(prefix="/api/employee/tests", tags=["employee-tests"])

LIST_COLUMNS = ["id", "topic_id", "subject_id", "difficulty", "total_questions", "status", "started_at", "completed_at",
                "topic_title", "subject_title", "score_correct", "score_percent"]
VIDEO_MAX_BYTES = 120 * 1024 * 1024
CHUNK_ROOT = "video_upload_chunks"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _title(row: dict) -> dict:
    return {**row, "topic_title": format_topic_title_for_display(row.get("topic_title"))}


def _owned(test_id: str, employee: dict):
    return et.get_owned_test(test_id, employee)


def _mark_video_ready(test: dict, url: str) -> None:
    proctoring = et.mark_video_uploaded(et.normalize_proctoring(test.get("proctoring")))
    get_store().update("tests", {"session_recording_url": url, "proctoring": proctoring}, {"id": test["id"]})


def _employee_questions(test: dict) -> tuple[list[dict], list[dict]]:
    questions = et.questions_for(test["id"])
    if test.get("status") != "completed":
        return [
            {key: value for key, value in question.items() if key not in ("correct_option_index", "explanation")}
            for question in questions
        ], []

    attempts = get_store().select(
        "test_attempts",
        {"test_id": test["id"]},
        ["id", "question_id", "selected_option_index", "is_correct", "time_taken_seconds", "created_at"],
    )
    attempted_ids = {str(attempt["question_id"]) for attempt in attempts}
    return [question for question in questions if str(question["id"]) in attempted_ids], attempts


@router.get("")
def list_tests(request: Request):
    code = employee_id_from_request(request)
    employee = accounts.get_account(code) if code else None
    if not employee:
        return []
    try:
        emp_uuid = accounts.employee_uuid(employee["employee_id"])
        if not emp_uuid:
            return []
        rows = get_store().select("tests", {"employee_id": emp_uuid}, LIST_COLUMNS, "started_at desc", limit=10)
        return [_title(r) for r in rows]
    except Exception as err:
        log.error("GET /employee/tests error: %s", err)
        return []


@router.get("/{test_id}")
def get_test(test_id: str, employee: dict = Depends(require_employee)):
    try:
        test = _owned(test_id, employee)
        if not test:
            return json_error(404, "Not found")
        if et.finalize_if_expired(test):
            test = _owned(test_id, employee)
        topic = test.get("topic_id")
        is_product_assessment = accounts.is_product_assessment_topic(topic)
        if is_product_assessment and not accounts.is_product_qb_employee(employee):
            return json_error(403, "Forbidden")
        if (accounts.is_assessment_only_employee(employee) and not is_product_assessment
                and not accounts.is_product_assessment_history_topic(topic)):
            return json_error(403, "Forbidden")
        questions, attempts = _employee_questions(test)
        if not questions and test.get("status") != "completed":
            return json_error(400, "This assessment has no questions assigned yet. Please contact your administrator.")
        write_log("employee", "EMPLOYEE_TEST_OPENED", "success", f"{employee['employee_id']} opened test {test['id']} ({test.get('topic_title') or topic})")
        return {"test": _title(test), "questions": questions, "attempts": attempts, "has_recording": test_video.exists(test["id"])}
    except Exception as err:
        return public_error(err, "Internal error")


@router.patch("/{test_id}")
def patch_test(test_id: str, body: dict = Body(default_factory=dict), employee: dict = Depends(require_employee)):
    try:
        test = _owned(test_id, employee)
        if not test:
            return json_error(404, "Test not found")
        problem = et.can_patch_progress(test, body.get("status"))
        if problem:
            return json_error(409, problem)
        if body.get("status") == "in_progress" and (test.get("status") or "pending") == "pending" and body.get("camera_granted") is not True:
            return json_error(400, "Camera access is required to start the assessment.")
        # Enforce assessment-level attempt restrictions on the backend:
        # one attempt per assessment, retakes require administrator authorization.
        if body.get("status") == "in_progress" and (test.get("status") or "pending") == "pending":
            try:
                emp_uuid = accounts.employee_uuid(employee["employee_id"])
                assessment_attempts.start_attempt(
                    emp_uuid, employee["employee_id"], test.get("topic_id") or "", test["id"]
                )
            except PermissionError as err:
                return json_error(409, str(err) or "Assessment attempt not allowed.")
        updates = {k: body[k] for k in ("in_progress", "current_question_index", "status", "started_at") if k in body}
        if body.get("status") == "in_progress" and body.get("started_at"):
            updates["proctoring"] = et.mark_session_started(et.normalize_proctoring(test.get("proctoring")))
        if updates:
            get_store().update("tests", updates, {"id": test["id"]})
        return {"success": True, "data": get_store().first("tests", {"id": test["id"]})}
    except Exception as err:
        return public_error(err, "Internal error")


@router.delete("/{test_id}")
def reset_test(test_id: str, employee: dict = Depends(require_employee)):
    # Retakes are admin-controlled only. An employee cannot reset their own assessment:
    # self-service resets skipped the fresh-session setup (fullscreen, camera, proctoring).
    # Keep the route so stale clients get a clear message instead of a generic 405.
    return json_error(403, "Employees cannot reset their own assessment. Contact your administrator to reset it for a retake.")


@router.post("/{test_id}/start")
def start_test(test_id: str, employee: dict = Depends(require_employee)):
    try:
        test = _owned(test_id, employee)
        if not test:
            return json_error(404, "Test not found")
        if test.get("status") == "completed":
            return json_error(409, "Test already submitted.")
        questions, _ = _employee_questions(test)
        if not questions:
            return json_error(400, "Test has no questions")
        return {"test": test, "questions": questions, "total": len(questions)}
    except Exception as err:
        return public_error(err, "Internal error")


@router.post("/{test_id}/submit")
def submit_test(test_id: str, body: dict = Body(default_factory=dict), employee: dict = Depends(require_employee)):
    try:
        answers = body.get("answers") if body.get("answers") is not None else []
        auto = body.get("autoSubmitted") is True
        if not isinstance(answers, list):
            return json_error(400, "Invalid answers")
        if not answers and not auto:
            return json_error(400, "Empty answers")
        test = _owned(test_id, employee)
        if not test:
            return json_error(404, "Test not found")
        if test.get("status") == "completed":
            total = test.get("total_questions") or 25
            correct = test.get("score_correct") or 0
            accuracy = test.get("score_percent")
            if accuracy is None:
                accuracy = round(correct / total * 100) if total else 0
            review_questions, review_attempts = _employee_questions(test)
            return {"success": True, "alreadyCompleted": True, "testId": test["id"], "total": total, "correct": correct,
                    "accuracy": accuracy, "ai_analysis": test.get("ai_analysis"),
                    "questions": review_questions, "attempts": review_attempts}
        ok, error, code = et.can_submit(test)
        if not auto and not ok and code == "NOT_STARTED":
            return json_error(400, error)
        result = et.finalize(test, accounts.employee_uuid(employee["employee_id"]), answers, auto)
        test_backup.snapshot(test["id"], "submit")
        completed_test = get_store().first("tests", {"id": test["id"]})
        review_questions, review_attempts = _employee_questions(completed_test)
        correct, total, accuracy = result["correct"], result["total"], result["accuracy"]
        write_log("employee", "EMPLOYEE_TEST_SUBMITTED", "success",
                  f"{employee['employee_id']} submitted test {test['id']}: {correct}/{total} ({accuracy}%){' [auto-submitted]' if auto else ''}")
        return {"success": True, "testId": test["id"], "total": total, "correct": correct, "accuracy": accuracy,
                "ai_analysis": None, "questions": review_questions, "attempts": review_attempts}
    except Exception as err:
        return public_error(err, "Internal error")


@router.post("/{test_id}/proctor_violation")
def proctor_violation(test_id: str, body: dict = Body(default_factory=dict), employee: dict = Depends(require_employee)):
    try:
        test = _owned(test_id, employee)
        if not test:
            return json_error(404, "Test not found")
        if test.get("status") == "completed":
            return {"success": True, "proctoring": et.normalize_proctoring(test.get("proctoring")), "autoSubmit": False, "ignored": True}
        ok, error, _ = et.can_submit(test)
        if not ok and test.get("status") != "in_progress":
            return json_error(400, error)
        vtype = str(body.get("violationType") or "Unknown Violation")[:120]
        existing = et.normalize_proctoring(test.get("proctoring"))
        updated = et.record_violation(
            existing, vtype, str(body["detail"])[:240] if body.get("detail") else None,
            str(body["category"]) if body.get("category") else None, str(body["severity"]) if body.get("severity") else None)
        get_store().update("tests", {"proctoring": updated}, {"id": test["id"]})
        test_backup.snapshot(test["id"], "proctor")
        # Keep autoSubmit true on every later violation too, so a client that
        # missed the crossing response can still recover and submit the test.
        auto = updated["autoSubmitted"] and updated["warningCount"] >= et.MAX_VIOLATIONS
        return {"success": True, "proctoring": updated, "autoSubmit": auto}
    except Exception as err:
        return public_error(err, "Internal error")


@router.post("/{test_id}/upload_video")
async def upload_video(test_id: str, video: UploadFile = File(None), employee: dict = Depends(require_employee)):
    if not as_path_id(test_id):
        return json_error(400, "Invalid test id")
    try:
        test = _owned(test_id, employee)
        if not test:
            return json_error(404, "Test not found")
        if video is None:
            return json_error(400, "No video file provided")
        buffer = await video.read()
        if not buffer:
            return json_error(400, "Recording file is empty")
        if len(buffer) > VIDEO_MAX_BYTES:
            return json_error(400, "File too large (max 120MB)")
        if not test_video.save_lenient(test["id"], buffer):
            return json_error(500, "Failed to store recording")
        url = test_video.admin_url(test["id"])
        _mark_video_ready(test, url)
        test_backup.snapshot(test["id"], "video")
        return {"success": True, "videoUrl": url}
    except Exception as err:
        return public_error(err, "Upload failed")


@router.post("/{test_id}/upload_video/progress")
async def upload_progress(test_id: str, video: UploadFile = File(None), employee: dict = Depends(require_employee)):
    try:
        test = _owned(test_id, employee)
        if not test:
            return json_error(404, "Test not found")
        if (test.get("status") or "pending") not in ("in_progress", "completed"):
            return json_error(400, "Recording upload not allowed for this test state.")
        if video is None:
            return json_error(400, "No video file provided")
        buffer = await video.read()
        if not buffer:
            return json_error(400, "Recording file is empty")
        if not test_video.save_progress(test["id"], buffer):
            return json_error(500, "Failed to store recording progress")
        url = test_video.admin_url(test["id"])
        _mark_video_ready(test, url)
        return {"success": True, "bytes": len(buffer), "videoUrl": url}
    except Exception as err:
        return public_error(err, "Progress upload failed")


@router.post("/{test_id}/upload_video/chunk")
async def upload_chunk(test_id: str, chunk: UploadFile = File(None), chunkIndex: str = Form(None), totalChunks: str = Form(None),
                       employee: dict = Depends(require_employee)):
    try:
        test = _owned(test_id, employee)
        if not test:
            return json_error(404, "Test not found")
        try:
            index, total = int(chunkIndex), int(totalChunks)
        except (TypeError, ValueError):
            return json_error(400, "Invalid chunk payload")
        if total < 1 or index < 0 or index >= total or chunk is None:
            return json_error(400, "Invalid chunk payload")
        data = await chunk.read()
        bucket = f"{CHUNK_ROOT}/{test['id']}"
        storage.disk_path(bucket, f"{index}.part").write_bytes(data)
        if any(not storage.disk_path(bucket, f"{i}.part").exists() for i in range(total)):
            return {"complete": False, "received": index, "totalChunks": total}
        full = b"".join(storage.disk_path(bucket, f"{i}.part").read_bytes() for i in range(total))
        if not full:
            return json_error(400, "Recording file is empty")
        if len(full) > VIDEO_MAX_BYTES:
            return json_error(400, "File too large (max 120MB)")
        if not test_video.save_lenient(test["id"], full):
            return json_error(500, "Failed to store recording")
        url = test_video.admin_url(test["id"])
        _mark_video_ready(test, url)
        test_backup.snapshot(test["id"], "video")
        storage.drop_disk(bucket)
        return {"complete": True, "success": True, "videoUrl": url, "bytes": len(full)}
    except Exception as err:
        return public_error(err, "Chunk upload failed")
