import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Request

from core.auth import employee_id_from_request, require_employee
from core.db import get_store
from core.errors import json_error, public_error
from services import accounts, employee_tests as et, portal, question_bank, test_video
from services.portal import js_round
from services.products import format_topic_title_for_display

log = logging.getLogger("talentscope")
router = APIRouter(prefix="/api/employee", tags=["employee-portal"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_employee(request: Request):
    code = employee_id_from_request(request)
    return accounts.get_account(code) if code else None


@router.get("/catalog")
def catalog(request: Request):
    employee = _optional_employee(request)
    if not employee:
        return []
    try:
        return portal.catalog_for_employee(employee)
    except Exception as err:
        log.error("GET /employee/catalog error: %s", err)
        return portal.portal_catalog()


@router.get("/subjects")
def subjects(employee: dict = Depends(require_employee)):
    try:
        topics = portal.learning_topics_for_employee(employee)
        if accounts.is_product_qb_employee(employee):
            emp_uuid = accounts.employee_uuid(employee["employee_id"])
            test = get_store().first("tests", {"employee_id": emp_uuid, "topic_id": accounts.PRODUCT_ASSESSMENT_TOPIC_ID}, ["id"]) if emp_uuid else None
            if test:
                for t in topics:
                    if t["id"] == accounts.PRODUCT_ASSESSMENT_TOPIC_ID:
                        t["href"] = f"/employee/tests/{test['id']}"
        return topics
    except Exception as err:
        return public_error(err, "Internal error")


@router.get("/assigned-test")
def assigned_test(employee: dict = Depends(require_employee)):
    try:
        if not accounts.is_product_qb_employee(employee):
            return {"active_test": None, "completed_test": None}
        emp_uuid = accounts.employee_uuid(employee["employee_id"])
        if not emp_uuid:
            return {"active_test": None, "completed_test": None}
        store = get_store()
        completed_rows = store.select(
            "tests", {"employee_id": emp_uuid, "status": "completed"},
            ["id", "topic_id", "topic_title", "total_questions", "score_correct", "score_percent", "completed_at", "status"],
            "completed_at desc", limit=20)
        done = next((r for r in completed_rows if r["topic_id"] == accounts.PRODUCT_ASSESSMENT_TOPIC_ID
                     or accounts.is_product_assessment_history_topic(r["topic_id"])), None)
        active = store.first("tests", [("employee_id", "eq", emp_uuid), ("topic_id", "eq", accounts.PRODUCT_ASSESSMENT_TOPIC_ID),
                                       ("status", "in", ["pending", "in_progress"])],
                             ["id", "topic_title", "total_questions", "status", "started_at", "completed_at", "created_at"], "created_at desc")
        completed_test = None
        if done:
            total = done.get("total_questions") or 25
            correct = done.get("score_correct") or 0
            percent = done.get("score_percent")
            completed_test = {
                "test_id": done["id"], "topic_title": format_topic_title_for_display(done.get("topic_title") or "Product Assessment"),
                "total_questions": total, "score_correct": correct,
                "score_percent": percent if percent is not None else (js_round(correct / total * 100) if total > 0 else 0),
                "completed_at": done.get("completed_at"), "can_retake": False,
                "recording_missing": not test_video.exists(done["id"]),
            }
        if not active:
            return {"active_test": None, "completed_test": completed_test}
        return {
            "active_test": {
                "test_id": active["id"], "topic_title": format_topic_title_for_display(active.get("topic_title") or "Product Assessment"),
                "subject_title": "Product Assessment", "total_questions": active.get("total_questions"), "status": active.get("status"),
                "started_at": active.get("started_at"), "completed_at": active.get("completed_at"),
            },
            "completed_test": completed_test,
        }
    except Exception as err:
        return public_error(err, "Internal error")


def _result_item(test: dict, topic_titles: dict, subject_titles: dict, correct: int) -> dict:
    total = test.get("total_questions") or 25
    percent = test.get("score_percent")
    return {
        "id": test["id"], "topic_id": test["topic_id"],
        "topic_title": format_topic_title_for_display(topic_titles.get(test["topic_id"]) or test.get("topic_title") or test["topic_id"]),
        "subject_id": test["subject_id"], "subject_title": subject_titles.get(test["subject_id"]) or test.get("subject_title") or test["subject_id"],
        "difficulty": test.get("difficulty"), "total_questions": test.get("total_questions"), "correct_answers": correct,
        "accuracy_pct": percent if percent is not None else (js_round(correct / total * 100) if total > 0 else 0),
        "time_taken_seconds": 0, "started_at": test.get("started_at"), "completed_at": test.get("completed_at") or "",
        "topic_breakdown": [],
        "ai_analysis": test.get("ai_analysis") if isinstance(test.get("ai_analysis"), str) else (test.get("in_progress") if isinstance(test.get("in_progress"), str) else ""),
        "improvement_suggestions": [],
    }


@router.get("/results")
def results(request: Request, employee: dict = Depends(require_employee)):
    wanted = (request.query_params.get("employee_id") or "").strip().upper()
    if wanted and wanted != employee["employee_id"].upper():
        return json_error(403, "Forbidden")
    try:
        emp_uuid = accounts.employee_uuid(employee["employee_id"])
        if not emp_uuid:
            return []
        store = get_store()
        tests = store.select("tests", {"employee_id": emp_uuid, "status": "completed"},
                             ["id", "topic_id", "subject_id", "difficulty", "total_questions", "started_at", "completed_at", "in_progress",
                              "score_correct", "score_percent", "ai_analysis", "topic_title", "subject_title"], "completed_at desc")
        if not tests:
            return []
        topic_titles = {r["id"]: r["title"] for r in store.select("learning_topics", {"id": list({t["topic_id"] for t in tests if _is_uuid(t["topic_id"])})}, ["id", "title"])}
        subject_titles = {r["id"]: r["title"] for r in store.select("learning_subjects", {"id": list({t["subject_id"] for t in tests if _is_uuid(t["subject_id"])})}, ["id", "title"])}
        attempts = store.select("test_attempts", {"test_id": [t["id"] for t in tests]}, ["test_id", "is_correct"])
        correct_by_test: dict = {}
        for a in attempts:
            correct_by_test[a["test_id"]] = correct_by_test.get(a["test_id"], 0) + (1 if a["is_correct"] else 0)
        return [_result_item(t, topic_titles, subject_titles,
                             t["score_correct"] if t.get("score_correct") is not None else correct_by_test.get(t["id"], 0)) for t in tests]
    except Exception as err:
        return public_error(err, "Failed to load results")


def _is_uuid(value) -> bool:
    return bool(et.UUID_RE.match(str(value or "")))


@router.get("/analytics")
def analytics(employee: dict = Depends(require_employee)):
    try:
        store = get_store()
        emp = store.first("employees", {"employee_id": employee["employee_id"]}, ["id"])
        if not emp:
            return json_error(500, "Failed to load analytics")
        uid = emp["id"]
        completed = store.select("tests", {"employee_id": uid, "status": "completed"},
                                 ["id", "topic_id", "subject_id", "difficulty", "total_questions", "started_at", "completed_at",
                                  "in_progress", "score_percent", "score_correct", "ai_analysis", "topic_title", "subject_title"], "completed_at desc")
        total_tests = len(completed)
        done_ids = {t["id"] for t in completed}
        attempts = [a for a in store.select("test_attempts", {"employee_id": uid}, ["test_id", "is_correct"]) if a["test_id"] in done_ids]
        correct_total = sum(1 for a in attempts if a["is_correct"])
        # Prefer stored test scores. Auto-submit / partial attempt rows can make
        # correct/attempts look like 100% while score_percent is e.g. 3/25 = 12%.
        stored_scores: list[float] = []
        for t in completed:
            if t.get("score_percent") is not None:
                try:
                    stored_scores.append(float(t["score_percent"]))
                    continue
                except (TypeError, ValueError):
                    pass
            total_q = int(t.get("total_questions") or 0)
            if t.get("score_correct") is not None and total_q > 0:
                try:
                    stored_scores.append(100.0 * float(t["score_correct"]) / total_q)
                except (TypeError, ValueError):
                    pass
        if stored_scores:
            average = js_round(sum(stored_scores) / len(stored_scores))
        else:
            average = js_round(correct_total / len(attempts) * 100) if total_tests > 0 and attempts else 0
        hours = js_round(sum((t.get("total_questions") or 0) * 2 for t in completed) / 60)

        def stats(test_ids: set) -> tuple[int, int]:
            # Prefer scored test rows over sparse attempt logs for the same reason as average.
            scored = [t for t in completed if t["id"] in test_ids]
            if scored and any(t.get("score_percent") is not None or t.get("score_correct") is not None for t in scored):
                correct = 0
                total = 0
                for t in scored:
                    total_q = int(t.get("total_questions") or 0) or 25
                    if t.get("score_correct") is not None:
                        correct += int(t["score_correct"] or 0)
                        total += total_q
                    elif t.get("score_percent") is not None:
                        correct += int(round(float(t["score_percent"]) * total_q / 100.0))
                        total += total_q
                return correct, total
            rel = [a for a in attempts if a["test_id"] in test_ids]
            return sum(1 for a in rel if a["is_correct"]), len(rel)

        breakdown = []
        for subject in store.select("learning_subjects", {"is_active": True}, ["id", "title"], "order_index asc"):
            mine = [t for t in completed if t["subject_id"] == subject["id"]]
            if not mine:
                breakdown.append({"subject_id": subject["id"], "subject_title": subject["title"], "average_pct": 0, "mastery_pct": 0, "topic_count": 0})
                continue
            correct, count = stats({t["id"] for t in mine})
            pct = js_round(correct / count * 100) if count else 0
            breakdown.append({"subject_id": subject["id"], "subject_title": subject["title"], "average_pct": pct,
                              "mastery_pct": len(mine) if pct >= 80 else 0, "topic_count": len(mine)})

        # Product assessments use subject_id like "resource-subject" (not in learning_subjects).
        # Surface them so Strongest/Weakest and readiness subject avg aren't empty.
        if not any((s.get("topic_count") or 0) > 0 for s in breakdown):
            by_title: dict = {}
            for t in completed:
                title = (t.get("topic_title") or t.get("subject_title") or "Product Assessment").strip() or "Product Assessment"
                by_title.setdefault(title, []).append(t)
            for title, mine in by_title.items():
                correct, count = stats({t["id"] for t in mine})
                pct = js_round(correct / count * 100) if count else 0
                if pct == 0 and mine:
                    # Fall back to stored score_percent average for this product group.
                    vals = []
                    for t in mine:
                        if t.get("score_percent") is not None:
                            vals.append(float(t["score_percent"]))
                        elif t.get("score_correct") is not None and (t.get("total_questions") or 0) > 0:
                            vals.append(100.0 * float(t["score_correct"]) / float(t["total_questions"]))
                    if vals:
                        pct = js_round(sum(vals) / len(vals))
                breakdown.append({
                    "subject_id": f"product:{title}",
                    "subject_title": title,
                    "average_pct": pct,
                    "mastery_pct": len(mine) if pct >= 80 else 0,
                    "topic_count": len(mine),
                })

        weeks: dict = {}
        for t in completed:
            if not t.get("completed_at"):
                continue
            slot = weeks.setdefault(portal.week_key(t["completed_at"]), {"tests": 0, "mins": 0, "ids": set()})
            slot["tests"] += 1
            slot["mins"] += (t.get("total_questions") or 0) * 2
            slot["ids"].add(t["id"])
        weekly = []
        for key, slot in weeks.items():
            correct, count = stats(slot["ids"])
            weekly.append({"week_start": key, "tests_taken": slot["tests"], "hours_spent": js_round(slot["mins"] / 60),
                           "avg_score": js_round(correct / count * 100) if count else 0})

        recent = sorted(completed, key=lambda t: t.get("completed_at") or "")[-10:]
        topic_titles = {r["id"]: r["title"] for r in store.select("learning_topics", {"id": list({t["topic_id"] for t in recent if _is_uuid(t["topic_id"])})}, ["id", "title"])}
        subject_titles = {r["id"]: r["title"] for r in store.select("learning_subjects", {"id": list({t["subject_id"] for t in recent if _is_uuid(t["subject_id"])})}, ["id", "title"])}
        recent_results = []
        for t in recent:
            mine = [a for a in attempts if a["test_id"] == t["id"]]
            correct = t["score_correct"] if t.get("score_correct") is not None else sum(1 for a in mine if a["is_correct"])
            item = _result_item(t, topic_titles, subject_titles, correct)
            if t.get("score_percent") is None:
                total_q = t.get("total_questions") or 25
                item["accuracy_pct"] = js_round(correct / len(mine) * 100) if mine else (js_round(correct / total_q * 100) if total_q > 0 else 0)
            recent_results.append(item)

        ranked = sorted(breakdown, key=lambda s: -s["average_pct"])
        pick = lambda s: {"subject_id": s["subject_id"], "subject_title": s["subject_title"], "average_pct": s["average_pct"]}
        readiness = portal.readiness_score(average, total_tests, breakdown, [r["accuracy_pct"] for r in recent_results])
        return {
            "total_tests_taken": total_tests, "average_score": average, "total_learning_hours": hours,
            "ai_readiness_score": readiness, "skill_level": portal.skill_level(readiness),
            # Keep XP aligned with readiness for assessment-only dashboards (was falling back in UI).
            "xp_points": readiness,
            "strongest_subject": pick(ranked[0]) if ranked and ranked[0]["topic_count"] > 0 else None,
            "weakest_subject": pick(ranked[-1]) if ranked and ranked[-1]["topic_count"] > 0 else None,
            "score_history": [{"date": r["completed_at"], "score": r["accuracy_pct"]} for r in recent_results],
            "subject_breakdown": breakdown, "weekly_activity": sorted(weekly, key=lambda w: w["week_start"]), "recent_attempts": recent_results,
        }
    except Exception as err:
        return public_error(err, "Failed to load analytics")


def _resolve_topic(topic_id: str, difficulty: str) -> dict:
    store = get_store()
    if _is_uuid(topic_id):
        topic = store.first("learning_topics", {"id": topic_id}, ["id", "title", "module_id", "difficulty"])
        module = store.first("learning_modules", {"id": topic["module_id"]}, ["subject_id"]) if topic else None
        subject = store.first("learning_subjects", {"id": module["subject_id"]}, ["title"]) if module else None
        if topic and module:
            return {"topic_title": topic.get("title") or topic_id, "subject_title": (subject or {}).get("title") or "",
                    "subject_id": module.get("subject_id") or "", "difficulty": topic.get("difficulty") or difficulty}
    static = portal.find_static_topic(topic_id)
    if not static:
        raise LookupError("Topic not found")
    return {"topic_title": static["title"], "subject_title": static["subject_title"], "subject_id": static["subject_id"],
            "difficulty": static["difficulty"]}


@router.post("/learning/{topic_id}/ai-quiz")
def ai_quiz(topic_id: str, body: dict = Body(default_factory=dict), employee: dict = Depends(require_employee)):
    try:
        if accounts.is_assessment_only_employee(employee):
            return json_error(403, "Other learning tests are disabled. Please take your assigned product assessment only.")
        difficulty = str(body.get("difficulty") or "medium")
        try:
            info = _resolve_topic(topic_id, difficulty)
        except LookupError:
            return json_error(404, "Topic not found")
        emp_uuid = accounts.employee_uuid(employee["employee_id"])
        if not emp_uuid:
            return json_error(500, "Internal error")
        store = get_store()
        existing = store.first("tests", {"employee_id": emp_uuid, "topic_id": topic_id}, ["id", "status", "total_questions", "started_at"])
        started_at, resume = _now(), False
        generated = question_bank.fetch_questions(info["subject_title"], info["topic_title"], info["difficulty"])
        if existing and existing["status"] in ("in_progress", "pending") and existing.get("total_questions") == 10:
            test_id, resume = existing["id"], True
            started_at = existing.get("started_at") or started_at
            store.update("tests", {"difficulty": info["difficulty"], "started_at": started_at}, {"id": test_id})
        else:
            if existing:
                test_id = existing["id"]
                store.update("tests", {"status": "pending", "in_progress": None, "current_question_index": 0, "started_at": started_at,
                                       "completed_at": None, "difficulty": info["difficulty"], "total_questions": 10}, {"id": test_id})
                store.delete("test_attempts", {"test_id": test_id})
                store.delete("test_questions", {"test_id": test_id})
            else:
                test_id = store.insert("tests", {
                    "employee_id": emp_uuid, "employee_code": employee["employee_id"], "topic_id": topic_id,
                    "subject_id": info["subject_id"], "topic_title": info["topic_title"], "subject_title": info["subject_title"],
                    "total_questions": 10, "time_limit_seconds": 1800, "difficulty": info["difficulty"], "status": "pending",
                    "current_question_index": 0, "started_at": started_at})[0]["id"]
            store.insert("test_questions", question_bank.to_rows(test_id, generated, topic_id, info["topic_title"], info["difficulty"]))
        return {"test_id": test_id, "isResume": resume, "topic_title": info["topic_title"], "subject_title": info["subject_title"],
                "difficulty": info["difficulty"], "total_questions": 10, "time_limit_seconds": 1800, "started_at": started_at}
    except Exception as err:
        return public_error(err, "Internal error")
