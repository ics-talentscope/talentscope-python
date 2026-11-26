import logging
from typing import Optional

from core import tenancy
from services import storage

log = logging.getLogger("talentscope")

BUCKET = "employee_test_recordings"
LEGACY_BUCKET = "recordings"
LEGACY_PREFIX = "employee-tests/"
MIN_WEBM_BYTES = 4096
CLUSTER_MAGIC = bytes([0x1F, 0x43, 0xB6, 0x75])
EBML_MAGIC = bytes([0x1A, 0x45, 0xDF, 0xA3])


def has_ebml_header(buf: bytes) -> bool:
    return len(buf) >= 4 and buf[:4] == EBML_MAGIC


def has_cluster_data(buf: bytes) -> bool:
    return len(buf) >= 512 and CLUSTER_MAGIC in buf[: 4 * 1024 * 1024]


def is_playable(buf: bytes, min_bytes: int = MIN_WEBM_BYTES) -> bool:
    return len(buf) >= min_bytes and has_ebml_header(buf) and has_cluster_data(buf)


def repair(buf: bytes) -> bytes:
    if not buf or buf[:4] == EBML_MAGIC:
        return buf
    idx = buf.find(EBML_MAGIC, 1, min(len(buf), 512 * 1024 + 4))
    return buf[idx:] if idx > 0 else buf


def admin_url(test_id: str) -> str:
    return f"/api/admin/employee-tests/{test_id}/video"


def _name(test_id: str) -> str:
    return f"{test_id}.webm"


def _legacy_path(test_id: str) -> str:
    return f"{LEGACY_PREFIX}{test_id}.webm"


def _as_bytes(value) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytearray):
        return bytes(value)
    return value if isinstance(value, bytes) else b""


def _sql_get(bucket: str, path: str) -> Optional[bytes]:
    try:
        from core.db import get_store

        rows = get_store().select("storage_objects", {"bucket": bucket, "path": path}, ["content"], limit=1)
    except Exception as err:
        log.warning("Employee test video storage read failed for %s/%s: %s", bucket, path, err)
        return None
    data = _as_bytes(rows[0].get("content")) if rows else b""
    return data or None


def _sql_exists(bucket: str, path: str) -> bool:
    try:
        from core.db import get_store

        return bool(get_store().count("storage_objects", [
            ("bucket", "eq", bucket), ("path", "eq", path), ("size_bytes", "gt", 0),
        ]))
    except Exception as err:
        log.warning("Employee test video storage exists check failed for %s/%s: %s", bucket, path, err)
        return False


def _sql_delete(bucket: str, path: str) -> None:
    try:
        from core.db import get_store

        get_store().delete("storage_objects", {"bucket": bucket, "path": path})
    except Exception as err:
        log.warning("Employee test video storage delete failed for %s/%s: %s", bucket, path, err)


def _sql_list_ids() -> set[str]:
    ids: set[str] = set()
    try:
        from core.db import get_store

        store = get_store()
        rows = store.select("storage_objects", [
            ("bucket", "eq", BUCKET), ("path", "like", "%.webm"),
        ], ["bucket", "path"])
        rows += store.select("storage_objects", [
            ("bucket", "eq", LEGACY_BUCKET), ("path", "like", f"{LEGACY_PREFIX}%.webm"),
        ], ["bucket", "path"])
    except Exception as err:
        log.warning("Employee test video storage list failed: %s", err)
        return ids
    for row in rows:
        path = str(row.get("path") or "")
        bucket = str(row.get("bucket") or "")
        if bucket == BUCKET and path.endswith(".webm") and "/" not in path and "\\" not in path:
            ids.add(path[:-5])
        elif bucket == LEGACY_BUCKET and path.startswith(LEGACY_PREFIX) and path.endswith(".webm"):
            ids.add(path[len(LEGACY_PREFIX):-5])
    return ids


def _disk_list_ids() -> set[str]:
    try:
        ids: set[str] = set()
        for name in storage.disk_names(BUCKET):
            if name.endswith(".webm") and storage.disk_path(BUCKET, name).stat().st_size > 0:
                ids.add(name[:-5])
        return ids
    except Exception:
        return set()


def save_progress(test_id: str, buf: bytes) -> bool:
    cleaned = repair(buf)
    if not has_ebml_header(cleaned) or len(cleaned) < 1024:
        return False
    try:
        storage.save(BUCKET, _name(test_id), cleaned)
        return True
    except OSError as err:
        log.error("Employee test video progress save failed for %s: %s", test_id, err)
        return False


def save(test_id: str, buf: bytes) -> bool:
    cleaned = repair(buf)
    if not is_playable(cleaned):
        return False
    try:
        storage.save(BUCKET, _name(test_id), cleaned)
        return True
    except OSError as err:
        log.error("Employee test video save failed for %s: %s", test_id, err)
        return False


def save_lenient(test_id: str, buf: bytes) -> bool:
    return save(test_id, buf) or save_progress(test_id, buf)


def read(test_id: str) -> Optional[bytes]:
    data = storage.read(BUCKET, _name(test_id))
    if not data:
        data = _sql_get(BUCKET, _name(test_id))
    if not data:
        data = _sql_get(LEGACY_BUCKET, _legacy_path(test_id))
    return repair(data) if data else None


def exists(test_id: str) -> bool:
    if storage.resolve(BUCKET, _name(test_id)) is not None:
        return True
    if _sql_exists(BUCKET, _name(test_id)):
        return True
    return _sql_exists(LEGACY_BUCKET, _legacy_path(test_id))


def delete(test_id: str) -> None:
    storage.remove(BUCKET, _name(test_id))
    _sql_delete(BUCKET, _name(test_id))
    _sql_delete(LEGACY_BUCKET, _legacy_path(test_id))


def list_ids() -> set[str]:
    return _disk_list_ids() | _sql_list_ids()
