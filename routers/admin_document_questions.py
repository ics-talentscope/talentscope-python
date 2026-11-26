"""Admin endpoints for document-based question upload and extraction.

Endpoints:
  POST /api/admin/document-questions/parse
    Upload a .docx or .pdf question document; returns extracted questions for
    admin preview (nothing is published).
  POST /api/admin/document-questions/publish
    Publish admin-approved extracted questions as test_questions for an
    assessment/test.

Nothing is written to the delivery tables until the admin explicitly publishes,
matching the meeting requirement: "Provide an admin preview before publishing
extracted questions."
"""

import logging

from fastapi import APIRouter, Body, Depends, File, Form, Request, UploadFile

from core.auth import require_permission
from core.db import get_store
from core.errors import json_error, public_error
from services import document_questions, employee_tests
from services.structured_log import write_log

log = logging.getLogger("talentscope")
router = APIRouter(prefix="/api/admin/document-questions", tags=["admin-document-questions"],
                   dependencies=[Depends(require_permission("adminControls"))])


@router.post("/parse")
async def parse_document(request: Request, file: UploadFile = File(None)) -> dict:
    """Parse an uploaded question document and return extracted questions for preview."""
    if file is None:
        return json_error(400, "No file provided")
    try:
        data = await file.read()
        if not data:
            return json_error(400, "Uploaded file is empty")
        result = document_questions.extract_document_questions(file.filename or "upload", data)
        actor = admin_email(request)
        write_log("admin", "DOCUMENT_QUESTIONS_PARSED", "success",
                  f"{actor or 'admin'} parsed {file.filename}: {result['validCount']}/{result['questionCount']} valid")
        return {"success": True, **result}
    except ValueError as err:
        return json_error(400, str(err))
    except Exception as err:
        return public_error(err, "Failed to parse question document")


@router.post("/publish")
def publish_questions(
    request: Request,
    body: dict = Body(default_factory=dict),
) -> dict:
    """Publish approved extracted questions into an assessment's test_questions."""
    try:
        test_id = str(body.get("test_id") or body.get("testId") or "").strip()
        topic_id = str(body.get("topic_id") or body.get("topicId") or "").strip() or None
        topic_title = str(body.get("topic_title") or body.get("topicTitle") or "Document Assessment").strip()
        questions = body.get("questions")
        if not isinstance(questions, list) or not questions:
            return json_error(400, "No questions to publish")
        if not test_id:
            return json_error(400, "test_id is required")

        store = get_store()
        test = store.first("tests", {"id": test_id}, ["id", "employee_id", "topic_id", "status"])
        if not test:
            return json_error(404, "Test not found")
        if test.get("status") == "completed":
            return json_error(409, "Cannot publish to a completed assessment.")

        rows = document_questions.questions_to_test_rows(
            test_id=test_id,
            questions=questions,
            topic_id=topic_id or test.get("topic_id") or "",
            topic_title=topic_title,
        )
        if not rows:
            return json_error(400, "No valid questions to publish")

        # Keep only approved (valid) questions.
        rows = [r for r in rows if r.get("question_text") and r.get("options")]
        store.delete("test_questions", {"test_id": test_id})
        store.insert("test_questions", rows)
        store.update("tests", {"total_questions": len(rows), "topic_title": topic_title},
                     {"id": test_id})
        actor = admin_email(request)
        write_log("admin", "DOCUMENT_QUESTIONS_PUBLISHED", "success",
                  f"{actor or 'admin'} published {len(rows)} questions to test {test_id}")
        return {"success": True, "testId": test_id, "questionCount": len(rows)}
    except ValueError as err:
        return json_error(400, str(err))
    except Exception as err:
        return public_error(err, "Failed to publish questions")


def admin_email(request: Request) -> str:
    from core.auth import admin_email as _ae
    return _ae(request) or ""
