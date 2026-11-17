import json
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from services import accounts
from services.products import format_product_display_name

DATA = Path(__file__).resolve().parents[1] / "data"
BLOCKED_SUBJECTS = {"question banks", "auto-imported products"}


def js_round(value: float) -> int:
    return int(math.floor(value + 0.5))


@lru_cache(maxsize=1)
def _static() -> dict:
    return json.loads((DATA / "learning_static.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def curriculum() -> list[dict]:
    return json.loads((DATA / "learning_curriculum.json").read_text(encoding="utf-8"))


def _blocked(subject: dict) -> bool:
    title = str(subject.get("title") or "").strip().lower()
    description = str(subject.get("description") or "").strip().lower()
    return title in BLOCKED_SUBJECTS or "auto-imported" in description


def learning_topics_for_employee(employee: dict) -> list[dict]:
    topics = [dict(s) for s in _static()["subjects"]]
    product = str(employee.get("product") or "").strip()
    if accounts.is_product_qb_employee(employee) and product and not accounts.is_ai_questionnaire_employee(employee):
        topics.insert(0, {
            "id": accounts.PRODUCT_ASSESSMENT_TOPIC_ID,
            "title": f"{format_product_display_name(product)} Question Bank" if product else "Product Question Bank",
            "description": "Your assigned product assessment from the question bank",
            "icon": "Database", "color": "#4338CA",
        })
    topics = [t for t in topics if not _blocked(t)]
    if accounts.is_assessment_only_employee(employee):
        topics = [t for t in topics if t["id"] == accounts.PRODUCT_ASSESSMENT_TOPIC_ID]
    return topics


def portal_catalog() -> list[dict]:
    return [dict(c) for c in _static()["catalog"]]


def catalog_for_employee(employee: dict) -> list[dict]:
    base = {c["id"]: c for c in portal_catalog()}
    out = []
    for subject in learning_topics_for_employee(employee):
        if subject["id"] == accounts.PRODUCT_ASSESSMENT_TOPIC_ID:
            out.append({
                "id": subject["id"], "title": subject["title"], "description": subject["description"],
                "icon": subject["icon"], "color": subject["color"], "is_active": True,
                "modules": [{
                    "id": f"{subject['id']}-core", "subject_id": subject["id"], "title": "Assigned Assessment",
                    "description": subject["description"], "order_index": 1,
                    "topics": [{"id": f"{subject['id']}-test", "module_id": f"{subject['id']}-core", "title": subject["title"],
                                "difficulty": "medium", "order_index": 1, "estimated_minutes": 25}],
                }],
            })
        else:
            out.append(base.get(subject["id"]) or {**{k: subject[k] for k in ("id", "title", "description", "icon", "color")},
                                                    "is_active": True, "modules": []})
    return out


def find_static_topic(id_or_title: str) -> Optional[dict]:
    needle = id_or_title.lower()
    for subject in curriculum():
        for module in subject["modules"]:
            for topic in module["topics"]:
                if topic["id"].lower() == needle or needle in topic["title"].lower():
                    return {"subject_id": subject["id"], "subject_title": subject["title"], "title": topic["title"],
                            "difficulty": topic.get("difficulty") or "medium"}
    for subject in portal_catalog():
        for module in subject["modules"]:
            for topic in module["topics"]:
                if topic["id"].lower() == needle:
                    return {"subject_id": subject["id"], "subject_title": subject["title"], "title": topic["title"],
                            "difficulty": topic.get("difficulty") or "medium"}
    return None


def skill_level(readiness: float) -> str:
    if readiness >= 80:
        return "advanced"
    if readiness >= 60:
        return "intermediate"
    return "beginner"


def readiness_score(average: float, total_tests: int, breakdown: list[dict], test_scores: list[float]) -> int:
    if total_tests <= 0 and not test_scores:
        return 0
    active = [s for s in breakdown if (s.get("topic_count") or 0) > 0]
    subject_avg = sum(s.get("average_pct") or 0 for s in active) / len(active) if active else 0
    per_test = sum(test_scores) / len(test_scores) if test_scores else 0
    base = max(average, subject_avg, per_test)
    if base <= 0:
        return 0
    return min(100, js_round(base + min(max(len(active) - 1, 0) * 2, 10)))


def week_key(moment: str) -> str:
    date = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    date = date.astimezone(timezone.utc)
    monday = date - timedelta(days=date.weekday())
    return monday.date().isoformat()
