import hashlib
import json
import logging
import os
import re
import threading
import time
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from core import tenancy
from core.config import UPLOAD_DIR, env
from core.db import MssqlStore, TENANT_TABLES, get_store
from services import llm, question_queue

log = logging.getLogger("talentscope")
CACHE_S = 8.0
STALE_AFTER_S = 180
PERMISSION_HINTS = ("permission", "denied", "external policy action")
_cache: dict[str, tuple[float, Any]] = {}
_locks = {"vm": threading.Lock(), "db": threading.Lock()}


def monitor_dir() -> Path:
    return Path(env("MONITOR_DIR") or "/host-monitor")


def compare_database() -> str:
    return env("MONITOR_COMPARE_DB") or "TalentScope_DB_Live"


def section(fn: Callable[[], Any]) -> dict:
    try:
        return {"ok": True, "data": fn()}
    except Exception as err:
        message = str(err)
        return {"ok": False, "error": message[:300], "needsPermission": any(h in message.lower() for h in PERMISSION_HINTS)}


def rows(sql: str, params: tuple = ()) -> list[dict]:
    return get_store().query("SET NOCOUNT ON;\n" + sql, params)


def read_json(name: str):
    path = monitor_dir() / name
    try:
        return json.loads(path.read_text()), datetime.fromtimestamp(path.stat().st_mtime, timezone.utc), None
    except (OSError, ValueError) as err:
        return None, None, str(err)


def proc_cpu_times() -> list[int]:
    return [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]


def live_readings() -> dict:
    first = proc_cpu_times()
    time.sleep(0.4)
    second = proc_cpu_times()
    delta = [b - a for a, b in zip(first, second)]
    total = sum(delta) or 1
    idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if rest.strip().split():
            mem[key] = int(rest.strip().split()[0]) * 1024
    load = [float(v) for v in Path("/proc/loadavg").read_text().split()[:3]]
    total_mem, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    return {"at": datetime.now(timezone.utc).isoformat(), "cpuPercent": round(100 * (total - idle) / total, 1),
            "memTotal": total_mem, "memUsed": total_mem - avail,
            "memPercent": round(100 * (total_mem - avail) / total_mem, 1) if total_mem else 0,
            "swapTotal": mem.get("SwapTotal", 0), "swapUsed": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
            "load": load, "uptimeSeconds": round(float(Path("/proc/uptime").read_text().split()[0]))}


def app_runtime() -> dict:
    from core import security

    store = get_store()
    return {"llm": llm.stats(), "traffic": security.traffic_snapshot(), "questionQueue": question_queue.snapshot(),
            "dbPool": store.pool_stats() if isinstance(store, MssqlStore) else None}


def collect_vm() -> dict:
    snapshot, modified, error = read_json("snapshot.json")
    age = round((datetime.now(timezone.utc) - modified).total_seconds()) if modified else None
    try:
        live = live_readings()
    except Exception as err:
        live = {"error": str(err)}
    return {"collectedAt": datetime.now(timezone.utc).isoformat(),
            "collector": {"available": snapshot is not None, "dir": str(monitor_dir()),
                          "snapshotAt": modified.isoformat() if modified else None, "ageSeconds": age,
                          "stale": age is None or age > STALE_AFTER_S, "error": error},
            "live": live, "snapshot": snapshot, "runtime": app_runtime()}


def measure_latency() -> dict:
    samples = []
    for _ in range(3):
        started = time.perf_counter()
        rows("SELECT 1 AS ok")
        samples.append(round((time.perf_counter() - started) * 1000, 1))
    return {"samplesMs": samples, "avgMs": round(sum(samples) / len(samples), 1)}


def server_info() -> dict:
    props = rows("""
        SELECT CAST(SERVERPROPERTY('MachineName') AS nvarchar(128)) AS machine,
               CAST(SERVERPROPERTY('ServerName') AS nvarchar(128)) AS serverName,
               CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition,
               CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)) AS version,
               CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(64)) AS level,
               CAST(SERVERPROPERTY('ProductUpdateLevel') AS nvarchar(64)) AS updateLevel,
               CAST(SERVERPROPERTY('Collation') AS nvarchar(128)) AS collation,
               CAST(SERVERPROPERTY('IsHadrEnabled') AS int) AS alwaysOn,
               CAST(SERVERPROPERTY('IsClustered') AS int) AS [clustered],
               LEFT(@@VERSION, CHARINDEX(CHAR(10), @@VERSION + CHAR(10)) - 1) AS versionLine,
               DB_NAME() AS currentDb, SUSER_SNAME() AS [login], SYSDATETIMEOFFSET() AS serverTime,
               HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE') AS viewServerState,
               HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER PERFORMANCE STATE') AS viewServerPerformanceState,
               HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DATABASE STATE') AS viewDatabaseState,
               IS_MEMBER('db_owner') AS dbOwner, IS_SRVROLEMEMBER('sysadmin') AS sysadmin""")[0]
    try:
        host = rows("SELECT host_platform AS platform, host_distribution AS distribution, host_release AS [release] FROM sys.dm_os_host_info")
    except Exception:
        host = []
    server_time = props.get("serverTime")
    skew = None
    if isinstance(server_time, datetime):
        stamp = server_time if server_time.tzinfo else server_time.replace(tzinfo=timezone.utc)
        skew = round((datetime.now(timezone.utc) - stamp).total_seconds() * 1000)
    return {**props, "host": host[0] if host else None, "clockSkewMs": skew}


