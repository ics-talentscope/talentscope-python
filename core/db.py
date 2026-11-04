import json
import re
import struct
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional, Union

from core.config import db_target, env, mssql_settings, supabase_settings
from core.logger import logger
from core import request_context

JSON_COLUMNS = {
    "employees": {"badges"},
    "tests": {"in_progress", "proctoring", "ai_analysis"},
    "test_questions": {"options"},
    "employee_portal_question_bank": {"options"},
    "portal_settings": {"value"},
    "evaluations": {"bloom_scores", "bloom_submissions", "bloom_graded"},
    "candidate_interview_data": {"evaluation"},
    "question_sets": {"items"},
    "system_logs": {"request_payload", "response_payload"},
}

POSTGRES_JSON_COLUMNS = {
    "resumes": {"parsed", "analysis", "enhanced", "report"},
    "portal_settings": {"value"},
    "employees": {"badges"},
    "tests": {"in_progress", "proctoring", "ai_analysis"},
    "employee_portal_question_bank": {"options"},
    "audit_logs": {"details"},
    "candidate_interview_data": {"evaluation"},
    "test_questions": {"options"},
    "evaluations": {"bloom_scores", "bloom_submissions", "bloom_graded"},
    "question_sets": {"items"},
    "system_logs": {"request_payload", "response_payload"},
}

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
UUID_VALUE_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ISO_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")
MAX_IN = 1000
OPS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "like": "LIKE"}

Where = Union[None, dict, list]


def ident(name: str) -> str:
    if not IDENT_RE.match(name):
        raise ValueError(f"Invalid identifier: {name}")
    return f"[{name}]"


def normalize_where(where: Where) -> list[tuple]:
    if not where:
        return []
    if isinstance(where, dict):
        out = []
        for col, val in where.items():
            if val is None:
                out.append((col, "is_null", None))
            elif isinstance(val, (list, tuple, set)):
                out.append((col, "in", list(val)))
            else:
                out.append((col, "eq", val))
        return out
    return [tuple(item) if len(item) == 3 else (item[0], item[1], None) for item in where]


def normalize_order(order: Union[None, str, list]) -> list[tuple[str, bool]]:
    if not order:
        return []
    items = [order] if isinstance(order, str) else list(order)
    out = []
    for item in items:
        if isinstance(item, str):
            parts = item.split()
            out.append((parts[0], len(parts) > 1 and parts[1].lower() == "desc"))
        else:
            out.append((item[0], str(item[1]).lower() == "desc"))
    return out


def normalize_columns(columns: Union[None, str, Iterable[str]]) -> Optional[list[str]]:
    if not columns or columns == "*":
        return None
    if isinstance(columns, str):
        return [c.strip() for c in columns.split(",") if c.strip()]
    return list(columns)


