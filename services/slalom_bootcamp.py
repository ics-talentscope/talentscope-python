"""Slalom-only bootcamp configuration and read models.

Configuration lives in tenant-scoped bootcamp_* tables. Delivery still uses
``tests`` / ``test_questions`` / ``test_attempts``; the leaderboard is a derived
read-only view over those delivery tables.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from core import tenancy
from core.db import get_store
from services import accounts, employee_tests
from services.portal import js_round

log = logging.getLogger("talentscope")

ASSESSMENTS = "bootcamp_assessments"
COHORTS = "bootcamp_cohorts"
MEMBERS = "bootcamp_cohort_members"
ASSIGNMENTS = "bootcamp_assignments"
CONTENT = "bootcamp_content"

CONTENT_TYPES = {"catalog", "subject", "module", "topic", "source"}
# The learning-content form accepts friendly source types. They are normalised
# to tenant-scoped ``source`` rows; the original label is kept in the payload.
CONTENT_TYPE_ALIASES = {
    "article": "source", "video": "source", "link": "source", "guide": "source",
    "pdf": "source", "document": "source", "reference": "source", "learning": "source",
    "sources": "source", "content": "source",
}
AI_SOURCES = {
    "ai", "ai-questionnaire", "ai-questionaire", "ai questionnaire", "questionnaire", "questionaire", "generated",
}
CONTENT_META_KEYS = {
    "id", "contentId", "content_id", "contentType", "content_type", "contentKey", "content_key",
    "title", "isActive", "is_active", "createdAt", "created_at", "updatedAt", "updated_at", "createdBy", "created_by",
}
TEST_COLUMNS = [
    "id", "employee_id", "employee_code", "topic_id", "topic_title", "subject_id", "subject_title",
    "total_questions", "time_limit_seconds", "status", "started_at", "completed_at",
    "score_correct", "score_total", "score_percent", "proctoring", "created_at",
]


def ensure_slalom_context() -> None:
    if tenancy.current_client() != "slalom":
        raise PermissionError("Bootcamp management is available only for Slalom.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "on")
    return bool(value)


def _first(body: dict, *names, default=None):
    for name in names:
        if name in body and body[name] is not None:
            return body[name]
    return default


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _difficulty_mix_total(value) -> int:
    if not isinstance(value, dict):
        return 0
    total = 0
    for key in ("easy", "medium", "hard"):
        raw = value.get(key)
        if raw in (None, ""):
            continue
        try:
            count = int(raw)
        except (TypeError, ValueError) as err:
            raise ValueError(f"Difficulty count for {key} must be a whole number.") from err
        if count < 0:
            raise ValueError(f"Difficulty count for {key} cannot be negative.")
        total += count
    return total


def _source(value) -> str:
    raw = str(value or "bank").strip().casefold().replace("_", "-")
    return "ai" if raw in AI_SOURCES else "bank"


def _content_type(value) -> str:
    raw = str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")
    return CONTENT_TYPE_ALIASES.get(raw, raw if raw in CONTENT_TYPES else "")


def _slug(value) -> str:
    return "-".join("".join(char if char.isalnum() else " " for char in str(value or "").lower()).split())


def _row(table: str, row_id: str) -> dict:
    row = get_store().first(table, {"id": row_id})
    if not row:
        raise LookupError("Bootcamp record not found.")
    return row


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

def map_assessment(row: dict) -> dict:
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    active = _bool(row.get("is_active"))
    status = str(config.get("status") or ("closed" if not active else "published")).strip().casefold()
    title = row.get("title") or ""
    bank_key = row.get("question_bank_product") or config.get("bankKey") or config.get("bank_key") or ""
    topics = config.get("topics") if isinstance(config.get("topics"), list) else (
        config.get("topicList") if isinstance(config.get("topicList"), list) else []
    )
    return {
        "id": row.get("id"),
        "title": title,
        "name": config.get("name") or title,
        "description": row.get("description") or "",
        "topicId": row.get("topic_id") or accounts.PRODUCT_ASSESSMENT_TOPIC_ID,
        "questionCount": _int(row.get("question_count"), 10),
        "timeLimitSeconds": _int(row.get("time_limit_seconds"), 1800),
        "durationSeconds": _int(row.get("time_limit_seconds"), 1800),
        "durationMinutes": js_round(_int(row.get("time_limit_seconds"), 1800) / 60),
        "passingScore": _int(row.get("passing_score"), 0),
        "questionBankProduct": bank_key,
        "bankKey": bank_key,
        "bank_key": bank_key,
        "product": bank_key,
        "subjectId": row.get("subject_id") or "resource-subject",
        "subjectTitle": row.get("subject_title") or "Bootcamp Assessment",
        "difficulty": row.get("difficulty") or "medium",
        "questionSource": row.get("question_source") or "bank",
        "topics": topics,
        "topicList": topics,
        "difficultyMix": config.get("difficultyMix") or config.get("difficulty_mix") or {},
        "randomizeQuestions": bool(config.get("randomizeQuestions", config.get("randomize_questions", True))),
        "randomizeOptions": bool(config.get("randomizeOptions", config.get("randomize_options", True))),
        "status": status,
        "startsAt": config.get("startsAt") or config.get("starts_at"),
        "endsAt": config.get("endsAt") or config.get("ends_at"),
        "attemptLimit": config.get("attemptLimit") or config.get("attempt_limit"),
        "config": config,
        "isActive": active,
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def map_cohort(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "name": row.get("name") or "",
        "description": row.get("description") or "",
        "isActive": _bool(row.get("is_active")),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def map_member(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "cohortId": row.get("cohort_id"),
        "employeeId": row.get("employee_id"),
        "employeeUuid": row.get("employee_uuid"),
        "employeeName": row.get("employee_name") or "",
        "email": row.get("employee_email") or "",
        "joinedAt": row.get("joined_at") or row.get("created_at"),
    }


def map_assignment(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "cohortId": row.get("cohort_id"),
        "assessmentId": row.get("assessment_id"),
        "employeeId": row.get("employee_id"),
        "testId": row.get("test_id"),
        "status": row.get("status") or "assigned",
        "assignedAt": row.get("assigned_at") or row.get("created_at"),
    }


def map_content(row: dict) -> dict:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    original_type = payload.get("contentType") or payload.get("content_type") or payload.get("type")
    body = _first(payload, "body", "content", "text", "explanation", default="")
    source = _first(payload, "source", "sourceType", "source_type", "sourceKey", "source_key", default="")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "id": row.get("id"),
        "contentType": row.get("content_type"),
        "type": original_type or row.get("content_type"),
        "contentKey": row.get("content_key"),
        "key": row.get("content_key"),
        "title": row.get("title") or payload.get("topic_title") or "",
        "body": body,
        "source": source,
        "metadata": metadata,
        "payload": payload,
        "isActive": _bool(row.get("is_active")),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Assessments
# ---------------------------------------------------------------------------

def get_assessment(assessment_id: str, active_only: bool = False) -> dict:
    row = _row(ASSESSMENTS, str(assessment_id))
    if active_only and not _bool(row.get("is_active")):
        raise LookupError("Bootcamp assessment is not active.")
    return row


def list_assessments(active_only: bool = False) -> list[dict]:
    where = {"is_active": True} if active_only else None
    return [map_assessment(r) for r in get_store().select(ASSESSMENTS, where, order="created_at desc")]


def save_assessment(body: dict, actor: str = "") -> dict:
    ensure_slalom_context()
    assessment_id = str(_first(body, "id", "assessmentId", "assessment_id") or "")
    existing = _row(ASSESSMENTS, assessment_id) if assessment_id else None
    title = " ".join(str(_first(body, "title", "name", "assessmentName",
                                default=(existing or {}).get("title") or "") or "").split())
    if not title or len(title) > 200:
        raise ValueError("Assessment title is required and must be 200 characters or fewer.")
    question_count = _int(
        _first(body, "questionCount", "question_count", "numQuestions", "num_questions",
               default=(existing or {}).get("question_count") or 10), 10
    )
    if not 1 <= question_count <= 500:
        raise ValueError("Question count must be between 1 and 500.")
    stored_config = (existing or {}).get("config")
    stored_config = stored_config if isinstance(stored_config, dict) else {}
    raw_mix = _first(
        body, "difficultyMix", "difficulty_mix",
        default=stored_config.get("difficultyMix") or stored_config.get("difficulty_mix") or {},
    )
    mix_total = _difficulty_mix_total(raw_mix)
    if mix_total > question_count:
        raise ValueError(
            f"Difficulty mix total ({mix_total}) cannot exceed the question count ({question_count})."
        )
    fallback_time = _int((existing or {}).get("time_limit_seconds"), max(1800, question_count * 72))
    raw_time = _first(body, "timeLimitSeconds", "time_limit_seconds", "durationSeconds", "duration_seconds")
    if raw_time is None:
        raw_minutes = _first(body, "durationMinutes", "duration_minutes")
        raw_time = int(float(raw_minutes) * 60) if raw_minutes not in (None, "") else None
    time_limit = _int(raw_time, fallback_time)
    if not 60 <= time_limit <= 86400:
        raise ValueError("Time limit must be between 60 and 86400 seconds.")

    config = dict((existing or {}).get("config") or {}) if isinstance((existing or {}).get("config"), dict) else {}
    if isinstance(body.get("config"), dict):
        config.update(body["config"])
    config.update(body)
    status = str(_first(body, "status", default=config.get("status") or "published") or "published").strip().casefold()
    if status not in {"draft", "published", "closed"}:
        status = "published"
    config["status"] = status
    question_source = _source(_first(body, "questionSource", "question_source", "source", "questionType",
                                     default=(existing or {}).get("question_source") or "bank"))
    bank_key = str(_first(body, "questionBankProduct", "question_bank_product", "bankKey", "bank_key",
                          "questionBank", "bank", "product",
                          default=(existing or {}).get("question_bank_product") or "") or "")
    topic_id = str(_first(body, "topicId", "topic_id",
                          default=(existing or {}).get("topic_id") or accounts.PRODUCT_ASSESSMENT_TOPIC_ID))
    description = str(_first(body, "description", "desc",
                             default=(existing or {}).get("description") or "") or "")
    subject_id = str(_first(body, "subjectId", "subject_id",
                            default=(existing or {}).get("subject_id") or "resource-subject") or "resource-subject")
    subject_title = str(_first(body, "subjectTitle", "subject_title",
                               default=(existing or {}).get("subject_title") or "Bootcamp Assessment")
                        or "Bootcamp Assessment")
    difficulty = str(_first(body, "difficulty", default=(existing or {}).get("difficulty") or "medium") or "medium")
    passing_score = _int(_first(body, "passingScore", "passing_score",
                                default=(existing or {}).get("passing_score") or 0), 0)
    is_active = _bool(_first(body, "isActive", "is_active",
                             default=(existing or {}).get("is_active", status != "closed")))
    if status == "closed":
        is_active = False
    config.update({
        "title": title, "name": config.get("name") or title, "description": description,
        "questionCount": question_count, "question_count": question_count,
        "timeLimitSeconds": time_limit, "time_limit_seconds": time_limit,
        "durationSeconds": time_limit, "duration_seconds": time_limit,
        "bankKey": bank_key, "bank_key": bank_key, "product": bank_key,
        "questionSource": question_source, "question_source": question_source,
        "topicId": topic_id, "topic_id": topic_id,
        "subjectId": subject_id, "subject_id": subject_id,
        "subjectTitle": subject_title, "subject_title": subject_title,
        "difficulty": difficulty, "status": status, "isActive": is_active,
    })
    row = {
        "title": title,
        "description": description,
        "topic_id": topic_id,
        "question_count": question_count,
        "time_limit_seconds": time_limit,
        "passing_score": passing_score,
        "question_bank_product": bank_key,
        "subject_id": subject_id,
        "subject_title": subject_title,
        "difficulty": difficulty,
        "question_source": question_source,
        "config": config,
        "is_active": is_active,
        "updated_at": _now(),
    }
    store = get_store()
    if existing:
        store.update(ASSESSMENTS, row, {"id": existing["id"]})
        saved_id = existing["id"]
    else:
        row.update({"id": str(uuid.uuid4()), "created_at": _now(), "created_by": actor or ""})
        saved_id = store.insert(ASSESSMENTS, row)[0]["id"]
    return map_assessment(_row(ASSESSMENTS, saved_id))


def delete_assessment(assessment_id: str) -> bool:
    ensure_slalom_context()
    get_assessment(assessment_id)
    store = get_store()
    store.delete(ASSIGNMENTS, {"assessment_id": assessment_id})
    return bool(store.delete(ASSESSMENTS, {"id": assessment_id}))


# ---------------------------------------------------------------------------
# Cohorts and roster
# ---------------------------------------------------------------------------

def get_cohort(cohort_id: str) -> dict:
    return _row(COHORTS, str(cohort_id))


def list_cohorts() -> list[dict]:
    store = get_store()
    out = []
    for row in store.select(COHORTS, order="created_at desc"):
        item = map_cohort(row)
        members = store.select(MEMBERS, {"cohort_id": row["id"]}, ["employee_id"])
        item["memberCount"] = len(members)
        item["members"] = [member["employee_id"] for member in members]
        out.append(item)
    return out


def save_cohort(body: dict, actor: str = "") -> dict:
    ensure_slalom_context()
    cohort_id = str(_first(body, "id", "cohortId", "cohort_id") or "")
    existing = _row(COHORTS, cohort_id) if cohort_id else None
    name = " ".join(str(_first(body, "name", "title", default=(existing or {}).get("name") or "") or "").split())
    if not name or len(name) > 200:
        raise ValueError("Cohort name is required and must be 200 characters or fewer.")
    row = {
        "name": name,
        "description": str(_first(body, "description", default=(existing or {}).get("description") or "") or ""),
        "is_active": _bool(_first(body, "isActive", "is_active", default=(existing or {}).get("is_active", True))),
        "updated_at": _now(),
    }
    store = get_store()
    if existing:
        store.update(COHORTS, row, {"id": existing["id"]})
        saved_id = existing["id"]
    else:
        row.update({"id": str(uuid.uuid4()), "created_at": _now(), "created_by": actor or ""})
        saved_id = store.insert(COHORTS, row)[0]["id"]
    return map_cohort(_row(COHORTS, saved_id))


def delete_cohort(cohort_id: str) -> bool:
    ensure_slalom_context()
    get_cohort(cohort_id)
    store = get_store()
    store.delete(ASSIGNMENTS, {"cohort_id": cohort_id})
    store.delete(MEMBERS, {"cohort_id": cohort_id})
    return bool(store.delete(COHORTS, {"id": cohort_id}))


def list_members(cohort_id: str) -> list[dict]:
    get_cohort(cohort_id)
    return [map_member(r) for r in get_store().select(MEMBERS, {"cohort_id": cohort_id}, order="created_at asc")]


def _employee_from_item(item) -> dict:
    if isinstance(item, dict):
        employee_id = str(_first(item, "employeeId", "employee_id", "id", default="") or "").strip()
        email = str(_first(item, "email", "employeeEmail", default="") or "").strip().lower()
    else:
        raw = str(item or "").strip()
        employee_id = "" if "@" in raw else raw
        email = raw.lower() if "@" in raw else ""
    account = accounts.get_account(employee_id) if employee_id else None
    if not account and email:
        account = accounts.get_account_by_email(email)
    if not account:
        raise LookupError(f"Employee portal account not found: {employee_id or email}")
    if account.get("role") != "employee" or not account.get("product_qb_eligible"):
        raise LookupError(f"Not an employee portal account: {account['employee_id']}")
    return {"account": account, "item": item if isinstance(item, dict) else {}}


def _provision_details(item) -> dict:
    """Resolve a roster entry into the detail shape provisioning expects.

    Cohort/assignment forms accept a mix of Employee IDs and email addresses,
    so bare emails must be resolved before ``provision_employee`` validates
    the required name/email fields.
    """
    resolved = _employee_from_item(item)
    account = resolved["account"]
    details = dict(resolved["item"])
    details.update({
        "employee_id": account["employee_id"],
        "full_name": details.get("full_name") or details.get("fullName") or details.get("name") or account.get("full_name") or "",
        "email": details.get("email") or account.get("email") or "",
        "designation": details.get("designation") or account.get("designation") or "",
        "department": details.get("department") or account.get("department") or "",
    })
    return details


def add_members(cohort_id: str, employees: list, actor: str = "") -> list[dict]:
    ensure_slalom_context()
    get_cohort(cohort_id)
    if not isinstance(employees, list) or not employees:
        raise ValueError("Select at least one employee.")
    if len(employees) > 500:
        raise ValueError("Add no more than 500 employees at a time.")
    store = get_store()
    out = []
    for item in employees:
        resolved = _employee_from_item(item)
        account = resolved["account"]
        details = resolved["item"]
        employee_id = account["employee_id"]
        existing = store.first(MEMBERS, {"cohort_id": cohort_id, "employee_id": employee_id})
        values = {
            "employee_uuid": account.get("client_id") and accounts.employee_uuid(employee_id),
            "employee_email": str(details.get("email") or account.get("email") or "").strip().lower(),
            "employee_name": str(details.get("name") or details.get("full_name") or account.get("full_name") or ""),
        }
        if existing:
            store.update(MEMBERS, values, {"id": existing["id"]})
            member_id = existing["id"]
        else:
            values.update({
                "id": str(uuid.uuid4()), "cohort_id": cohort_id, "employee_id": employee_id,
                "joined_at": _now(), "created_at": _now(), "created_by": actor or "",
            })
            member_id = store.insert(MEMBERS, values)[0]["id"]
        out.append(map_member(_row(MEMBERS, member_id)))
    return out


def _learner_details(item) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Each learner needs an Employee ID, name and email.")
    return {
        "employee_id": str(_first(item, "employeeId", "employee_id", "id", default="") or "").strip(),
        "full_name": " ".join(str(_first(item, "fullName", "full_name", "name", default="") or "").split()),
        "email": str(_first(item, "email", default="") or "").strip().lower(),
        "designation": str(_first(item, "designation", "role", default="") or "").strip(),
        "department": str(_first(item, "department", default="") or "").strip(),
    }


def cohort_assessment_ids(cohort_id: str) -> list[str]:
    rows = get_store().select(ASSIGNMENTS, {"cohort_id": cohort_id}, ["assessment_id"])
    return list(dict.fromkeys(row["assessment_id"] for row in rows if row.get("assessment_id")))


def enroll_learners(cohort_id: str, learners: list, actor: str = "", auto_assign: bool = True) -> dict:
    """Create employee portal accounts inside a cohort and enrol them in one step.

    Assessments already assigned to the cohort are assigned to each new learner,
    so a cohort behaves the same for late joiners as for the first intake.
    """
    ensure_slalom_context()
    get_cohort(cohort_id)
    if not isinstance(learners, list) or not learners:
        raise ValueError("Add at least one learner.")
    if len(learners) > 500:
        raise ValueError("Add no more than 500 learners at a time.")
    from services import employee_portal_admin as portal_admin

    assessment_ids = cohort_assessment_ids(cohort_id) if auto_assign else []
    enrolled, failed, seen = [], [], set()
    for row, item in enumerate(learners, start=1):
        label = ""
        try:
            details = _learner_details(item)
            label = details["employee_id"]
            key = label.casefold()
            if key in seen:
                raise ValueError("Duplicate Employee ID in this batch.")
            seen.add(key)
            result = portal_admin.provision_employee(details)
            employee_id = result["employeeId"]
            add_members(cohort_id, [{"employeeId": employee_id, "email": result["email"], "name": result["name"]}], actor)
            assigned = 0
            for assessment_id in assessment_ids:
                assigned += len(assign_assessment(assessment_id, [employee_id], cohort_id, actor))
            enrolled.append({
                "employeeId": employee_id, "name": result["name"], "email": result["email"],
                "created": bool(result.get("created")), "firstLoginPassword": result.get("firstLoginPassword"),
                "assigned": assigned,
            })
        except (ValueError, LookupError, RuntimeError) as err:
            failed.append({"row": row, "employeeId": label, "error": str(err)})
    return {
        "enrolled": enrolled, "failed": failed,
        "created": sum(1 for item in enrolled if item["created"]), "existing": sum(1 for item in enrolled if not item["created"]),
    }


def _progress_row(assignments: list[dict], tests: dict) -> dict:
    mine = [tests[a["testId"]] for a in assignments if a.get("testId") in tests]
    done = [t for t in mine if t.get("status") == "completed"]
    scores = [float(t["score_percent"]) for t in done if t.get("score_percent") is not None]
    return {
        "assigned": len(assignments),
        "started": sum(1 for t in mine if t.get("status") in ("in_progress", "completed")),
        "completed": len(done),
        "averageScore": round(sum(scores) / len(scores), 1) if scores else None,
    }


def cohort_overview(cohort_id: str) -> dict:
    """One read model for the cohort workspace: stage, learners, assessments and progress."""
    ensure_slalom_context()
    cohort = map_cohort(get_cohort(cohort_id))
    store = get_store()
    members = list_members(cohort_id)
    assignments = list_assignments(cohort_id=cohort_id)
    test_ids = [a["testId"] for a in assignments if a.get("testId")]
    tests = {t["id"]: t for t in store.select("tests", {"id": test_ids}, TEST_COLUMNS)} if test_ids else {}
    names = {a["id"]: a for a in list_assessments()}

    learners = []
    for member in members:
        mine = [a for a in assignments if a["employeeId"] == member["employeeId"]]
        learners.append({**member, **_progress_row(mine, tests)})
    assessments = []
    for assessment_id in dict.fromkeys(a["assessmentId"] for a in assignments if a.get("assessmentId")):
        mine = [a for a in assignments if a["assessmentId"] == assessment_id]
        info = names.get(assessment_id) or {}
        assessments.append({
            "assessmentId": assessment_id, "name": info.get("name") or info.get("title") or "Assessment",
            "status": info.get("status") or "closed", **_progress_row(mine, tests),
        })
    summary = _progress_row(assignments, tests)
    summary.update({"learners": len(members), "assessments": len(assessments)})
    steps = [
        {"key": "cohort", "done": True},
        {"key": "learners", "done": bool(members)},
        {"key": "assessments", "done": bool(assessments)},
        {"key": "progress", "done": summary["completed"] > 0},
    ]
    stage = next((step["key"] for step in steps if not step["done"]), "progress")
    return {"cohort": cohort, "stage": stage, "steps": steps, "summary": summary, "learners": learners, "assessments": assessments}


def remove_member(cohort_id: str, employee_id: str) -> bool:
    ensure_slalom_context()
    return bool(get_store().delete(MEMBERS, {"cohort_id": cohort_id, "employee_id": employee_id}))


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------

def list_assignments(assessment_id: str = "", cohort_id: str = "", employee_id: str = "") -> list[dict]:
    where = {}
    if assessment_id:
        where["assessment_id"] = assessment_id
    if cohort_id:
        where["cohort_id"] = cohort_id
    if employee_id:
        where["employee_id"] = employee_id
    return [map_assignment(r) for r in get_store().select(
        ASSIGNMENTS, where or None, order="assigned_at desc"
    )]


def get_assignment(assignment_id: str) -> dict:
    return _row(ASSIGNMENTS, str(assignment_id))


def assign_assessment(assessment_id: str, employees: list, cohort_id: str = "", actor: str = "") -> list[dict]:
    """Provision/refresh each employee's delivery test and record the mapping."""
    ensure_slalom_context()
    assessment = get_assessment(assessment_id, active_only=True)
    from services import employee_portal_admin as portal_admin

    if cohort_id:
        get_cohort(cohort_id)
    if not isinstance(employees, list):
        raise ValueError("Employees must be a list.")
    if not employees and cohort_id:
        employees = [m["employeeId"] for m in list_members(cohort_id)]
    if not employees:
        raise ValueError("Select at least one employee or a cohort with members.")
    if len(employees) > 500:
        raise ValueError("Assign no more than 500 employees at a time.")

    store = get_store()
    out = []
    for item in employees:
        details = _provision_details(item)
        details["assessmentId"] = assessment["id"]
        delivery_assessment = {
            **assessment,
            "topic_id": f"{accounts.PRODUCT_ASSESSMENT_TOPIC_ID}:{assessment['id']}",
        }
        result = portal_admin.provision_employee(details, assessment=delivery_assessment)
        resolved_id = result["employeeId"]
        where = {"assessment_id": assessment["id"], "employee_id": resolved_id}
        existing = store.first(ASSIGNMENTS, where)
        values = {
            "cohort_id": cohort_id or None,
            "test_id": result.get("testId"),
            "status": "assigned",
            "assigned_at": _now(),
            "created_by": actor or "",
        }
        if existing:
            store.update(ASSIGNMENTS, values, {"id": existing["id"]})
            assignment_id = existing["id"]
        else:
            values.update({"id": str(uuid.uuid4()), "assessment_id": assessment["id"],
                           "employee_id": resolved_id, "created_at": _now()})
            assignment_id = store.insert(ASSIGNMENTS, values)[0]["id"]
        if cohort_id:
            add_members(cohort_id, [resolved_id], actor)
        out.append(map_assignment(_row(ASSIGNMENTS, assignment_id)))
    return out