def pool_info() -> dict:
    store = get_store()
    stats = store.pool_stats()
    settings = store.settings
    return {"size": stats["idle"] + stats["inUse"], "available": stats["idle"], "pending": None, "borrowed": stats["inUse"],
            "max": stats["max"], "server": settings["server"], "port": settings["port"], "encrypt": settings["encrypt"]}


DATABASES_SQL = """
    SELECT name, state_desc AS state, recovery_model_desc AS recovery, compatibility_level AS compat,
           create_date AS created, log_reuse_wait_desc AS logReuseWait, is_read_only AS readOnly,
           user_access_desc AS access, page_verify_option_desc AS pageVerify, is_auto_shrink_on AS autoShrink,
           is_auto_close_on AS autoClose, snapshot_isolation_state_desc AS snapshotIsolation,
           is_read_committed_snapshot_on AS rcsi, is_query_store_on AS queryStore, collation_name AS collation
    FROM sys.databases ORDER BY database_id"""

FILES_SQL = """
    SELECT name, type_desc AS type, physical_name AS path, state_desc AS state,
           CAST(size AS bigint) * 8 / 1024.0 AS sizeMb,
           CAST(FILEPROPERTY(name, 'SpaceUsed') AS bigint) * 8 / 1024.0 AS usedMb,
           CASE WHEN is_percent_growth = 1 THEN CONCAT(growth, '%') ELSE CONCAT(CAST(growth AS bigint) * 8 / 1024, ' MB') END AS growth,
           CASE WHEN max_size = -1 THEN NULL ELSE CAST(max_size AS bigint) * 8 / 1024.0 END AS maxMb
    FROM sys.database_files"""

TABLES_SQL = """
    SELECT s.name AS schemaName, t.name, t.create_date AS created, t.modify_date AS modified,
           SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.row_count ELSE 0 END) AS [rowCount],
           SUM(ps.reserved_page_count) * 8 / 1024.0 AS reservedMb,
           SUM(ps.used_page_count) * 8 / 1024.0 AS usedMb,
           SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.in_row_data_page_count + ps.lob_used_page_count + ps.row_overflow_used_page_count ELSE 0 END) * 8 / 1024.0 AS dataMb,
           (SELECT COUNT(*) FROM sys.indexes i WHERE i.object_id = t.object_id AND i.index_id > 0) AS indexCount,
           CASE WHEN EXISTS (SELECT 1 FROM sys.indexes i WHERE i.object_id = t.object_id AND i.index_id = 0) THEN 1 ELSE 0 END AS isHeap,
           (SELECT MAX(STATS_DATE(st.object_id, st.stats_id)) FROM sys.stats st WHERE st.object_id = t.object_id) AS statsUpdated
    FROM sys.tables t
    JOIN sys.schemas s ON s.schema_id = t.schema_id
    JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
    GROUP BY s.name, t.name, t.object_id, t.create_date, t.modify_date
    ORDER BY reservedMb DESC"""

_TENANT_SQL_LIST = ", ".join(
    f"'{name}'" for name in sorted(TENANT_TABLES) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
)


def tables_sql() -> str:
    """Hide client-owned row counts from the cross-client DB inventory."""
    if not tenancy.client_filter():
        return TABLES_SQL
    return TABLES_SQL.replace(
        "FROM sys.tables t",
        f"FROM sys.tables t WHERE t.name NOT IN ({_TENANT_SQL_LIST})",
    )


INDEX_SQL = """
    SELECT TOP 20 OBJECT_NAME(ps.object_id) AS tableName, i.name AS indexName, i.type_desc AS type,
           CAST(ps.avg_fragmentation_in_percent AS decimal(5, 1)) AS fragmentation, ps.page_count AS pages
    FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED') ps
    JOIN sys.indexes i ON i.object_id = ps.object_id AND i.index_id = ps.index_id
    WHERE ps.page_count > 50 AND ps.alloc_unit_type_desc = 'IN_ROW_DATA'
    ORDER BY ps.avg_fragmentation_in_percent DESC"""

BACKUPS_SQL = """
    SELECT TOP 30 database_name AS databaseName,
           CASE type WHEN 'D' THEN 'Full' WHEN 'I' THEN 'Differential' WHEN 'L' THEN 'Log' ELSE type END AS type,
           backup_start_date AS started, backup_finish_date AS finished,
           DATEDIFF(second, backup_start_date, backup_finish_date) AS seconds,
           CAST(backup_size / 1048576.0 AS decimal(12, 1)) AS sizeMb,
           CAST(compressed_backup_size / 1048576.0 AS decimal(12, 1)) AS compressedMb,
           is_copy_only AS copyOnly, recovery_model AS recovery
    FROM msdb.dbo.backupset ORDER BY backup_finish_date DESC"""

LOG_USAGE_SQL = """
    SELECT total_log_size_in_bytes AS totalBytes, used_log_space_in_bytes AS usedBytes,
           used_log_space_in_percent AS usedPercent, log_space_in_bytes_since_last_backup AS sinceLastBackupBytes
    FROM sys.dm_db_log_space_usage"""

LOG_FALLBACK_SQL = """
    SELECT SUM(CAST(f.size AS bigint)) * 8192 AS totalBytes,
           SUM(CAST(FILEPROPERTY(f.name, 'SpaceUsed') AS bigint)) * 8192 AS usedBytes,
           CAST(100.0 * SUM(CAST(FILEPROPERTY(f.name, 'SpaceUsed') AS bigint)) / NULLIF(SUM(CAST(f.size AS bigint)), 0) AS decimal(5, 1)) AS usedPercent,
           (SELECT log_reuse_wait_desc FROM sys.databases WHERE database_id = DB_ID()) AS truncationHoldup
    FROM sys.database_files f WHERE f.type = 1"""

