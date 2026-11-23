import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core import tenancy
from core.config import UPLOAD_DIR, env

log = logging.getLogger("talentscope")
SAFE_RE = re.compile(r'[\\/:*?"<>|]+')
_sql_ready: Optional[bool] = None

MIME = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "zip": "application/zip",
    "txt": "text/plain",
    "html": "text/html",
    "htm": "text/html",
    "webm": "video/webm",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "json": "application/json",
}


def safe_name(name: str) -> str:
    return SAFE_RE.sub("_", name or "file")


def uses_sql() -> bool:
    return env("STORAGE_PROVIDER").lower() == "mssql"


def _sql_ok() -> bool:
    global _sql_ready
    if not uses_sql():
        return False
    if _sql_ready is not None:
        return _sql_ready
    try:
        from core.db import get_store

        rows = get_store().query("SELECT OBJECT_ID(N'dbo.storage_objects', N'U') AS id")
        _sql_ready = bool(rows and rows[0].get("id"))
    except Exception as err:
        log.warning("SQL file storage is unavailable: %s", err)
        _sql_ready = False
    if not _sql_ready:
        log.warning("STORAGE_PROVIDER=mssql but dbo.storage_objects is missing; keeping files on disk")
    return _sql_ready


def _content_type(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return MIME.get(ext, "application/octet-stream")


def _as_bytes(value) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytearray):
        return bytes(value)
    return value if isinstance(value, bytes) else b""


def _sql_put(bucket: str, path: str, data: bytes) -> None:
    if not data or not _sql_ok():
        return
    try:
        from core.db import get_store

        content_type = _content_type(path)
        size = len(data)
        client = tenancy.current_client()
        get_store().execute(
            """
            MERGE dbo.storage_objects WITH (HOLDLOCK) AS target
            USING (SELECT ? AS bucket, ? AS path, ? AS client_id) AS source
            ON target.bucket = source.bucket AND target.path = source.path
               AND target.client_id = source.client_id
            WHEN MATCHED THEN
              UPDATE SET content = ?, content_type = ?, size_bytes = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
              INSERT (bucket, path, client_id, content, content_type, size_bytes)
              VALUES (?, ?, ?, ?, ?, ?);
            """,
            (bucket, path, client, data, content_type, size, bucket, path, client, data, content_type, size),
        )
    except Exception as err:
        log.warning("SQL file write failed for %s/%s: %s", bucket, path, err)


def _sql_delete(bucket: str, path: Optional[str] = None, prefix: Optional[str] = None, older_than_days: Optional[int] = None) -> int:
    if not _sql_ok():
        return 0
    try:
        from core.db import get_store

        client = tenancy.current_client()
        if path is not None:
            return int(get_store().execute(
                "DELETE FROM dbo.storage_objects WHERE client_id = ? AND bucket = ? AND path = ?",
                (client, bucket, path),
            ) or 0)
        if prefix is not None:
            return int(get_store().execute(
                "DELETE FROM dbo.storage_objects WHERE client_id = ? AND bucket = ? AND path LIKE ?",
                (client, bucket, prefix.replace("%", "[%]").replace("_", "[_]") + "%"),
            ) or 0)
        if older_than_days is not None:
            return int(get_store().execute(
                "DELETE FROM dbo.storage_objects WHERE client_id = ? AND bucket = ? AND updated_at < DATEADD(day, ?, SYSUTCDATETIME())",
                (client, bucket, -int(older_than_days)),
            ) or 0)
        return int(get_store().execute(
            "DELETE FROM dbo.storage_objects WHERE client_id = ? AND bucket = ?", (client, bucket)
        ) or 0)
    except Exception as err:
        log.warning("SQL file delete failed for %s: %s", bucket, err)
        return 0


def _hydrate(bucket: str, name: str, target: Path) -> None:
    if not _sql_ok() or (target.is_file() and target.stat().st_size > 0):
        return
    try:
        from core.db import get_store

        clause, params = tenancy.sql_filter()
        rows = get_store().query(
            f"SELECT content FROM dbo.storage_objects WHERE bucket = ? AND path = ?{clause}",
            (bucket, name, *params),
        )
    except Exception as err:
        log.warning("SQL file read failed for %s/%s: %s", bucket, name, err)
        return
    data = _as_bytes(rows[0].get("content")) if rows else b""
    if not data:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _sync_names(bucket: str, folder: Path) -> None:
    if not _sql_ok():
        return
    try:
        from core.db import get_store

        clause, params = tenancy.sql_filter()
        rows = get_store().query(
            f"SELECT path FROM dbo.storage_objects WHERE bucket = ?{clause}", (bucket, *params)
        )
    except Exception as err:
        log.warning("SQL file list failed for %s: %s", bucket, err)
        return
    for row in rows:
        name = safe_name(str(row.get("path") or ""))
        if not name:
            continue
        target = folder / name
        if folder.resolve() not in target.resolve().parents:
            continue
        if not target.exists():
            target.touch()


