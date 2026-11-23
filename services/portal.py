import json
import logging
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from core import tenancy
from core.db import get_store
from services import accounts
from services.products import format_product_display_name

DATA = Path(__file__).resolve().parents[1] / "data"
BLOCKED_SUBJECTS = {"question banks", "auto-imported products"}
log = logging.getLogger("talentscope")
CONTENT_TYPES = {"catalog", "subject", "module", "topic"}


def js_round(value: float) -> int:
    return int(math.floor(value + 0.5))


def _content_rows(client: str) -> list[dict]:
    """Tenant-scoped learning content rows that override the static JSON."""
    try:
        with tenancy.client_context(client):
            return get_store().select(
                "bootcamp_content",
                {"content_type": sorted(CONTENT_TYPES), "is_active": True},
                ["content_type", "content_key", "title", "payload"],
                "created_at asc",
            )
    except Exception as err:
        # Static JSON remains the fallback when the content table is not
        # deployed yet (older installations) or the store is unavailable.
        log.warning("Could not load tenant learning content: %s", err)
        return []


def _merge(existing: dict, override: dict) -> dict:
    out = {**existing, **override}
    for key, value in override.items():
        if isinstance(value, list):
            out[key] = list(value)
    return out


def _upsert_by_id(items: list, value: dict) -> None:
    key = str(value.get("id") or "")
    if not key:
        return
    for index, item in enumerate(items):
        if str(item.get("id") or "").casefold() == key.casefold():
            items[index] = _merge(item, value)
            return
    items.append(dict(value))


def _apply_content_overrides(data: dict, client: str) -> dict:
    catalog = data.setdefault("catalog", [])
    subjects = data.setdefault("subjects", [])
    for row in _content_rows(client):
        if not isinstance(row.get("payload"), dict):
            continue
        kind = str(row.get("content_type") or "").strip().casefold()
        payload = dict(row["payload"])
        if kind == "catalog":
            if isinstance(payload.get("catalog"), list):
                data["catalog"] = payload["catalog"]
            else:
                _upsert_by_id(catalog, payload)
        elif kind == "subject":
            _upsert_by_id(subjects, payload)
            # Subject metadata is shared by the topic list and the catalog.
            # Merge into both; modules from the static catalog are retained
            # unless the override explicitly supplies replacements.
            _upsert_by_id(catalog, payload)
        elif kind == "module":
            subject_id = str(payload.get("subject_id") or payload.get("subjectId") or "")
            module = {k: v for k, v in payload.items() if k not in ("subject_id", "subjectId")}
            for collection in (subjects, catalog):
                for subject in collection:
                    if subject_id and str(subject.get("id") or "").casefold() != subject_id.casefold():
                        continue
                    _upsert_by_id(subject.setdefault("modules", []), module)
        elif kind == "topic":
            subject_id = str(payload.get("subject_id") or payload.get("subjectId") or "")
            module_id = str(payload.get("module_id") or payload.get("moduleId") or "")
            topic = {k: v for k, v in payload.items()
                     if k not in ("subject_id", "subjectId", "module_id", "moduleId")}
            for collection in (subjects, catalog):
                for subject in collection:
                    if subject_id and str(subject.get("id") or "").casefold() != subject_id.casefold():
                        continue
                    for module in subject.setdefault("modules", []):
                        if module_id and str(module.get("id") or "").casefold() != module_id.casefold():
                            continue
                        _upsert_by_id(module.setdefault("topics", []), topic)
    return data


@lru_cache(maxsize=None)
def _static_cached(client: str) -> dict:
    return _apply_content_overrides(
        json.loads((DATA / "learning_static.json").read_text(encoding="utf-8")), client
    )


def _static() -> dict:
    return _static_cached(tenancy.current_client())


@lru_cache(maxsize=None)
def _curriculum_cached(client: str) -> list[dict]:
    data = {"catalog": json.loads((DATA / "learning_curriculum.json").read_text(encoding="utf-8"))}
    return _apply_content_overrides(data, client)["catalog"]


def curriculum() -> list[dict]:
    return _curriculum_cached(tenancy.current_client())


def invalidate_content_cache() -> None:
    """Call after content CRUD so every tenant sees fresh overrides."""
    _static_cached.cache_clear()
    _curriculum_cached.cache_clear()


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
