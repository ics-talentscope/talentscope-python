import logging
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.db import get_store
from services import accounts, question_bank, test_video
from services.structured_log import write_log

log = logging.getLogger("talentscope")
SWEEP_INTERVAL_S = 120

MAX_VIOLATIONS = 3
VIOLATION_COOLDOWN_MS = 2500
# Face Missing flicker was burning 3 strikes in ~15s and auto-submitting fair attempts.
FACE_MISSING_COOLDOWN_MS = 45_000
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0


def classify(violation_type: str) -> dict:
    t = violation_type.lower()
    if "multiple face" in t:
        return {"category": "face", "severity": "high"}
    if "face missing" in t or "face not" in t:
        return {"category": "face", "severity": "high"}
    if "looking down" in t or "phone" in t:
        return {"category": "attention", "severity": "high"}
    if "looking away" in t or "distracted" in t:
        return {"category": "attention", "severity": "medium"}
    if "camera" in t:
        return {"category": "camera", "severity": "high"}
    if "recording" in t:
        return {"category": "camera", "severity": "medium"}
    if any(w in t for w in ("tab", "focus", "fullscreen", "devtools", "copy", "paste", "refresh", "print", "right click", "navigation")):
        return {"category": "browser", "severity": "medium"}
    return {"category": "other", "severity": "low"}


def empty_proctoring() -> dict:
    return {"warningCount": 0, "violations": [], "autoSubmitted": False, "sessionStartedAt": None,
            "videoUploaded": False, "lastViolationAt": None}


def normalize_proctoring(raw) -> dict:
    if not isinstance(raw, dict):
        return empty_proctoring()
    violations = []
    for v in raw.get("violations") or []:
        v = v if isinstance(v, dict) else {}
        vtype = str(v.get("type") or "Unknown")[:120]
        cls = classify(vtype)
        violations.append({
            "type": vtype, "timestamp": str(v.get("timestamp") or _now()),
            "category": v.get("category") or cls["category"], "severity": v.get("severity") or cls["severity"],
            "detail": str(v["detail"])[:240] if v.get("detail") else None,
        })
    try:
        warnings = int(raw.get("warningCount") or 0)
    except (TypeError, ValueError):
        warnings = 0
    return {"warningCount": warnings, "violations": violations, "autoSubmitted": bool(raw.get("autoSubmitted")),
            "sessionStartedAt": raw.get("sessionStartedAt"), "videoUploaded": bool(raw.get("videoUploaded")),
            "lastViolationAt": raw.get("lastViolationAt")}


def record_violation(existing: dict, violation_type: str, detail=None, category=None, severity=None) -> dict:
    t = (violation_type or "").lower()
    is_face_missing = "face missing" in t or "face not" in t
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    # Keep Face Missing cooldown separate so it cannot suppress hard strikes.
    if is_face_missing:
        last_face = _ms(existing.get("lastFaceMissingAt")) if existing.get("lastFaceMissingAt") else 0
        if last_face and now_ms - last_face < FACE_MISSING_COOLDOWN_MS:
            return existing
    else:
        last = _ms(existing["lastViolationAt"]) if existing.get("lastViolationAt") else 0
        if last and now_ms - last < VIOLATION_COOLDOWN_MS:
            return existing
    cls = classify(violation_type)
    now = _now()
    entry = {
        "type": violation_type[:120],
        "timestamp": now,
        "category": category or cls["category"],
        "severity": severity or cls["severity"],
        "detail": detail[:240] if detail else None,
    }
    # Face Missing is log-only: browser face models often false-negative while the
    # recorded video clearly shows the candidate. Do NOT burn strike / auto-submit.
    if is_face_missing:
        return {
            **existing,
            "lastFaceMissingAt": now,
            "faceMissingEvents": int(existing.get("faceMissingEvents") or 0) + 1,
            "violations": [*existing["violations"], entry],
        }
    count = min(MAX_VIOLATIONS, (existing.get("warningCount") or 0) + 1)
    return {
        **existing, "warningCount": count,
        "autoSubmitted": bool(existing.get("autoSubmitted")) or count >= MAX_VIOLATIONS,
        "lastViolationAt": now,
        "violations": [*existing["violations"], entry],
    }


def mark_session_started(existing: dict) -> dict:
    return existing if existing.get("sessionStartedAt") else {**existing, "sessionStartedAt": _now()}


def mark_video_uploaded(existing: dict) -> dict:
    return {**existing, "videoUploaded": True}


def is_time_expired(test: dict) -> bool:
    if not test.get("started_at"):
        return False
    elapsed = datetime.now(timezone.utc).timestamp() - _ms(test["started_at"]) / 1000
    return elapsed > (test.get("time_limit_seconds") or 1800) + 30