LOG_STATS_SQL = """
    SELECT recovery_model AS recovery, log_truncation_holdup_reason AS truncationHoldup, total_vlf_count AS totalVlfs,
           active_vlf_count AS activeVlfs, log_backup_time AS lastLogBackup, log_since_last_log_backup_mb AS sinceLastLogBackupMb
    FROM sys.dm_db_log_stats(DB_ID())"""

FRESHNESS_TARGETS = (
    ("Resumes", "resumes", "created_at"), ("Employee tests", "tests", "created_at"),
    ("Test attempts", "test_attempts", "created_at"), ("Employees (last update)", "employees", "updated_at"),
    ("Audit log", "audit_logs", "created_at"), ("Interview sessions", "candidate_sessions", "created_at"),
    ("Job descriptions", "job_descriptions", "created_at"),
)


def try_rows(sql: str, fallback: list | None = None) -> list[dict]:
    try:
        return rows(sql)
    except Exception:
        return fallback if fallback is not None else []


def table_exists(name: str) -> bool:
    return bool(rows(f"SELECT 1 AS ok WHERE OBJECT_ID(N'dbo.{name}', N'U') IS NOT NULL"))


def log_space() -> dict:
    usage = try_rows(LOG_USAGE_SQL) or rows(LOG_FALLBACK_SQL)
    stats = try_rows(LOG_STATS_SQL)
    return {**(usage[0] if usage else {}), **(stats[0] if stats else {})}


def compare_with_other_db(current: str):
    other = compare_database()
    if not other.replace("_", "").isalnum() or other == current:
        return None
    if not rows(f"SELECT 1 AS ok FROM sys.databases WHERE name = N'{other}' AND state_desc = 'ONLINE'"):
        return None
    tenant_filter = f"WHERE t.name NOT IN ({_TENANT_SQL_LIST})" if tenancy.client_filter() else ""
    tables = rows(f"""
        SELECT t.name, SUM(p.rows) AS [rowCount]
        FROM [{other}].sys.tables t JOIN [{other}].sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
        {tenant_filter}
        GROUP BY t.name""")
    return {"database": other, "tables": tables}


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET = re.compile(r"password|passwd|secret|token|salt|hash|credential", re.I)
_COMPARE_TYPES = {
    "bigint", "int", "smallint", "tinyint", "bit", "decimal", "numeric", "money", "smallmoney", "float", "real",
    "date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time",
    "char", "varchar", "nchar", "nvarchar", "uniqueidentifier", "xml",
}
_DIFF_LIMIT = 25


def _sql_name(value: str) -> str:
    if not _NAME.match(value or ""):
        raise ValueError("Invalid SQL name")
    return f"[{value}]"


def _type_label(column: dict) -> str:
    kind = str(column.get("typeName") or "")
    length = int(column.get("maxLength") or 0)
    if kind in ("nvarchar", "nchar"):
        length = length // 2
    if length < 0 or kind in ("xml", "text", "ntext"):
        sized = "max" if kind in ("varchar", "nvarchar", "varbinary") else kind
        return f"{kind}(max)" if kind in ("varchar", "nvarchar", "varbinary") else sized
    if kind in ("varchar", "nvarchar", "char", "nchar", "binary", "varbinary"):
        return f"{kind}({length})"
    return kind


def _table_columns(database: str, table: str) -> list[dict]:
    return rows(f"""
        SELECT c.name AS name, ty.name AS typeName, c.max_length AS maxLength, c.is_nullable AS nullable
        FROM {_sql_name(database)}.sys.columns c
        JOIN {_sql_name(database)}.sys.types ty ON ty.user_type_id = c.user_type_id
        WHERE c.object_id = OBJECT_ID(N'{database}.dbo.{table}')
        ORDER BY c.column_id""")


def _primary_key(database: str, table: str) -> list[str]:
    found = rows(f"""
        SELECT c.name AS name
        FROM {_sql_name(database)}.sys.indexes i
        JOIN {_sql_name(database)}.sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN {_sql_name(database)}.sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE i.is_primary_key = 1 AND i.object_id = OBJECT_ID(N'{database}.dbo.{table}')
        ORDER BY ic.key_ordinal""")
    return [str(row["name"]) for row in found if _NAME.match(str(row.get("name") or ""))]


def _display(column: str, value: Any) -> Any:
    if _SECRET.search(column):
        return None if value in (None, "") else "(hidden)"
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return f"{len(value)} bytes"
    if isinstance(value, Decimal):
        value = str(value)
    elif hasattr(value, "isoformat"):
        value = value.isoformat()
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= 240 else text[:240] + "…"


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, (bytes, bytearray)) or isinstance(right, (bytes, bytearray)):
        return hashlib.sha256(bytes(left or b"")).digest() == hashlib.sha256(bytes(right or b"")).digest() and type(left) is type(right)
    if hasattr(left, "isoformat"):
        left = left.isoformat()
    if hasattr(right, "isoformat"):
        right = right.isoformat()
    if isinstance(left, Decimal):
        left = str(left)
    if isinstance(right, Decimal):
        right = str(right)
    return left == right


def _key_label(columns: list[str], row: dict) -> str:
    return ", ".join(f"{column}={_display(column, row.get(column))}" for column in columns)