def encode_value(column: str, value: Any, json_cols: set) -> Any:
    if column in json_cols:
        return None if value is None else json.dumps(value, ensure_ascii=False)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str) and (column.endswith("_at") or column.endswith("_date")) and ISO_VALUE_RE.match(value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            return value
    return value


def decode_row(table: str, row: dict) -> dict:
    json_cols = JSON_COLUMNS.get(table, set())
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = float(value)
        elif isinstance(value, uuid.UUID):
            value = str(value).lower()
        elif isinstance(value, str):
            if key in json_cols:
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            elif UUID_VALUE_RE.match(value):
                value = value.lower()
        out[key] = value
    return out


class Store:
    def select(self, table: str, where: Where = None, columns=None, order=None, limit: Optional[int] = None,
               offset: Optional[int] = None) -> list[dict]:
        raise NotImplementedError

    def first(self, table: str, where: Where = None, columns=None, order=None) -> Optional[dict]:
        rows = self.select(table, where, columns, order, limit=1)
        return rows[0] if rows else None

    def count(self, table: str, where: Where = None) -> int:
        raise NotImplementedError

    def insert(self, table: str, rows: Union[dict, list[dict]]) -> list[dict]:
        raise NotImplementedError

    def update(self, table: str, values: dict, where: Where) -> int:
        raise NotImplementedError

    def delete(self, table: str, where: Where) -> int:
        raise NotImplementedError

    def upsert(self, table: str, row: dict, key: Union[str, list[str]]) -> dict:
        raise NotImplementedError

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        raise NotImplementedError

    def execute(self, sql: str, params: tuple = ()) -> int:
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError


def _database_operation(statement: str) -> tuple[str, str | None]:
    operation_match = re.match(r"\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|EXEC(?:UTE)?)\b", statement, re.I)
    if not operation_match:
        return "QUERY", None
    operation = operation_match.group(1).upper().replace("EXECUTE", "EXEC")
    clause = {"SELECT": "FROM", "INSERT": "INTO", "UPDATE": "UPDATE", "DELETE": "FROM", "MERGE": "INTO"}.get(operation)
    if not clause:
        return operation, None
    table_match = re.search(
        rf"\b{clause}\b\s+(?:\[dbo\]\.)?\[?([A-Za-z_][A-Za-z0-9_]*)\]?",
        statement,
        re.I,
    )
    return operation, table_match.group(1) if table_match else None


def _log_database_operation(
    operation: str,
    table: str | None,
    started: float,
    error: Exception | None = None,
) -> None:
    if table and table.lower() == "system_logs":
        return
    failed = error is not None
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    level = "ERROR" if failed else "INFO"
    if not failed and elapsed_ms > 3000:
        level = "CRITICAL"
    elif not failed and elapsed_ms > 1000:
        level = "WARNING"
    fields = {
        "module": "database",
        "action": operation.lower(),
        "table": table,
        "execution_time_ms": elapsed_ms,
        "status": "failed" if failed else "success",
        "request_id": request_context.request_id(),
    }
    logger_fields = {
        "module": "database",
        "action": operation.lower(),
        "db_operation": operation,
        "table_name": table,
        "execution_time": elapsed_ms,
        "status": "failed" if failed else "success",
        **request_context.log_context(),
    }
    if failed:
        logger.bind(**fields).opt(exception=(type(error), error, error.__traceback__)).error(
            "Database operation failed"
        )
    else:
        logger.bind(**fields).log(level, "Database operation completed")
    try:
        from services.system_logs import record_log

        record_log({
            **logger_fields,
            "db_operation": operation,
            "log_level": level,
            "message": f"{operation} operation on {table or 'unknown table'}",
            "error_message": str(error) if error else None,
            "stack_trace": "".join(traceback.format_exception(error)) if error else None,
        })
    except Exception:
        logger.bind(module="logging", action="database_event_failed").exception(
            "Could not persist database operation event"
        )


def _dto_converter(raw: bytes) -> datetime:
    t = struct.unpack("<6hI2h", raw)
    return datetime(t[0], t[1], t[2], t[3], t[4], t[5], t[6] // 1000,
                    tzinfo=timezone(timedelta(hours=t[7], minutes=t[8])))


def _str_converter(raw: Optional[bytes]) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


class MssqlStore(Store):
    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or mssql_settings()
        self.pool_max = max(1, int(self.settings.get("pool_max") or 10))
        self.pool_wait_s = float(self.settings.get("pool_wait_s") or 20)
        self.slots = threading.BoundedSemaphore(self.pool_max)
        self.idle: list = []
        self.idle_lock = threading.Lock()

    def _conn_string(self) -> str:
        s = self.settings

        def esc(value: str) -> str:
            return "{" + value.replace("}", "}}") + "}"

        parts = [
            f"DRIVER={{{s['driver']}}}",
            f"SERVER={s['server']},{s['port']}",
            f"DATABASE={s['database']}",
            f"UID={esc(s['user'])}",
            f"PWD={esc(s['password'])}",
            f"Encrypt={'yes' if s['encrypt'] else 'no'}",
            f"TrustServerCertificate={'yes' if s['trust_cert'] else 'no'}",
            "Connection Timeout=15",
        ]
        return ";".join(parts)

    def _connect(self):
        import pyodbc

        conn = pyodbc.connect(self._conn_string(), autocommit=True)
        conn.add_output_converter(-155, _dto_converter)
        conn.add_output_converter(pyodbc.SQL_CHAR, _str_converter)
        conn.add_output_converter(pyodbc.SQL_VARCHAR, _str_converter)
        conn.add_output_converter(pyodbc.SQL_LONGVARCHAR, _str_converter)
        conn.setencoding(encoding="utf-8")
        return conn

    def _acquire(self):
        if not self.slots.acquire(timeout=self.pool_wait_s):
            raise RuntimeError("Database connection pool exhausted")
        try:
            with self.idle_lock:
                conn = self.idle.pop() if self.idle else None
            return conn or self._connect()
        except BaseException:
            self.slots.release()
            raise

    def _release(self, conn, broken: bool = False) -> None:
        try:
            if broken:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                with self.idle_lock:
                    self.idle.append(conn)
        finally:
            self.slots.release()

    def pool_stats(self) -> dict:
        with self.idle_lock:
            idle = len(self.idle)
        return {"max": self.pool_max, "idle": idle, "inUse": self.pool_max - self.slots._value}

    def _run(self, sql: str, params: tuple, fetch: bool):
        import pyodbc

        def transient(err) -> bool:
            state = str(err.args[0]) if err.args else ""
            return state.startswith("08") or state in {"HY000", "HYT00", "01000"}

        started = time.perf_counter()
        operation, table = _database_operation(sql)
        error = None
        try:
            for attempt in (0, 1):
                try:
                    conn = self._acquire()
                except pyodbc.Error as err:
                    if attempt == 0 and transient(err):
                        continue
                    raise
                broken = False
                try:
                    cursor = conn.cursor()
                    cursor.execute(sql, params)
                    if fetch and cursor.description:
                        names = [d[0] for d in cursor.description]
                        rows = [dict(zip(names, r)) for r in cursor.fetchall()]
                        cursor.close()
                        return rows
                    count = cursor.rowcount
                    cursor.close()
                    return count
                except pyodbc.Error as err:
                    broken = transient(err)
                    if attempt == 0 and broken:
                        continue
                    raise
                finally:
                    self._release(conn, broken)
        except Exception as exc:
            error = exc
            raise
        finally:
            _log_database_operation(operation, table, started, error)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [decode_row("", r) for r in self._run(sql, params, True)]

    def execute(self, sql: str, params: tuple = ()) -> int:
        return self._run(sql, params, False)

    def ping(self) -> bool:
        try:
            return bool(self._run("SELECT 1 AS ok", (), True))
        except Exception:
            return False

    def _where(self, table: str, where: list[tuple]) -> tuple[str, list]:
        json_cols = JSON_COLUMNS.get(table, set())
        clauses, params = [], []
        for col, op, val in where:
            c = ident(col)
            if op == "is_null":
                clauses.append(f"{c} IS NULL")
            elif op == "not_null":
                clauses.append(f"{c} IS NOT NULL")
            elif op in ("in", "not_in"):
                vals = list(val or [])
                if not vals:
                    clauses.append("1=0" if op == "in" else "1=1")
                    continue
                marks = ",".join("?" for _ in vals)
                clauses.append(f"{c} {'IN' if op == 'in' else 'NOT IN'} ({marks})")
                params.extend(encode_value(col, v, set()) for v in vals)
            elif op == "ilike":
                clauses.append(f"{c} LIKE ?")
                params.append(val)
            elif op in OPS:
                clauses.append(f"{c} {OPS[op]} ?")
                params.append(encode_value(col, val, set()))
            else:
                raise ValueError(f"Unsupported operator: {op}")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _split_big_in(self, where: list[tuple]):
        for i, (col, op, val) in enumerate(where):
            if op in ("in", "not_in") and len(val or []) > MAX_IN and op == "in":
                vals = list(val)
                for start in range(0, len(vals), MAX_IN):
                    yield where[:i] + [(col, "in", vals[start:start + MAX_IN])] + where[i + 1:]
                return
        yield where

    def select(self, table, where=None, columns=None, order=None, limit=None, offset=None):
        w = normalize_where(where)
        chunks = list(self._split_big_in(w))
        if len(chunks) > 1:
            rows = []
            for chunk in chunks:
                rows.extend(self.select(table, chunk, columns, order, None, None))
            for col, desc in reversed(normalize_order(order)):
                rows.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            start = offset or 0
            return rows[start:start + limit] if limit is not None else rows[start:]
        cols = normalize_columns(columns)
        col_sql = ", ".join(ident(c) for c in cols) if cols else "*"
        where_sql, params = self._where(table, w)
        order_items = normalize_order(order)
        order_sql = ", ".join(f"{ident(c)} {'DESC' if d else 'ASC'}" for c, d in order_items)
        if offset is not None:
            sql = f"SELECT {col_sql} FROM [dbo].{ident(table)}{where_sql} ORDER BY {order_sql or '(SELECT NULL)'} OFFSET ? ROWS"
            params.append(int(offset))
            if limit is not None:
                sql += " FETCH NEXT ? ROWS ONLY"
                params.append(int(limit))
        else:
            top = f"TOP ({int(limit)}) " if limit is not None else ""
            sql = f"SELECT {top}{col_sql} FROM [dbo].{ident(table)}{where_sql}"
            if order_sql:
                sql += f" ORDER BY {order_sql}"
        return [decode_row(table, r) for r in self._run(sql, tuple(params), True)]

    def count(self, table, where=None):
        where_sql, params = self._where(table, normalize_where(where))
        rows = self._run(f"SELECT COUNT(*) AS n FROM [dbo].{ident(table)}{where_sql}", tuple(params), True)
        return int(rows[0]["n"])

    def _insert_one(self, table: str, row: dict) -> dict:
        json_cols = JSON_COLUMNS.get(table, set())
        data = dict(row)
        if table != "portal_settings" and not data.get("id"):
            data["id"] = str(uuid.uuid4())
        cols = list(data.keys())
        marks = ",".join("?" for _ in cols)
        sql = (f"INSERT INTO [dbo].{ident(table)} ({', '.join(ident(c) for c in cols)}) "
               f"OUTPUT INSERTED.* VALUES ({marks})")
        params = tuple(encode_value(c, data[c], json_cols) for c in cols)
        rows = self._run(sql, params, True)
        return decode_row(table, rows[0]) if rows else decode_row(table, data)

    def insert(self, table, rows):
        items = [rows] if isinstance(rows, dict) else list(rows)
        return [self._insert_one(table, r) for r in items]

    def update(self, table, values, where):
        if not values:
            return 0
        json_cols = JSON_COLUMNS.get(table, set())
        cols = list(values.keys())
        set_sql = ", ".join(f"{ident(c)} = ?" for c in cols)
        set_params = [encode_value(c, values[c], json_cols) for c in cols]
        total = 0
        for chunk in self._split_big_in(normalize_where(where)):
            where_sql, params = self._where(table, chunk)
            result = self._run(f"UPDATE [dbo].{ident(table)} SET {set_sql}{where_sql}", tuple(set_params + params), False)
            total += max(int(result), 0)
        return total

    def delete(self, table, where):
        total = 0
        for chunk in self._split_big_in(normalize_where(where)):
            where_sql, params = self._where(table, chunk)
            result = self._run(f"DELETE FROM [dbo].{ident(table)}{where_sql}", tuple(params), False)
            total += max(int(result), 0)
        return total

    def upsert(self, table, row, key):
        keys = [key] if isinstance(key, str) else list(key)
        json_cols = JSON_COLUMNS.get(table, set())
        data = dict(row)
        key_filter = {k: data[k] for k in keys}
        # Prefer existence check: pyodbc rowcount is often -1 after UPDATE, which used to
        # trigger a duplicate INSERT and PK violations (e.g. portal_settings).
        if self.first(table, key_filter):
            updatable = {c: data[c] for c in data if c not in keys}
            if updatable:
                self.update(table, updatable, key_filter)
        else:
            self._insert_one(table, data)
        return self.first(table, key_filter) or decode_row(table, data)


class SupabaseStore(Store):
    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or supabase_settings()
        missing = [name for name in ("url", "service_role_key") if not self.settings.get(name)]
        if missing:
            missing_env = {
                "url": "NEXT_PUBLIC_SUPABASE_URL",
                "service_role_key": "SUPABASE_SERVICE_ROLE_KEY",
            }
            raise ValueError("Missing Supabase API setting(s): " +
                             ", ".join(missing_env[name] for name in missing))
        if not self.settings["url"].startswith("https://"):
            raise ValueError("Supabase URL must use HTTPS")

        import httpx

        self.client = httpx.Client(
            base_url=self.settings["url"] + "/rest/v1/",
            headers={
                "apikey": self.settings["service_role_key"],
                "Authorization": f"Bearer {self.settings['service_role_key']}",
                "Accept-Profile": "public",
                "Content-Profile": "public",
            },
            timeout=float(self.settings.get("timeout_s") or 20),
        )

    @staticmethod
    def _ident(name: str) -> str:
        if not IDENT_RE.fullmatch(name):
            raise ValueError(f"Invalid identifier: {name}")
        return name

    @classmethod
    def _table(cls, name: str) -> str:
        if not IDENT_RE.fullmatch(name):
            raise ValueError(f"Invalid identifier: {name}")
        return name

    def _request(self, method: str, table: str, params=None, payload=None, prefer: str | None = None):
        started = time.perf_counter()
        operation = {
            "GET": "SELECT", "HEAD": "SELECT", "POST": "INSERT",
            "PATCH": "UPDATE", "DELETE": "DELETE",
        }.get(method, method)
        table_path = self._table(table)
        headers = {"Prefer": prefer} if prefer else None
        error = None
        try:
            response = self.client.request(
                method,
                table_path,
                params=params,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return response, []
            data = response.json()
            return response, data if isinstance(data, list) else [data]
        except Exception as exc:
            error = exc
            raise
        finally:
            _log_database_operation(operation, table_path, started, error)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        raise NotImplementedError("Raw SQL is not supported by the Supabase Data API")

    def execute(self, sql: str, params: tuple = ()) -> int:
        raise NotImplementedError("Raw SQL is not supported by the Supabase Data API")

    def ping(self) -> bool:
        try:
            self._request("GET", "resumes", [("select", "id"), ("limit", "0")])
            return True
        except Exception:
            return False

    def pool_stats(self) -> dict:
        return {"max": 0, "idle": 0, "inUse": 0}

    @staticmethod
    def _filter_value(value: Any) -> str:
        if isinstance(value, (dict, list)):
            import json

            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        elif isinstance(value, bool):
            value = "true" if value else "false"
        elif value is None:
            value = "null"
        elif isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif isinstance(value, uuid.UUID):
            value = str(value)
        elif isinstance(value, Decimal):
            value = str(value)
        else:
            value = str(value)
        if any(char in value for char in ',()"\\') or value.strip() != value:
            value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return value

    def _where(self, table: str, where: list[tuple]) -> list[tuple[str, str]]:
        filters = []
        for col, op, val in where:
            c = self._ident(col)
            if op == "is_null":
                expression = "is.null"
            elif op == "not_null":
                expression = "not.is.null"
            elif op in ("in", "not_in"):
                vals = list(val or [])
                if not vals:
                    continue
                values = ",".join(self._filter_value(item) for item in vals)
                expression = f"{'in' if op == 'in' else 'not.in'}.({values})"
            elif op == "ilike":
                expression = f"ilike.{self._filter_value(val)}"
            elif op == "like":
                expression = f"like.{self._filter_value(val)}"
            elif op in OPS:
                pg_op = {"neq": "neq", "gt": "gt", "gte": "gte", "lt": "lt", "lte": "lte", "eq": "eq"}[op]
                expression = f"{pg_op}.{self._filter_value(val)}"
            else:
                raise ValueError(f"Unsupported operator: {op}")
            filters.append((c, expression))
        return filters

    def _split_big_in(self, where: list[tuple]):
        for i, (col, op, val) in enumerate(where):
            if op == "in" and len(val or []) > MAX_IN:
                vals = list(val)
                for start in range(0, len(vals), MAX_IN):
                    yield where[:i] + [(col, "in", vals[start:start + MAX_IN])] + where[i + 1:]
                return
        yield where

    @staticmethod
    def _value(table: str, column: str, value: Any) -> Any:
        if value is None:
            return None
        if column in POSTGRES_JSON_COLUMNS.get(table, set()):
            import json
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except ValueError:
                    return value
            return value
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, datetime) and value.tzinfo is None and (
            column.endswith("_at") or column.endswith("_date")
        ):
            return value.replace(tzinfo=timezone.utc).isoformat()
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, (uuid.UUID, Decimal)):
            return str(value) if isinstance(value, uuid.UUID) else float(value)
        if isinstance(value, str) and (column.endswith("_at") or column.endswith("_date")) and ISO_VALUE_RE.match(value):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=timezone.utc).isoformat() if parsed.tzinfo is None else parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                return value
        return value

    def select(self, table, where=None, columns=None, order=None, limit=None, offset=None):
        w = normalize_where(where)
        chunks = list(self._split_big_in(w))
        if len(chunks) > 1:
            rows = []
            for chunk in chunks:
                rows.extend(self.select(table, chunk, columns, order, None, None))
            for col, desc in reversed(normalize_order(order)):
                rows.sort(key=lambda row: (row.get(col) is None, row.get(col)), reverse=desc)
            start = offset or 0
            return rows[start:start + limit] if limit is not None else rows[start:]

        cols = normalize_columns(columns)
        params = [("select", ",".join(self._ident(col) for col in cols) if cols else "*")]
        params.extend(self._where(table, w))
        order_items = normalize_order(order)
        if order_items:
            params.append(("order", ",".join(
                f"{self._ident(col)}.{'desc' if desc else 'asc'}" for col, desc in order_items
            )))
        if limit is not None:
            params.append(("limit", str(int(limit))))
        if offset is not None:
            params.append(("offset", str(int(offset))))
        _, rows = self._request("GET", table, params)
        return [decode_row(table, row) for row in rows]

    def count(self, table, where=None):
        params = [("select", "id")] + self._where(table, normalize_where(where))
        response, _ = self._request("HEAD", table, params, prefer="count=exact")
        content_range = response.headers.get("content-range", "")
        total = content_range.rsplit("/", 1)[-1]
        if not total.isdigit():
            raise RuntimeError("Supabase count response did not include an exact total")
        return int(total)

    def _insert_one(self, table: str, row: dict) -> dict:
        data = dict(row)
        if table != "portal_settings" and not data.get("id"):
            data["id"] = str(uuid.uuid4())
        payload = {col: self._value(table, col, value) for col, value in data.items()}
        _, rows = self._request("POST", table, payload=payload, prefer="return=representation")
        return decode_row(table, rows[0]) if rows else decode_row(table, data)

    def insert(self, table, rows):
        items = [rows] if isinstance(rows, dict) else list(rows)
        return [self._insert_one(table, row) for row in items]

    def update(self, table, values, where):
        if not values:
            return 0
        payload = {col: self._value(table, col, value) for col, value in values.items()}
        total = 0
        for chunk in self._split_big_in(normalize_where(where)):
            _, rows = self._request("PATCH", table, self._where(table, chunk), payload, "return=representation")
            total += len(rows)
        return total

    def delete(self, table, where):
        total = 0
        for chunk in self._split_big_in(normalize_where(where)):
            _, rows = self._request("DELETE", table, self._where(table, chunk), prefer="return=representation")
            total += len(rows)
        return total

    def upsert(self, table, row, key):
        keys = [key] if isinstance(key, str) else list(key)
        data = dict(row)
        if table != "portal_settings" and not data.get("id"):
            data["id"] = str(uuid.uuid4())
        params = [("on_conflict", ",".join(self._ident(col) for col in keys))]
        payload = {col: self._value(table, col, value) for col, value in data.items()}
        result = self._request(
            "POST",
            table,
            params,
            payload,
            "resolution=merge-duplicates,return=representation",
        )[1]
        if result:
            return decode_row(table, result[0])
        return self.first(table, {col: data[col] for col in keys}) or decode_row(table, data)