def bucket_dir(bucket: str, create: bool = True) -> Path:
    path = tenancy.client_root(UPLOAD_DIR).joinpath(*bucket.split("/"))
    if create:
        path.mkdir(parents=True, exist_ok=True)
        _sync_names(bucket, path)
    return path


def save(bucket: str, name: str, data: bytes) -> str:
    target = bucket_dir(bucket) / safe_name(name)
    target.write_bytes(data)
    _sql_put(bucket, target.name, data)
    return target.name


def save_stream(bucket: str, name: str, source, max_bytes: int, signature: bytes = b"", chunk: int = 1024 * 1024) -> int:
    target = bucket_dir(bucket) / safe_name(name)
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".part")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                block = source.read(chunk)
                if not block:
                    break
                if size == 0 and signature and not block.startswith(signature):
                    raise ValueError("File verification failed: content does not match the file type")
                size += len(block)
                if size > max_bytes:
                    raise ValueError(f"File too large (max {round(max_bytes / (1024 * 1024))}MB)")
                out.write(block)
        if size == 0:
            raise ValueError("File is empty")
        os.replace(tmp, target)
        _sql_put(bucket, target.name, target.read_bytes())
        return size
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def resolve(bucket: str, name: str) -> Optional[Path]:
    folder = bucket_dir(bucket, create=False)
    stored = safe_name(name)
    target = (folder / stored).resolve()
    if folder.resolve() not in target.parents:
        return None
    _hydrate(bucket, stored, target)
    if not target.is_file() or target.stat().st_size == 0:
        return None
    return target


def read(bucket: str, name: str) -> Optional[bytes]:
    target = resolve(bucket, name)
    return target.read_bytes() if target else None


def remove_prefix(bucket: str, prefix: str) -> int:
    count = 0
    for item in bucket_dir(bucket).iterdir():
        if item.name.startswith(prefix):
            item.unlink(missing_ok=True)
            count += 1
    sql_count = _sql_delete(bucket, prefix=prefix)
    return sql_count or count


def remove(bucket: str, name: str) -> None:
    target = bucket_dir(bucket) / safe_name(name)
    if target.is_file():
        target.unlink(missing_ok=True)
    _sql_delete(bucket, path=safe_name(name))


def clear_bucket(bucket: str) -> None:
    for item in bucket_dir(bucket).iterdir():
        if item.is_file():
            item.unlink(missing_ok=True)
    _sql_delete(bucket)


def prune_older_than(bucket: str, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    folder = bucket_dir(bucket)
    removed = 0
    for path in folder.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    if not _sql_ok():
        return removed
    try:
        from core.db import get_store

        clause, params = tenancy.sql_filter()
        rows = get_store().query(
            f"SELECT path FROM dbo.storage_objects WHERE bucket = ? AND updated_at < DATEADD(day, ?, SYSUTCDATETIME()){clause}",
            (bucket, -int(days), *params),
        )
    except Exception as err:
        log.warning("SQL file prune list failed for %s: %s", bucket, err)
        rows = []
    for row in rows:
        (folder / safe_name(str(row.get("path") or ""))).unlink(missing_ok=True)
    return _sql_delete(bucket, older_than_days=days) or removed


def usage_summary() -> list[dict]:
    if _sql_ok():
        try:
            from core.db import get_store

            clause, params = tenancy.sql_filter(prefix="WHERE")
            rows = get_store().query(
                f"""
                SELECT bucket, COUNT(*) AS files, SUM(CAST(size_bytes AS bigint)) AS bytes,
                       MAX(size_bytes) AS largestBytes, MIN(created_at) AS oldest, MAX(updated_at) AS newest
                FROM dbo.storage_objects{clause} GROUP BY bucket
                """,
                params,
            )
            out = []
            for row in rows:
                files = int(row.get("files") or 0)
                total = int(row.get("bytes") or 0)
                if not files:
                    continue
                largest = int(row.get("largestBytes") or 0)
                out.append({
                    "bucket": row.get("bucket"),
                    "files": files,
                    "bytes": total,
                    "largestBytes": largest,
                    "avgBytes": total // files,
                    "newest": _iso(row.get("newest")),
                    "oldest": _iso(row.get("oldest")),
                })
            return sorted(out, key=lambda item: item["bytes"], reverse=True)
        except Exception as err:
            log.warning("SQL storage summary failed: %s", err)
    root = tenancy.client_root(UPLOAD_DIR)
    out = []
    for bucket in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")) if root.is_dir() else []:
        files = [f for f in bucket.iterdir() if f.is_file() and not f.name.endswith(".part") and f.stat().st_size > 0]
        if not files:
            continue
        sizes = [f.stat().st_size for f in files]
        times = [f.stat().st_mtime for f in files]
        out.append({
            "bucket": bucket.name,
            "files": len(sizes),
            "bytes": sum(sizes),
            "largestBytes": max(sizes),
            "avgBytes": sum(sizes) // len(sizes),
            "newest": datetime.fromtimestamp(max(times), timezone.utc).isoformat(),
            "oldest": datetime.fromtimestamp(min(times), timezone.utc).isoformat(),
        })
    return sorted(out, key=lambda item: item["bytes"], reverse=True)


def _iso(value) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None