def _schema_changes(current_cols: list[dict], other_cols: list[dict], current: str, other: str) -> list[dict]:
    left = {str(col["name"]): col for col in current_cols if _NAME.match(str(col.get("name") or ""))}
    right = {str(col["name"]): col for col in other_cols if _NAME.match(str(col.get("name") or ""))}
    changes = []
    for name in sorted(set(left) | set(right)):
        a, b = left.get(name), right.get(name)
        if a and not b:
            changes.append({"column": name, "change": f"only in {current}", "currentType": _type_label(a)})
        elif b and not a:
            changes.append({"column": name, "change": f"only in {other}", "otherType": _type_label(b)})
        elif _type_label(a) != _type_label(b) or bool(a.get("nullable")) != bool(b.get("nullable")):
            changes.append({
                "column": name, "change": "type or nullability differs",
                "currentType": _type_label(a), "otherType": _type_label(b),
                "currentNullable": bool(a.get("nullable")), "otherNullable": bool(b.get("nullable")),
            })
    return changes


def _row_where(keys: list[str]) -> str:
    return " AND ".join(f"src.{_sql_name(key)} = ?" for key in keys)


def _preview_row(row: dict, columns: list[str], keys: list[str]) -> dict:
    shown = [column for column in columns if column not in keys][:6]
    return {"key": _key_label(keys, row), "fields": {column: _display(column, row.get(column)) for column in shown}}


def compare_table_detail(table: str) -> dict:
    if not _NAME.match(table or ""):
        raise ValueError("Choose a table name")
    if table.lower() in TENANT_TABLES and tenancy.client_filter():
        return {
            "table": table, "current": "", "other": compare_database(),
            "inCurrent": True, "inOther": True,
            "schema": [], "key": [], "onlyInCurrent": [], "onlyInOther": [], "changed": [],
            "truncated": False,
            "note": "Row-level comparison is disabled for client-owned tables.",
        }
    store = get_store()
    if not isinstance(store, MssqlStore):
        raise ValueError("SQL Server is not configured")
    current = str((rows("SELECT DB_NAME() AS name") or [{"name": ""}])[0].get("name") or "")
    other = compare_database()
    if not _NAME.match(current) or not _NAME.match(other) or current == other:
        raise ValueError("The two databases are not available to compare")
    if not rows("SELECT 1 AS ok FROM sys.databases WHERE name = ? AND state_desc = 'ONLINE'", (other,)):
        raise ValueError(f"{other} is not online")
    current_id = rows("SELECT OBJECT_ID(?) AS id", (f"{current}.dbo.{table}",))
    other_id = rows("SELECT OBJECT_ID(?) AS id", (f"{other}.dbo.{table}",))
    in_current = bool(current_id and current_id[0].get("id"))
    in_other = bool(other_id and other_id[0].get("id"))
    current_cols = _table_columns(current, table) if in_current else []
    other_cols = _table_columns(other, table) if in_other else []
    detail = {
        "table": table, "current": current, "other": other,
        "inCurrent": in_current, "inOther": in_other,
        "schema": _schema_changes(current_cols, other_cols, current, other),
        "key": [], "onlyInCurrent": [], "onlyInOther": [], "changed": [],
        "truncated": False, "note": "",
    }
    if not in_current or not in_other:
        detail["note"] = f"This table exists only in {current if in_current else other}."
        return detail
    keys = _primary_key(current, table)
    other_keys = _primary_key(other, table)
    if keys != other_keys:
        shared = [name for name in keys if name in other_keys]
        keys = shared or ([ "id" ] if any(col["name"] == "id" for col in current_cols) and any(col["name"] == "id" for col in other_cols) else [])
    elif not keys and any(col["name"] == "id" for col in current_cols) and any(col["name"] == "id" for col in other_cols):
        keys = ["id"]
    detail["key"] = keys
    names = [str(col["name"]) for col in current_cols if _NAME.match(str(col.get("name") or ""))]
    if not keys:
        detail["note"] = "This table has no primary key, so individual rows cannot be lined up. The row-count difference is still shown in the table list."
        return detail
    comparable = [
        col for col in current_cols
        if str(col.get("name")) in names and str(col.get("typeName")) in _COMPARE_TYPES
        and any(other["name"] == col["name"] and other.get("typeName") == col.get("typeName") for other in other_cols)
    ]
    key_sql = ", ".join(_sql_name(key) for key in keys)
    current_from = f"{_sql_name(current)}.dbo.{_sql_name(table)}"
    other_from = f"{_sql_name(other)}.dbo.{_sql_name(table)}"
    only_current = rows(f"SELECT TOP {_DIFF_LIMIT + 1} {key_sql} FROM (SELECT {key_sql} FROM {current_from} EXCEPT SELECT {key_sql} FROM {other_from}) d")
    only_other = rows(f"SELECT TOP {_DIFF_LIMIT + 1} {key_sql} FROM (SELECT {key_sql} FROM {other_from} EXCEPT SELECT {key_sql} FROM {current_from}) d")
    if len(only_current) > _DIFF_LIMIT or len(only_other) > _DIFF_LIMIT:
        detail["truncated"] = True
    preview_cols = names[:8]

    def load_side(database: str, key_rows: list[dict]) -> list[dict]:
        loaded = []
        for key_row in key_rows[:_DIFF_LIMIT]:
            values = tuple(key_row.get(key) for key in keys)
            found = rows(
                f"SELECT TOP 1 {', '.join(_sql_name(column) for column in preview_cols)} FROM {_sql_name(database)}.dbo.{_sql_name(table)} AS src WHERE {_row_where(keys)}",
                values,
            )
            if found:
                loaded.append(_preview_row({**key_row, **found[0]}, preview_cols, keys))
        return loaded

    detail["onlyInCurrent"] = load_side(current, only_current)
    detail["onlyInOther"] = load_side(other, only_other)
    if comparable:
        checks = []
        for column in comparable:
            quoted = _sql_name(str(column["name"]))
            checks.append(
                f"(a.{quoted} IS NULL AND b.{quoted} IS NOT NULL) OR (a.{quoted} IS NOT NULL AND b.{quoted} IS NULL) "
                f"OR ISNULL(DATALENGTH(a.{quoted}), -1) <> ISNULL(DATALENGTH(b.{quoted}), -1) "
                f"OR ISNULL(CONVERT(nvarchar(400), a.{quoted}), N'<NULL>') <> ISNULL(CONVERT(nvarchar(400), b.{quoted}), N'<NULL>')"
            )
        changed_keys = rows(
            f"SELECT TOP {_DIFF_LIMIT + 1} {', '.join(f'a.{_sql_name(key)} AS {_sql_name(key)}' for key in keys)} "
            f"FROM {current_from} a INNER JOIN {other_from} b ON "
            + " AND ".join(f"a.{_sql_name(key)} = b.{_sql_name(key)}" for key in keys)
            + " WHERE " + " OR ".join(checks)
        )
        if len(changed_keys) > _DIFF_LIMIT:
            detail["truncated"] = True
        select_list = ", ".join(_sql_name(str(column["name"])) for column in comparable)
        changed = []
        for key_row in changed_keys[:_DIFF_LIMIT]:
            values = tuple(key_row.get(key) for key in keys)
            left = rows(f"SELECT TOP 1 {select_list} FROM {current_from} AS src WHERE {_row_where(keys)}", values)
            right = rows(f"SELECT TOP 1 {select_list} FROM {other_from} AS src WHERE {_row_where(keys)}", values)
            if not left or not right:
                continue
            columns = []
            for column in comparable:
                name = str(column["name"])
                if _same(left[0].get(name), right[0].get(name)):
                    continue
                columns.append({"column": name, "current": _display(name, left[0].get(name)), "other": _display(name, right[0].get(name))})
            if columns:
                changed.append({"key": _key_label(keys, key_row), "columns": columns})
        detail["changed"] = changed
    if not detail["onlyInCurrent"] and not detail["onlyInOther"] and not detail["changed"] and not detail["schema"]:
        detail["note"] = "No column or row differences were found for this table."
    elif detail["truncated"]:
        detail["note"] = f"Showing the first {_DIFF_LIMIT} differences."
    return detail