def delete_assignment(assignment_id: str) -> bool:
    ensure_slalom_context()
    get_assignment(assignment_id)
    return bool(get_store().delete(ASSIGNMENTS, {"id": assignment_id}))


# ---------------------------------------------------------------------------
# Tenant-scoped learning content
# ---------------------------------------------------------------------------

def _invalidate_content_caches() -> None:
    from services import portal, topic_sources

    portal.invalidate_content_cache()
    topic_sources.invalidate_content_cache()


def list_content(content_type: str = "") -> list[dict]:
    where = {"is_active": True}
    if content_type:
        where["content_type"] = content_type
    return [map_content(r) for r in get_store().select(CONTENT, where, order="created_at asc")]


def get_content(content_id: str) -> dict:
    return _row(CONTENT, str(content_id))


def save_content(body: dict, actor: str = "") -> dict:
    ensure_slalom_context()
    content_id = str(_first(body, "id", "contentId", "content_id") or "")
    existing = _row(CONTENT, content_id) if content_id else None
    nested = body.get("payload") if isinstance(body.get("payload"), dict) else None
    # The UI submits the friendly label as ``type``; a read-model PATCH also
    # carries the canonical ``contentType``, which must not mask that label.
    raw_value = _first(body, "type", "contentType", "content_type", default=None)
    if raw_value is None:
        raw_value = _first(nested or {}, "type", "contentType", "content_type", default=None)
    if raw_value is None:
        raw_value = (existing or {}).get("content_type") or "source"
    raw_kind = str(raw_value or "source")
    original_kind = raw_kind.strip()
    kind = _content_type(original_kind)
    if not kind:
        raise ValueError(f"Content type must be one of: {', '.join(sorted(CONTENT_TYPES))}.")
    if nested is not None:
        # UI PATCH requests merge the read model (with its nested payload) and
        # the submitted top-level fields; let the submitted fields win so body,
        # source and metadata edits are not silently discarded.
        payload = dict(nested)
        for field, value in body.items():
            if field not in CONTENT_META_KEYS and field != "payload":
                payload[field] = value
    else:
        payload = {field: value for field, value in body.items() if field not in CONTENT_META_KEYS}
    if not payload:
        raise ValueError("Content payload is required.")
    title = str(_first(body, "title", "name",
                       default=payload.get("topic_title") or payload.get("title")
                       or (existing or {}).get("title") or "") or "")
    key = str(_first(body, "contentKey", "content_key", "key", "slug", default="") or "")
    if not key:
        if kind == "source":
            key = str(payload.get("topic_id") or payload.get("topicId") or payload.get("id") or payload.get("key") or "")
        else:
            key = str(payload.get("id") or payload.get("key") or "")
    if not key and title:
        key = _slug(title)
    key = key.strip()
    if not key or len(key) > 200:
        raise ValueError("A content key is required and must be 200 characters or fewer.")
    if not title:
        title = key
    body_text = str(_first(payload, "body", "content", "text", "explanation", default="") or "")
    if "explanation" not in body and any(field in body for field in ("body", "content", "text")):
        # The UI edits the body but has no separate explanation field.
        payload["explanation"] = body_text
    source_value = str(_first(payload, "source", "sourceType", "source_type", "sourceKey", "source_key",
                              default="") or "")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if kind == "source":
        sources = payload.get("sources")
        if not isinstance(sources, list) or not sources:
            source_url = str(_first(metadata, "sourceUrl", "source_url", "url", default="") or "")
            sources = [{
                "name": source_value or metadata.get("sourceName") or "Reference",
                "title": metadata.get("title") or title,
                "url": source_url,
                "description": body_text[:240],
            }] if source_url else []
        payload.update({
            "contentType": original_kind, "type": original_kind, "key": key,
            "topic_id": payload.get("topic_id") or payload.get("topicId") or key,
            "topic_title": payload.get("topic_title") or title,
            "title": payload.get("title") or title,
            "body": body_text, "explanation": payload.get("explanation") or body_text or title,
            "source": source_value, "metadata": metadata, "sources": sources,
        })
    else:
        payload.setdefault("id", key)
        payload.setdefault("key", key)
        payload.setdefault("title", title)
    row = {
        "content_type": kind,
        "content_key": key,
        "title": title[:500],
        "payload": payload,
        "is_active": _bool(_first(body, "isActive", "is_active", default=(existing or {}).get("is_active", True))),
        "updated_at": _now(),
    }
    store = get_store()
    existing_by_key = store.first(CONTENT, {"content_type": kind, "content_key": key})
    target = existing or existing_by_key
    if target:
        store.update(CONTENT, row, {"id": target["id"]})
        saved_id = target["id"]
    else:
        row.update({"id": str(uuid.uuid4()), "created_at": _now(), "created_by": actor or ""})
        saved_id = store.insert(CONTENT, row)[0]["id"]
    _invalidate_content_caches()
    return map_content(_row(CONTENT, saved_id))


