import logging
import re
from typing import Iterable

from services import dashboard_sync, settings

log = logging.getLogger("talentscope")
ROSTER_KEY = "corp_pool_roster"
DELETED_KEY = "deleted_corp_pool"


def normalize_employee_id(value: object) -> str:
    ident = str(value or "").strip()
    ident = re.sub(r"^(\d+)\.0+$", r"\1", ident)
    return ident.casefold()


def _as_list(raw) -> list[dict]:
    rows = raw if isinstance(raw, list) else (raw.get("employees") if isinstance(raw, dict) and isinstance(raw.get("employees"), list) else [])
    seen, out = set(), []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = str(row.get("employee_id") or "").strip()
        normalized = normalize_employee_id(ident)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append({**row, "employee_id": ident})
    return out


def load_roster() -> list[dict]:
    try:
        return _as_list(settings.get_setting(ROSTER_KEY))
    except Exception as err:
        log.warning("Failed to load Corp Pool roster: %s", err)
        return []


def save_roster(employees: Iterable[dict]) -> None:
    settings.set_setting(ROSTER_KEY, {"employees": _as_list(list(employees))})
    dashboard_sync.bump_sync("corp_pool")


def _clean(values) -> list[str]:
    seen = dict.fromkeys(str(v or "").strip().lower() for v in (values if isinstance(values, list) else []))
    return [v for v in seen if v]


def load_deleted() -> dict:
    try:
        raw = settings.get_setting(DELETED_KEY)
    except Exception as err:
        log.warning("Failed to load deleted Corp Pool list: %s", err)
        raw = None
    raw = raw if isinstance(raw, dict) else {}
    return {"ids": _clean(raw.get("ids")), "files": _clean(raw.get("files"))}


def _save_deleted(value: dict) -> None:
    settings.set_setting(DELETED_KEY, {"ids": _clean(value["ids"]), "files": _clean(value["files"])})


def is_deleted(deleted: dict, ident: str = "", file: str = "") -> bool:
    i, f = str(ident or "").strip().lower(), str(file or "").strip().lower()
    return bool(i and i in deleted["ids"]) or bool(f and f in deleted["files"])


def mark_deleted(ids: list[str], files: list[str] | None = None) -> None:
    current = load_deleted()
    _save_deleted({"ids": [*current["ids"], *ids], "files": [*current["files"], *(files or [])]})


def unmark_deleted(ids: list[str], files: list[str] | None = None) -> None:
    current = load_deleted()
    drop_ids, drop_files = set(_clean(ids)), set(_clean(files or []))
    _save_deleted({"ids": [i for i in current["ids"] if i not in drop_ids], "files": [f for f in current["files"] if f not in drop_files]})