def storage_usage() -> list[dict]:
    from services import storage

    return storage.usage_summary()


def app_activity():
    if not table_exists("audit_logs"):
        return None
    window = "created_at >= DATEADD(hour, -24, SYSUTCDATETIME())"
    clause, params = tenancy.sql_filter()
    totals = rows(f"""
        SELECT COUNT(*) AS last24h, SUM(CASE WHEN action LIKE '%FAIL%' THEN 1 ELSE 0 END) AS failures24h,
               SUM(CASE WHEN created_at >= DATEADD(hour, -1, SYSUTCDATETIME()) THEN 1 ELSE 0 END) AS lastHour
        FROM dbo.audit_logs WHERE {window}{clause}""", params)[0]
    return {**totals,
            "byAction": rows(
                f"SELECT TOP 15 action, COUNT(*) AS count FROM dbo.audit_logs WHERE {window}{clause} GROUP BY action ORDER BY count DESC",
                params,
            ),
            "perDay": rows(f"""
                SELECT CONVERT(char(10), CAST(created_at AS date), 23) AS day, COUNT(*) AS total,
                       SUM(CASE WHEN action LIKE '%FAIL%' THEN 1 ELSE 0 END) AS failures
                FROM dbo.audit_logs WHERE created_at >= DATEADD(day, -14, SYSUTCDATETIME()){clause}
                GROUP BY CAST(created_at AS date) ORDER BY day""", params),
            "recentFailures": rows(f"""
                SELECT TOP 15 created_at AS at, action, actor_email AS actor, ip_address AS ip
                FROM dbo.audit_logs WHERE action LIKE '%FAIL%'{clause} ORDER BY created_at DESC""", params)}


def freshness() -> list[dict]:
    out = []
    clause, params = tenancy.sql_filter()
    for label, table, column in FRESHNESS_TARGETS:
        if not table_exists(table):
            continue
        try:
            newest = rows(f"SELECT MAX({column}) AS newest FROM dbo.{table}{clause}", params)
        except Exception:
            newest = [{"newest": None}]
        out.append({"label": label, "table": table, "newest": newest[0]["newest"] if newest else None})
    return out


def migrations() -> list[dict]:
    if not table_exists("__migrations"):
        return []
    return rows("SELECT filename, applied_at AS appliedAt FROM dbo.__migrations ORDER BY id")


def server_resources() -> dict:
    info = rows("""SELECT cpu_count AS cpus, hyperthread_ratio AS hyperthreadRatio, physical_memory_kb AS physicalKb,
                          committed_kb AS committedKb, committed_target_kb AS targetKb, sqlserver_start_time AS startedAt
                   FROM sys.dm_os_sys_info""")[0]
    mem = rows("""SELECT total_physical_memory_kb AS totalKb, available_physical_memory_kb AS availableKb,
                         system_memory_state_desc AS state FROM sys.dm_os_sys_memory""")[0]
    proc = rows("""SELECT physical_memory_in_use_kb AS inUseKb, memory_utilization_percentage AS utilisation,
                          process_physical_memory_low AS physicalLow, process_virtual_memory_low AS virtualLow
                   FROM sys.dm_os_process_memory""")[0]
    return {**info, "memory": mem, "process": proc}


