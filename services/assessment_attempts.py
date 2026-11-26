"""Assessment attempt management.

Implements the meeting's attempt restrictions on top of the existing
``tests`` / ``test_questions`` / ``test_attempts`` delivery tables:

* one attempt per candidate per assessment (topic),
* no second attempt for an individual question once submitted,
* a further attempt requires administrator authorization
  (``is_retake_authorized``).

This is enforced on the backend, not only through frontend controls.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from core import tenancy
from core.db import get_store

log = logging.getLogger("talentscope")

TABLE = "assessment_attempts"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_ABANDONED = "abandoned"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_id() -> str:
    return tenancy.current_client()


def _row(attempt: dict) -> dict:
    return {
        "id": attempt.get("id"),
        "employeeId": attempt.get("employee_id"),
        "employeeUuid": attempt.get("employee_uuid"),
        "topicId": attempt.get("topic_id"),
        "testId": attempt.get("test_id"),
        "attemptNumber": attempt.get("attempt_number"),
        "startedAt": attempt.get("started_at"),
        "completedAt": attempt.get("completed_at"),
        "status": attempt.get("status"),
        "score": attempt.get("score"),
        "isRetakeAuthorized": bool(attempt.get("is_retake_authorized")),
        "authorizedBy": attempt.get("authorized_by"),
        "authorizedAt": attempt.get("authorized_at"),
        "createdAt": attempt.get("created_at"),
    }


def list_attempts(employee_uuid: str, topic_id: str) -> list[dict]:
    store = get_store()
    rows = store.select(
        TABLE,
        {"employee_uuid": employee_uuid, "topic_id": topic_id},
        order="attempt_number desc",
    )
    return [_row(r) for r in rows]


def get_latest_attempt(employee_uuid: str, topic_id: str) -> Optional[dict]:
    rows = list_attempts(employee_uuid, topic_id)
    return rows[0] if rows else None


def can_start_assessment(employee_uuid: str, topic_id: str) -> tuple[bool, Optional[str]]:
    """Whether a candidate may start a new attempt for an assessment.

    Returns (allowed, reason). A candidate may (re)start when:
      * no attempt exists yet, or
      * the latest attempt is in progress / abandoned, or
      * the latest attempt is completed AND retake is authorized.
    """
    latest = get_latest_attempt(employee_uuid, topic_id)
    if not latest:
        return True, None
    status = latest.get("status")
    if status in (STATUS_IN_PROGRESS, STATUS_ABANDONED):
        return True, None
    if status == STATUS_COMPLETED and latest.get("isRetakeAuthorized"):
        return True, None
    if status == STATUS_COMPLETED:
        return False, "Assessment already completed. A retake requires administrator authorization."
    return True, None


def start_attempt(
    employee_uuid: str,
    employee_id: str,
    topic_id: str,
    test_id: str,
) -> dict:
    """Start (or resume) an assessment attempt, enforcing the retake rule."""
    allowed, reason = can_start_assessment(employee_uuid, topic_id)
    if not allowed:
        raise PermissionError(reason or "Assessment attempt not allowed.")

    store = get_store()
    existing = store.first(
        TABLE,
        {"employee_uuid": employee_uuid, "topic_id": topic_id, "status": STATUS_IN_PROGRESS},
    )
    if existing:
        # Resume the in-progress attempt; keep its attempt number.
        attempt_number = int(existing.get("attempt_number") or 1)
        store.update(TABLE, {"started_at": _now(), "updated_at": _now()}, {"id": existing["id"]})
        return _row(store.first(TABLE, {"id": existing["id"]}))

    # Determine the next attempt number.
    previous = store.select(
        TABLE,
        {"employee_uuid": employee_uuid, "topic_id": topic_id},
        ["attempt_number"],
        order="attempt_number desc",
        limit=1,
    )
    attempt_number = (int(previous[0]["attempt_number"]) + 1) if previous else 1

    row = {
        "id": str(uuid.uuid4()),
        "client_id": _client_id(),
        "employee_id": employee_id,
        "employee_uuid": employee_uuid,
        "topic_id": topic_id,
        "test_id": test_id,
        "attempt_number": attempt_number,
        "started_at": _now(),
        "status": STATUS_IN_PROGRESS,
        "is_retake_authorized": False,
        "created_at": _now(),
        "updated_at": _now(),
    }
    store.insert(TABLE, row)
    return _row(store.first(TABLE, {"id": row["id"]}))


def complete_attempt(
    test_id: str,
    score: Optional[float],
    status: str = STATUS_COMPLETED,
) -> Optional[dict]:
    """Mark the latest attempt for a delivery test as completed."""
    store = get_store()
    rows = store.select(
        TABLE,
        {"test_id": test_id},
        order="attempt_number desc",
        limit=1,
    )
    if not rows:
        return None
    attempt = rows[0]
    store.update(
        TABLE,
        {
            "status": status,
            "completed_at": _now(),
            "score": score,
            "updated_at": _now(),
        },
        {"id": attempt["id"]},
    )
    return _row(store.first(TABLE, {"id": attempt["id"]}))


def authorize_retake(attempt_id: str, admin_email: str) -> bool:
    """Administrator authorizes a retake for a completed attempt."""
    store = get_store()
    attempt = store.first(TABLE, {"id": attempt_id})
    if not attempt:
        return False
    store.update(
        TABLE,
        {
            "is_retake_authorized": True,
            "authorized_by": admin_email,
            "authorized_at": _now(),
            "updated_at": _now(),
        },
        {"id": attempt_id},
    )
    return True