def can_patch_progress(test: dict, status: Optional[str]) -> Optional[str]:
    current = test.get("status") or "pending"
    if current == "completed":
        return "Test already completed."
    if current == "abandoned":
        return "Test is no longer available."
    if status == "pending" and current == "in_progress":
        return "Cannot revert an in-progress test to pending."
    return None


def can_submit(test: dict) -> tuple[bool, str, str]:
    status = test.get("status") or "pending"
    if status == "completed":
        return False, "Test already submitted.", "ALREADY_COMPLETED"
    if status == "pending":
        return False, "Test has not been started.", "NOT_STARTED"
    if is_time_expired(test):
        return False, "Time limit exceeded.", "TIME_EXPIRED"
    return True, "", ""


def get_owned_test(test_id: str, employee: dict) -> Optional[dict]:
    if not UUID_RE.match(test_id or ""):
        return None
    test = get_store().first("tests", {"id": test_id})
    if not test:
        return None
    emp_uuid = accounts.employee_uuid(employee["employee_id"]) or ""
    owner = str(test.get("employee_id") or "").strip().lower()
    code = str(test.get("employee_code") or "").strip().lower()
    mine = employee["employee_id"].strip().lower()
    return test if owner in (mine, emp_uuid.lower()) or code == mine else None


def questions_for(test_id: str) -> list[dict]:
    return get_store().select("test_questions", {"test_id": test_id}, order="question_index asc")


def archive_as_history(test_id: str) -> Optional[str]:
    store = get_store()
    test = store.first("tests", {"id": test_id})
    if not test or accounts.is_product_assessment_history_topic(test.get("topic_id")):
        return None
    attempts = store.select("test_attempts", {"test_id": test_id})
    if not (test.get("status") == "completed" or test.get("score_percent") is not None or test.get("completed_at") or attempts):
        return None
    history_id = str(uuid.uuid4())
    title = re.sub(r"\s*\(previous(?: attempt)?\)\s*$", "", str(test.get("topic_title") or "Product Assessment"), flags=re.I)
    row = {k: v for k, v in test.items() if k not in ("id", "created_at")}
    row.update({
        "id": history_id, "topic_id": f"{accounts.PRODUCT_ASSESSMENT_HISTORY_TOPIC_ID}:{history_id}",
        "topic_title": f"{title} (previous)", "status": "completed", "completed_at": test.get("completed_at") or _now(),
    })
    store.insert("tests", row)
    id_map = {}
    copies = []
    for q in questions_for(test_id):
        new_id = str(uuid.uuid4())
        id_map[str(q["id"])] = new_id
        copies.append({**{k: v for k, v in q.items() if k != "created_at"}, "id": new_id, "test_id": history_id})
    if copies:
        store.insert("test_questions", copies)
    if attempts:
        store.insert("test_attempts", [
            {**{k: v for k, v in a.items() if k != "created_at"}, "id": str(uuid.uuid4()), "test_id": history_id,
             "question_id": id_map.get(str(a["question_id"]), a["question_id"])} for a in attempts])
    return history_id


def reset_test(test_id: str) -> None:
    archive_as_history(test_id)
    store = get_store()
    store.delete("test_attempts", {"test_id": test_id})
    store.update("tests", {
        "status": "pending", "in_progress": None, "current_question_index": 0, "started_at": None, "completed_at": None,
        "session_recording_url": None, "proctoring": None, "score_correct": None, "score_total": None,
        "score_percent": None, "ai_analysis": None}, {"id": test_id})
    test_video.delete(test_id)
    shuffle_questions(test_id)


def shuffle_questions(test_id: str) -> None:
    store = get_store()
    questions = questions_for(test_id)
    if not questions:
        return

    rng = random.SystemRandom()
    order = list(questions)
    rng.shuffle(order)
    offset = max(int(q["question_index"]) for q in questions) + len(questions) + 1
    for index, question in enumerate(order):
        store.update("test_questions", {"question_index": offset + index}, {"id": question["id"]})

    for index, question in enumerate(order):
        choices = list(enumerate(question.get("options") or []))
        rng.shuffle(choices)
        correct_index = question.get("correct_option_index")
        correct_option_index = next(
            (i for i, (original_index, _) in enumerate(choices) if original_index == correct_index),
            None,
        )
        if correct_option_index is None:
            raise ValueError(f"Question {question['id']} has an invalid correct option index.")
        store.update(
            "test_questions",
            {
                "question_index": index,
                "options": [option for _, option in choices],
                "correct_option_index": correct_option_index,
            },
            {"id": question["id"]},
        )


def regenerate_questions(test: dict) -> None:
    store = get_store()
    test_id = test["id"]
    difficulty = test.get("difficulty") or "medium"
    store.update("tests", {"status": "pending", "in_progress": None, "current_question_index": 0,
                           "started_at": _now(), "completed_at": None, "total_questions": 10}, {"id": test_id})
    store.delete("test_attempts", {"test_id": test_id})
    store.delete("test_questions", {"test_id": test_id})
    generated = question_bank.fetch_questions(test.get("subject_title") or "", test.get("topic_title") or test["topic_id"], difficulty)
    store.insert("test_questions", question_bank.to_rows(test_id, generated, test["topic_id"], test.get("topic_title") or "", difficulty))
    shuffle_questions(test_id)