CPU_HISTORY_SQL = """
    DECLARE @ticks bigint = (SELECT cpu_ticks / (cpu_ticks / ms_ticks) FROM sys.dm_os_sys_info);
    SELECT TOP 60 DATEADD(ms, -1 * (@ticks - [timestamp]), SYSUTCDATETIME()) AS t,
           record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS sql,
           100 - record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int')
               - record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS other
    FROM (SELECT [timestamp], CONVERT(xml, record) AS record FROM sys.dm_os_ring_buffers
          WHERE ring_buffer_type = N'RING_BUFFER_SCHEDULER_MONITOR' AND record LIKE N'%<SystemHealth>%') x
    ORDER BY [timestamp] DESC"""

IDLE_WAITS = ("SLEEP_TASK", "BROKER_TASK_STOP", "BROKER_TO_FLUSH", "SQLTRACE_BUFFER_FLUSH", "CLR_AUTO_EVENT", "CLR_MANUAL_EVENT",
              "LAZYWRITER_SLEEP", "CHECKPOINT_QUEUE", "WAITFOR", "XE_TIMER_EVENT", "XE_DISPATCHER_WAIT", "FT_IFTS_SCHEDULER_IDLE_WAIT",
              "LOGMGR_QUEUE", "REQUEST_FOR_DEADLOCK_SEARCH", "SQLTRACE_INCREMENTAL_FLUSH_SLEEP", "DIRTY_PAGE_POLL",
              "HADR_FILESTREAM_IOMGR_IOCOMPLETION", "SP_SERVER_DIAGNOSTICS_SLEEP", "BROKER_EVENTHANDLER", "ONDEMAND_TASK_QUEUE",
              "SLEEP_SYSTEMTASK", "QDS_PERSIST_TASK_MAIN_LOOP_SLEEP", "QDS_ASYNC_QUEUE",
              "QDS_CLEANUP_STALE_QUERIES_TASK_MAIN_LOOP_SLEEP", "PWAIT_ALL_COMPONENTS_INITIALIZED",
              "PWAIT_EXTENSIBILITY_CLEANUP_TASK", "PREEMPTIVE_OS_FLUSHFILEBUFFERS", "SOS_WORK_DISPATCHER", "PVS_PREALLOCATE")

WAITS_SQL = f"""
    SELECT TOP 12 wait_type AS waitType, waiting_tasks_count AS tasks, wait_time_ms AS waitMs, signal_wait_time_ms AS signalMs,
           CAST(100.0 * wait_time_ms / NULLIF(SUM(wait_time_ms) OVER (), 0) AS decimal(5, 1)) AS pct
    FROM sys.dm_os_wait_stats
    WHERE wait_type NOT IN ({", ".join(f"'{w}'" for w in IDLE_WAITS)}) AND wait_time_ms > 0
    ORDER BY wait_time_ms DESC"""

SESSIONS_SQL = """
    SELECT s.session_id AS id, s.login_name AS [login], s.host_name AS host, s.program_name AS program, s.status,
           DB_NAME(s.database_id) AS db, s.cpu_time AS cpuMs, s.memory_usage * 8 AS memKb, s.reads, s.writes,
           s.last_request_end_time AS lastRequest, r.status AS requestStatus, r.command, r.wait_type AS waitType,
           r.blocking_session_id AS blockedBy, r.total_elapsed_time AS elapsedMs
    FROM sys.dm_exec_sessions s LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
    WHERE s.is_user_process = 1 ORDER BY s.cpu_time DESC"""

TOP_QUERIES_SQL = """
    SELECT TOP 10 qs.execution_count AS executions, qs.total_worker_time / 1000 AS totalCpuMs,
           qs.total_worker_time / NULLIF(qs.execution_count, 0) / 1000.0 AS avgCpuMs,
           qs.total_elapsed_time / NULLIF(qs.execution_count, 0) / 1000.0 AS avgElapsedMs,
           qs.total_logical_reads / NULLIF(qs.execution_count, 0) AS avgReads, qs.last_execution_time AS lastRun,
           DB_NAME(st.dbid) AS db, LEFT(REPLACE(REPLACE(st.text, CHAR(13), ' '), CHAR(10), ' '), 300) AS sqlText
    FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
    ORDER BY qs.total_worker_time DESC"""

IO_SQL = """
    SELECT DB_NAME(vfs.database_id) AS db, mf.name AS fileName, mf.type_desc AS type,
           vfs.num_of_reads AS reads, vfs.num_of_writes AS writes,
           CAST(vfs.io_stall_read_ms / NULLIF(vfs.num_of_reads, 0) AS decimal(10, 1)) AS readLatencyMs,
           CAST(vfs.io_stall_write_ms / NULLIF(vfs.num_of_writes, 0) AS decimal(10, 1)) AS writeLatencyMs,
           vfs.num_of_bytes_read / 1048576 AS readMb, vfs.num_of_bytes_written / 1048576 AS writtenMb
    FROM sys.dm_io_virtual_file_stats(NULL, NULL) vfs
    JOIN sys.master_files mf ON mf.database_id = vfs.database_id AND mf.file_id = vfs.file_id
    ORDER BY vfs.io_stall_read_ms + vfs.io_stall_write_ms DESC"""

VOLUMES_SQL = """
    SELECT DISTINCT vs.volume_mount_point AS mount, vs.logical_volume_name AS label, vs.total_bytes AS totalBytes,
           vs.available_bytes AS availableBytes
    FROM sys.master_files mf CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs"""