def delete_content(content_id: str) -> bool:
    ensure_slalom_context()
    row = get_content(content_id)
    deleted = bool(get_store().delete(CONTENT, {"id": row["id"]}))
    if deleted:
        _invalidate_content_caches()
    return deleted


# ---------------------------------------------------------------------------
# Leaderboard (read-only over tests + test_attempts)
# ---------------------------------------------------------------------------

def _score_percent(test: dict, correct: int, attempts: int) -> float:
    if test.get("score_percent") is not None:
        try:
            return float(test["score_percent"])
        except (TypeError, ValueError):
            pass
    total = _int(test.get("total_questions"), 0)
    if test.get("score_correct") is not None and total > 0:
        return 100.0 * _int(test["score_correct"], 0) / total
    if attempts:
        return 100.0 * correct / attempts
    return 0.0


def leaderboard(assessment_id: str = "", cohort_id: str = "", limit: int = 100) -> dict:
    ensure_slalom_context()
    store = get_store()
    assessment = get_assessment(assessment_id) if assessment_id else None
    cohort = get_cohort(cohort_id) if cohort_id else None

    assignment_where = {}
    if assessment_id:
        assignment_where["assessment_id"] = assessment_id
    if cohort_id:
        assignment_where["cohort_id"] = cohort_id
    assignments = store.select(ASSIGNMENTS, assignment_where or None,
                               ["id", "assessment_id", "cohort_id", "employee_id", "test_id"])
    employee_ids = [row["employee_id"] for row in assignments]
    if cohort_id:
        employee_ids += [row["employee_id"] for row in store.select(
            MEMBERS, {"cohort_id": cohort_id}, ["employee_id"]
        )]
    employee_ids = list(dict.fromkeys(filter(None, employee_ids)))

    employee_rows = store.select("employees", {"employee_id": employee_ids},
                                 ["id", "employee_id", "full_name", "department"]) if employee_ids else []
    uid_to_employee = {row["id"]: row for row in employee_rows}
    employee_uuids = [row["id"] for row in employee_rows]

    if employee_uuids:
        tests = store.select("tests", {"employee_id": employee_uuids}, TEST_COLUMNS)
    elif assessment:
        tests = store.select("tests", {"topic_id": assessment.get("topic_id")}, TEST_COLUMNS)
    else:
        tests = store.select("tests", None, TEST_COLUMNS)

    assignment_test_ids = {row.get("test_id") for row in assignments if row.get("test_id")}
    if assignment_test_ids:
        tests = [test for test in tests if test["id"] in assignment_test_ids]
    elif assessment:
        tests = [test for test in tests if test.get("topic_id") == assessment.get("topic_id")]
    tests = [test for test in tests if test.get("status") == "completed" or test.get("completed_at")]

    test_ids = [test["id"] for test in tests]
    attempts = store.select("test_attempts", {"test_id": test_ids},
                            ["test_id", "is_correct", "time_taken_seconds"]) if test_ids else []
    correct_by_test: dict[str, int] = {}
    attempts_by_test: dict[str, int] = {}
    seconds_by_test: dict[str, float] = {}
    for attempt in attempts:
        test_id = attempt["test_id"]
        attempts_by_test[test_id] = attempts_by_test.get(test_id, 0) + 1
        correct_by_test[test_id] = correct_by_test.get(test_id, 0) + (1 if attempt.get("is_correct") else 0)
        try:
            seconds_by_test[test_id] = seconds_by_test.get(test_id, 0.0) + float(attempt.get("time_taken_seconds") or 0)
        except (TypeError, ValueError):
            pass

    entries = []
    for test in tests:
        employee = uid_to_employee.get(test.get("employee_id")) or {}
        correct = correct_by_test.get(test["id"], 0)
        attempt_count = attempts_by_test.get(test["id"], 0)
        score = _score_percent(test, correct, attempt_count)
        proctoring = test.get("proctoring")
        violations = 0
        auto_submitted = False
        if isinstance(proctoring, dict):
            violations = _int(proctoring.get("warningCount") or proctoring.get("violationCount"), 0)
            if not violations and isinstance(proctoring.get("violations"), list):
                violations = len(proctoring["violations"])
            auto_submitted = bool(proctoring.get("autoSubmitted")) or violations >= employee_tests.MAX_VIOLATIONS
        percent = js_round(score)
        seconds = js_round(seconds_by_test.get(test["id"], 0.0))
        employee_name = employee.get("full_name") or test.get("employee_code") or ""
        entries.append({
            "employeeId": employee.get("employee_id") or test.get("employee_code") or "",
            "employeeName": employee_name,
            "name": employee_name,
            "department": employee.get("department") or "",
            "cohortId": cohort_id or "",
            "cohortName": (cohort or {}).get("name") or "",
            "assessmentId": assessment_id or "",
            "assessmentName": (map_assessment(assessment)["name"] if assessment else ""),
            "testId": test["id"],
            "scoreCorrect": _int(test.get("score_correct"), correct),
            "scoreTotal": _int(test.get("total_questions"), 0),
            "scorePercent": percent,
            "score": percent,
            "completedAt": test.get("completed_at"),
            "timeTakenSeconds": seconds,
            "timeSeconds": seconds,
            "attemptCount": attempt_count,
            "violations": violations,
            "autoSubmitted": auto_submitted,
        })
    entries.sort(key=lambda item: (-item["scorePercent"], item["timeTakenSeconds"], item["completedAt"] or ""))
    entries = entries[:max(1, min(int(limit or 100), 500))]
    for index, entry in enumerate(entries, start=1):
        entry["rank"] = index
    return {
        "assessment": map_assessment(assessment) if assessment else None,
        "cohort": map_cohort(cohort) if cohort else None,
        "entries": entries,
        "leaderboard": entries,
    }


def employee_assignment(employee_id: str) -> Optional[dict]:
    """Latest bootcamp assignment and its delivery-test summary for an employee."""
    ensure_slalom_context()
    rows = list_assignments(employee_id=employee_id)
    if not rows:
        return None
    assignment = rows[0]
    result = dict(assignment)
    if assignment["assessmentId"]:
        try:
            result["assessment"] = map_assessment(get_assessment(assignment["assessmentId"]))
        except LookupError:
            result["assessment"] = None
    if assignment["cohortId"]:
        try:
            result["cohort"] = map_cohort(get_cohort(assignment["cohortId"]))
        except LookupError:
            result["cohort"] = None
    if assignment["testId"]:
        test = get_store().first("tests", {"id": assignment["testId"]}, TEST_COLUMNS)
        if test:
            result["test"] = {
                "id": test["id"], "status": test.get("status"),
                "topicId": test.get("topic_id"), "topicTitle": test.get("topic_title"),
                "totalQuestions": test.get("total_questions"), "timeLimitSeconds": test.get("time_limit_seconds"),
                "scoreCorrect": test.get("score_correct"), "scorePercent": test.get("score_percent"),
                "completedAt": test.get("completed_at"),
            }
    return result