def _option_label(index: int) -> str:
    return chr(65 + index)


def question_attempts_batch(test_ids: list[str]) -> dict[str, list[dict]]:
    ids = list(dict.fromkeys(i for i in test_ids if i))
    if not ids:
        return {}
    store = get_store()
    questions = store.select("test_questions", {"test_id": ids})
    attempts = store.select("test_attempts", {"test_id": ids})
    by_question = {str(a["question_id"]): a for a in attempts}
    out: dict[str, list[dict]] = {i: [] for i in ids}
    for q in sorted(questions, key=lambda r: (r["test_id"], r["question_index"])):
        attempt = by_question.get(str(q["id"]))
        if not attempt:
            continue
        selected = attempt.get("selected_option_index") if attempt else None
        options = q.get("options") or []
        text = f"{_option_label(selected)}) {options[selected]}" if selected is not None and 0 <= selected < len(options) else None
        out[q["test_id"]].append({
            "question_index": q["question_index"], "question_text": q["question_text"], "options": options,
            "selected_option_index": selected, "selected_option_text": text,
            "is_correct": attempt.get("is_correct") if attempt else None,
            "submitted_at": attempt.get("created_at") if attempt else None,
        })
    return out


def finalize(test: dict, emp_uuid: Optional[str], answers: list[dict], auto: bool) -> dict:
    store = get_store()
    questions = questions_for(test["id"])
    correct_map = {str(q["id"]): q["correct_option_index"] for q in questions}
    attempts = [
        {"test_id": test["id"], "employee_id": emp_uuid, "question_id": a["question_id"],
         "selected_option_index": a.get("selected_index"),
         "is_correct": correct_map[str(a["question_id"])] == a.get("selected_index"),
         "time_taken_seconds": int(a.get("time_seconds") or 0), "session_key": test["id"][:8]}
        for a in answers if isinstance(a, dict) and str(a.get("question_id")) in correct_map and isinstance(a.get("selected_index"), int)
    ]
    if attempts:
        store.delete("test_attempts", {"test_id": test["id"]})
        store.insert("test_attempts", attempts)
    correct = sum(1 for a in attempts if a["is_correct"])
    total = test.get("total_questions") or len(questions) or len(attempts)
    accuracy = round(correct / max(1, total) * 100)
    proctoring = normalize_proctoring(test.get("proctoring"))
    proctoring["autoSubmitted"] = proctoring["autoSubmitted"] or auto
    store.update("tests", {"status": "completed", "completed_at": _now(), "score_correct": correct, "score_total": total,
                           "score_percent": accuracy, "ai_analysis": None, "proctoring": proctoring}, {"id": test["id"]})
    return {"total": total, "correct": correct, "accuracy": accuracy}


def saved_answers(test: dict) -> list[dict]:
    saved = test.get("in_progress")
    if not isinstance(saved, dict):
        return []
    by_index = {int(q["question_index"]): q for q in questions_for(test["id"])}
    out = []
    for key, selected in saved.items():
        question = by_index.get(int(key)) if str(key).lstrip("-").isdigit() else None
        if question and isinstance(selected, int):
            out.append({"question_id": question["id"], "selected_index": selected, "time_seconds": 0})
    return out


def finalize_if_expired(test: dict) -> Optional[dict]:
    if (test.get("status") or "pending") != "in_progress" or not is_time_expired(test):
        return None
    owner = str(test.get("employee_id") or "")
    emp_uuid = owner if UUID_RE.match(owner) else accounts.employee_uuid(str(test.get("employee_code") or owner))
    result = finalize(test, emp_uuid, saved_answers(test), True)
    write_log("employee", "EMPLOYEE_TEST_AUTO_SUBMITTED", "success",
              f"Test {test['id']} reached its time limit and was submitted with the saved answers: {result['correct']}/{result['total']}", actor="system")
    return result


def sweep_expired() -> int:
    done = 0
    try:
        for test in get_store().select("tests", {"status": "in_progress"}):
            try:
                if finalize_if_expired(test):
                    done += 1
            except Exception as err:
                log.error("auto-submit of expired test %s failed: %s", test.get("id"), err)
    except Exception as err:
        log.error("expired test sweep failed: %s", err)
    return done


def _sweeper_loop() -> None:
    time.sleep(30)
    while True:
        sweep_expired()
        time.sleep(SWEEP_INTERVAL_S)


def start_expiry_sweeper() -> None:
    threading.Thread(target=_sweeper_loop, daemon=True, name="test-expiry").start()