COUNTERS_SQL = """
    SELECT RTRIM(counter_name) AS counter, RTRIM(instance_name) AS instance, cntr_value AS value
    FROM sys.dm_os_performance_counters
    WHERE counter_name IN ('Page life expectancy', 'User Connections', 'Batch Requests/sec', 'SQL Compilations/sec',
                           'Lock Waits/sec', 'Number of Deadlocks/sec', 'Memory Grants Pending', 'Buffer cache hit ratio',
                           'Buffer cache hit ratio base', 'Target Server Memory (KB)', 'Total Server Memory (KB)', 'Processes blocked')
      AND (instance_name = '' OR instance_name = '_Total')"""

ERROR_PATTERN = ("error", "fail", "severity", "deadlock", "stack", "corrupt", "login failed", "i/o")


def session_summary() -> dict:
    listing = rows(SESSIONS_SQL)
    return {"total": len(listing), "blocked": sum(1 for s in listing if (s.get("blockedBy") or 0) > 0), "list": listing[:40]}


def error_log() -> list[dict]:
    entries = rows("EXEC sys.xp_readerrorlog 0, 1")
    hits = [r for r in entries if any(p in str(r.get("Text", "")).lower() for p in ERROR_PATTERN)]
    return [{"at": r.get("LogDate"), "source": r.get("ProcessInfo"), "text": str(r.get("Text", ""))[:400]} for r in hits[-40:][::-1]]


def collect_db() -> dict:
    collected = datetime.now(timezone.utc).isoformat()
    store = get_store()
    if not isinstance(store, MssqlStore):
        return {"collectedAt": collected, "configured": False,
                "connection": {"ok": False, "error": "SQL Server is not configured (DB_TARGET / MSSQL_* env)"}}
    started = time.perf_counter()
    server = section(server_info)
    if not server["ok"]:
        return {"collectedAt": collected, "configured": True, "connection": {"ok": False, "error": server["error"]}}
    current = str(server["data"].get("currentDb") or "")
    out = {
        "collectedAt": collected, "configured": True, "connection": section(pool_info), "server": server,
        "latency": section(measure_latency), "databases": section(lambda: rows(DATABASES_SQL)),
        "files": section(lambda: rows(FILES_SQL)), "log": section(log_space), "tables": section(lambda: rows(tables_sql())),
        "compare": section(lambda: compare_with_other_db(current)), "storage": section(storage_usage),
        "indexes": section(lambda: rows(INDEX_SQL)), "backups": section(lambda: rows(BACKUPS_SQL)),
        "activity": section(app_activity), "freshness": section(freshness), "migrations": section(migrations),
        "serverLevel": {
            "resources": section(server_resources), "cpu": section(lambda: rows(CPU_HISTORY_SQL)),
            "waits": section(lambda: rows(WAITS_SQL)), "sessions": section(session_summary),
            "topQueries": section(lambda: rows(TOP_QUERIES_SQL)), "io": section(lambda: rows(IO_SQL)),
            "volumes": section(lambda: rows(VOLUMES_SQL)), "counters": section(lambda: rows(COUNTERS_SQL)),
            "errorLog": section(error_log),
        },
    }
    out["collectMs"] = round((time.perf_counter() - started) * 1000)
    return out


HISTORY_DAYS = 7
HISTORY_RETENTION_S = (HISTORY_DAYS + 1) * 86400
MAX_WINDOW_S = 25 * 3600
SHORT_WINDOW_S = 6 * 3600
DB_SAMPLE_S = 60
DB_PRUNE_S = 3600
_sampler = {"started": False, "prev_batch": None, "last_prune": 0.0}
_sampler_permissions = {"warned": False}
_history_lock = threading.Lock()


def db_history_path() -> Path:
    return UPLOAD_DIR / "monitor" / "db-history.jsonl"


def vm_history_path() -> Path:
    return monitor_dir() / "history.jsonl"


def parse_window(params, now: Optional[float] = None) -> tuple[int, int]:
    now = time.time() if now is None else now
    try:
        start, end = float(params.get("from")), float(params.get("to"))
    except (TypeError, ValueError):
        start = end = 0.0
    valid = end > start and end - start <= MAX_WINDOW_S and now - HISTORY_RETENTION_S - 86400 <= start <= now
    return (int(start), int(end) + (0 if end == int(end) else 1)) if valid else (int(now) - SHORT_WINDOW_S, int(now))


