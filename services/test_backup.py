"""Latest and submit snapshots of an employee test. Live portal rows are not changed."""

import json
import logging
from datetime import datetime, timezone

from core.db import get_store
from services import storage

log = logging.getLogger("talentscope")
SUBMIT_EVENT = "submit"


def snapshot(test_id: str, event: str) -> None:
    try:
        store = get_store()
        test = store.first("tests", {"id": test_id})
        if not test:
            return
        payload = {
            "event": event,
            "savedAt": datetime.now(timezone.utc).isoformat(),
            "test": test,
            "questions": store.select("test_questions", {"test_id": test_id}),
            "attempts": store.select("test_attempts", {"test_id": test_id}),
        }
        raw = json.dumps(payload, default=str).encode("utf-8")
        storage.save("backups", f"{test_id}-latest.json", raw)
        if event == SUBMIT_EVENT:
            storage.save("backups", f"{test_id}-submit.json", raw)
    except Exception as err:
        log.warning("employee test backup failed for %s (%s): %s", test_id, event, err)


def read_latest(test_id: str):
    try:
        raw = storage.read("backups", f"{test_id}-latest.json")
    except Exception as err:
        log.warning("employee test backup read failed for %s: %s", test_id, err)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