def _match(row: dict, where: list[tuple]) -> bool:
    for col, op, val in where:
        cur = row.get(col)
        if op == "is_null":
            ok = cur is None
        elif op == "not_null":
            ok = cur is not None
        elif op == "eq":
            ok = _eq(cur, val)
        elif op == "neq":
            ok = not _eq(cur, val)
        elif op == "in":
            ok = any(_eq(cur, v) for v in (val or []))
        elif op == "not_in":
            ok = not any(_eq(cur, v) for v in (val or []))
        elif op in ("like", "ilike"):
            pattern = "^" + re.escape(str(val)).replace("%", ".*").replace("_", ".") + "$"
            ok = cur is not None and re.match(pattern, str(cur), re.I | re.S) is not None
        elif cur is None:
            ok = False
        else:
            ok = {"gt": cur > val, "gte": cur >= val, "lt": cur < val, "lte": cur <= val}[op]
        if not ok:
            return False
    return True


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    return a == b


class MemoryStore(Store):
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.lock = threading.RLock()

    def seed(self, table: str, rows: list[dict]) -> None:
        with self.lock:
            self.tables.setdefault(table, []).extend(json.loads(json.dumps(rows, default=str)))

    def _rows(self, table: str) -> list[dict]:
        return self.tables.setdefault(table, [])

    def select(self, table, where=None, columns=None, order=None, limit=None, offset=None):
        with self.lock:
            w = normalize_where(where)
            rows = [dict(r) for r in self._rows(table) if _match(r, w)]
            for col, desc in reversed(normalize_order(order)):
                rows.sort(key=lambda r: (r.get(col) is None, r.get(col) if r.get(col) is not None else 0), reverse=desc)
            start = offset or 0
            rows = rows[start:start + limit] if limit is not None else rows[start:]
            cols = normalize_columns(columns)
            if cols:
                rows = [{c: r.get(c) for c in cols} for r in rows]
            return rows

    def count(self, table, where=None):
        with self.lock:
            w = normalize_where(where)
            return sum(1 for r in self._rows(table) if _match(r, w))

    def insert(self, table, rows):
        with self.lock:
            items = [rows] if isinstance(rows, dict) else list(rows)
            out = []
            for item in items:
                data = dict(item)
                if table != "portal_settings" and not data.get("id"):
                    data["id"] = str(uuid.uuid4())
                if "created_at" not in data and table != "portal_settings":
                    data["created_at"] = datetime.now(timezone.utc).isoformat()
                self._rows(table).append(data)
                out.append(dict(data))
            return out

    def update(self, table, values, where):
        with self.lock:
            w = normalize_where(where)
            n = 0
            for row in self._rows(table):
                if _match(row, w):
                    row.update(values)
                    n += 1
            return n

    def delete(self, table, where):
        with self.lock:
            w = normalize_where(where)
            keep = [r for r in self._rows(table) if not _match(r, w)]
            removed = len(self._rows(table)) - len(keep)
            self.tables[table] = keep
            return removed

    def upsert(self, table, row, key):
        with self.lock:
            keys = [key] if isinstance(key, str) else list(key)
            cond = {k: row[k] for k in keys}
            if self.update(table, {k: v for k, v in row.items() if k not in keys}, cond) == 0:
                self.insert(table, row)
            return self.first(table, cond)

    def ping(self) -> bool:
        return True


_store: Optional[Store] = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                use_supabase = db_target() == "SUPABASE" or env("DB_PROVIDER").lower() == "supabase"
                _store = SupabaseStore() if use_supabase else MssqlStore()
    return _store


def set_store(store: Optional[Store]) -> None:
    global _store
    _store = store