def read_history_file(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    points = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            point = json.loads(line)
        except ValueError:
            continue
        if isinstance(point, dict) and isinstance(point.get("ts"), (int, float)):
            points.append(point)
    return points


def downsample(points: list[dict], window: tuple[int, int]) -> dict:
    start, end = window
    bucket = 3600 if end - start > SHORT_WINDOW_S else 300
    sums: dict[int, dict[str, list[float]]] = {}
    for point in points:
        if not start <= point["ts"] < end:
            continue
        key = start + int((point["ts"] - start) // bucket) * bucket
        slot = sums.setdefault(key, {})
        for name, value in point.items():
            if name == "ts" or isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                continue
            total = slot.setdefault(name, [0.0, 0])
            total[0] += value
            total[1] += 1
    out = []
    for key in sorted(sums):
        entry = {"ts": key, "t": datetime.fromtimestamp(key, timezone.utc).isoformat()}
        entry.update({name: round(total / count, 2) for name, (total, count) in sums[key].items()})
        out.append(entry)
    return {"from": datetime.fromtimestamp(start, timezone.utc).isoformat(), "to": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            "bucketSeconds": bucket, "points": out,
            "oldestAt": datetime.fromtimestamp(points[0]["ts"], timezone.utc).isoformat() if points else None,
            "retentionDays": HISTORY_DAYS}


def legacy_vm_history(raw: bool = False) -> list[dict]:
    data, _, _ = read_json("history.json")
    if not isinstance(data, list):
        return []
    if raw:
        return [p for p in data if isinstance(p, dict)]
    return [p for p in data if isinstance(p, dict) and isinstance(p.get("ts"), (int, float))]


def history_points(target: str, raw: bool = False) -> list[dict]:
    if target == "db":
        return read_history_file(db_history_path())
    return read_history_file(vm_history_path()) or legacy_vm_history(raw=raw)


def _round(value, digits: int = 1):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def db_point() -> dict:
    now = int(time.time())
    point: dict = {"ts": now, "t": datetime.fromtimestamp(now, timezone.utc).isoformat()}
    started = time.perf_counter()
    rows("SELECT 1 AS ok")
    point["latencyMs"] = _round((time.perf_counter() - started) * 1000)
    point["down"] = 0
    space = rows("""
        SELECT SUM(CASE WHEN type = 0 THEN CAST(size AS bigint) END) * 8 / 1024.0 AS dataMb,
               SUM(CASE WHEN type = 0 THEN CAST(FILEPROPERTY(name, 'SpaceUsed') AS bigint) END) * 8 / 1024.0 AS dataUsedMb,
               SUM(CASE WHEN type = 1 THEN CAST(size AS bigint) END) * 8 / 1024.0 AS logMb,
               SUM(CASE WHEN type = 1 THEN CAST(FILEPROPERTY(name, 'SpaceUsed') AS bigint) END) * 8 / 1024.0 AS logUsedMb
        FROM sys.database_files""")[0]
    point["dataMb"], point["dataUsedMb"], point["logMb"] = _round(space.get("dataMb")), _round(space.get("dataUsedMb")), _round(space.get("logMb"))
    point["logUsedPct"] = _round(100 * float(space["logUsedMb"]) / float(space["logMb"])) if space.get("logMb") else None
    store = get_store()
    if isinstance(store, MssqlStore):
        point["poolInUse"] = store.pool_stats()["inUse"]
    permissions = rows("""
        SELECT COALESCE(HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE'), 0) AS viewServerState,
               COALESCE(HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER PERFORMANCE STATE'), 0) AS viewServerPerformanceState
    """)[0]
    can_read_server_metrics = bool(
        permissions.get("viewServerState") or permissions.get("viewServerPerformanceState")
    )
    if not can_read_server_metrics:
        if not _sampler_permissions["warned"]:
            log.warning(
                "SQL Server performance history is unavailable: the database login lacks VIEW SERVER STATE "
                "and VIEW SERVER PERFORMANCE STATE; database-level health metrics remain available"
            )
            _sampler_permissions["warned"] = True
        return point
    try:
        counters = {r["counter"]: float(r["value"]) for r in rows(COUNTERS_SQL)}
        point["userConnections"] = counters.get("User Connections")
        point["blocked"] = counters.get("Processes blocked")
        point["pageLifeSec"] = counters.get("Page life expectancy")
        memory = counters.get("Total Server Memory (KB)")
        point["sqlMemoryMb"] = _round(memory / 1024, 0) if memory is not None else None
        batches = counters.get("Batch Requests/sec")
        if batches is not None:
            prev = _sampler["prev_batch"]
            if prev and batches >= prev[1] and 0 < now - prev[0] < 600:
                point["batchPerSec"] = _round((batches - prev[1]) / (now - prev[0]), 2)
            _sampler["prev_batch"] = (now, batches)
        cpu = rows(CPU_HISTORY_SQL)
        if cpu:
            point["sqlCpu"], point["otherCpu"] = cpu[0].get("sql"), cpu[0].get("other")
    except Exception:
        pass
    return point


def prune_db_history(path: Path) -> None:
    since = time.time() - HISTORY_RETENTION_S
    points = read_history_file(path)
    if not points or points[0]["ts"] >= since:
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(p, separators=(",", ":")) + "\n" for p in points if p["ts"] >= since), encoding="utf-8")
    tmp.replace(path)


def sample_db_once() -> None:
    path = db_history_path()
    try:
        point = db_point()
    except Exception as err:
        now = int(time.time())
        point = {"ts": now, "t": datetime.fromtimestamp(now, timezone.utc).isoformat(), "down": 1, "error": str(err)[:200]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with _history_lock:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(point, separators=(",", ":")) + "\n")
        if time.time() - _sampler["last_prune"] > DB_PRUNE_S:
            _sampler["last_prune"] = time.time()
            prune_db_history(path)


def _sampler_loop() -> None:
    time.sleep(20)
    while True:
        try:
            sample_db_once()
        except Exception:
            pass
        time.sleep(DB_SAMPLE_S)


def start_db_sampler() -> bool:
    if _sampler["started"] or not isinstance(get_store(), MssqlStore):
        return False
    _sampler["started"] = True
    threading.Thread(target=_sampler_loop, daemon=True, name="db-history").start()
    return True


COLLECTORS = {"vm": collect_vm, "db": collect_db}


def collect(target: str, window: Optional[tuple[int, int]] = None) -> dict:
    cache_key = f"{tenancy.current_client()}:{target}"
    with _locks[target]:
        hit = _cache.get(cache_key)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            body = hit[1]
        else:
            body = COLLECTORS[target]()
            _cache[cache_key] = (time.monotonic(), body)
    history = history_points(target, raw=window is None)
    # Direct callers (and legacy collectors) receive the raw points. The admin
    # API always passes a parsed window and receives the downsampled envelope.
    return {**body, "history": history if window is None else downsample(history, window)}
