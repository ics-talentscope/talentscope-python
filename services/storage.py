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

BUCKET_PREFIX = {
    "recordings": "interview-recording",
    "verifications": "verification",
    "resumes": "resume",
    "backups": "backup",
    "employee_test_recordings": "test-recording",
    "monitor": "monitor",
    "video_upload_chunks": "video-upload-chunks",
    "docs/BR": "doc-br",
    "docs/JD": "doc-jd",
    "docs/Resumes": "doc-resume",
    "docs/Corp Pool": "doc-corp-pool",
    "docs/Portal Mapping": "doc-portal-mapping",
}

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


def _sync_names(bucket: str) -> None:
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
        if name:
            target = disk_path(bucket, name)
            if not target.exists():
                target.touch()


def root(create: bool = True) -> Path:
    path = Path(UPLOAD_DIR)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _client_head() -> str:
    client = tenancy.current_client()
    return "" if client == tenancy.DEFAULT_CLIENT else f"{client}-"


def prefix(bucket: str) -> str:
    base = BUCKET_PREFIX.get(bucket) or re.sub(r"[^a-z0-9]+", "-", bucket.lower()).strip("-") or "files"
    return f"{_client_head()}{base}-"


def disk_path(bucket: str, name: str) -> Path:
    return root() / f"{prefix(bucket)}{safe_name(name)}"


def disk_names(bucket: str) -> list[str]:
    folder = root(create=False)
    head = prefix(bucket)
    if not folder.is_dir():
        return []
    return sorted(p.name[len(head):] for p in folder.iterdir() if p.is_file() and p.name.startswith(head))


def names(bucket: str) -> list[str]:
    _sync_names(bucket)
    return disk_names(bucket)


def drop_disk(bucket: str) -> None:
    for name in disk_names(bucket):
        disk_path(bucket, name).unlink(missing_ok=True)


def drop_stale(bucket_root: str, hours: int) -> int:
    folder = root(create=False)
    if not folder.is_dir():
        return 0
    head = prefix(bucket_root)
    cutoff = time.time() - hours * 3600
    removed = 0
    for path in folder.iterdir():
        if path.is_file() and path.name.startswith(head) and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def save(bucket: str, name: str, data: bytes) -> str:
    target = disk_path(bucket, name)
    target.write_bytes(data)
    _sql_put(bucket, safe_name(name), data)
    return safe_name(name)


def save_stream(bucket: str, name: str, source, max_bytes: int, signature: bytes = b"", chunk: int = 1024 * 1024) -> int:
    target = disk_path(bucket, name)
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
        _sql_put(bucket, safe_name(name), target.read_bytes())
        return size
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def resolve(bucket: str, name: str) -> Optional[Path]:
    stored = safe_name(name)
    target = (root(create=False) / f"{prefix(bucket)}{stored}").resolve()
    if root(create=False).resolve() not in target.parents:
        return None
    _hydrate(bucket, stored, target)
    if not target.is_file() or target.stat().st_size == 0:
        return None
    return target


def read(bucket: str, name: str) -> Optional[bytes]:
    target = resolve(bucket, name)
    return target.read_bytes() if target else None


def remove_prefix(bucket: str, name_prefix: str) -> int:
    count = 0
    for name in names(bucket):
        if name.startswith(name_prefix):
            disk_path(bucket, name).unlink(missing_ok=True)
            count += 1
    sql_count = _sql_delete(bucket, prefix=name_prefix)
    return sql_count or count


def remove(bucket: str, name: str) -> None:
    target = disk_path(bucket, name)
    if target.is_file():
        target.unlink(missing_ok=True)
    _sql_delete(bucket, path=safe_name(name))


def clear_bucket(bucket: str) -> None:
    drop_disk(bucket)
    _sql_delete(bucket)


def prune_older_than(bucket: str, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for name in names(bucket):
        path = disk_path(bucket, name)
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
        disk_path(bucket, str(row.get("path") or "")).unlink(missing_ok=True)
    return _sql_delete(bucket, older_than_days=days) or removed


def _bucket_of(file_name: str) -> Optional[str]:
    head = _client_head()
    others = [f"{c}-" for c in tenancy.CLIENTS if c != tenancy.DEFAULT_CLIENT]
    if head and not file_name.startswith(head):
        return None
    if not head and any(file_name.startswith(other) for other in others):
        return None
    rest = file_name[len(head):]
    for bucket, base in BUCKET_PREFIX.items():
        if rest.startswith(f"{base}-"):
            return bucket
    return None


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
    folder = root(create=False)
    grouped: dict[str, list[Path]] = {}
    for item in folder.iterdir() if folder.is_dir() else []:
        bucket = _bucket_of(item.name) if item.is_file() and item.stat().st_size > 0 else None
        if bucket:
            grouped.setdefault(bucket, []).append(item)
    out = []
    for bucket, files in sorted(grouped.items()):
        sizes = [f.stat().st_size for f in files]
        times = [f.stat().st_mtime for f in files]
        out.append({
            "bucket": bucket,
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
