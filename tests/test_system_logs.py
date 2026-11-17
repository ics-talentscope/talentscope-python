import re
import uuid

from core.db import _database_operation
from services.system_logs import metrics, record_log, safe_payload


def test_safe_payload_keeps_only_allowlisted_operational_fields():
    assert safe_payload({
        "status": "ok",
        "count": 3,
        "email": "person@example.com",
        "password": "do-not-log",
        "candidateText": "resume contents",
    }) == {"status": "ok", "count": 3}


def test_record_log_persists_request_context_fields(store):
    request_id = str(uuid.uuid4())
    record_log({
        "module": "http",
        "action": "api_request",
        "log_level": "INFO",
        "request_id": request_id,
        "endpoint": "/api/health",
        "execution_time": 14.2,
        "status": "200",
        "message": "GET /api/health completed",
    })

    row = store.first("system_logs", {"request_id": request_id})
    assert row is not None
    assert row["endpoint"] == "/api/health"
    assert row["execution_time"] == 14.2


def test_request_middleware_returns_and_persists_a_correlation_id(client, store):
    response = client.get("/api/health")
    request_id = response.headers.get("x-request-id")

    assert response.status_code == 200
    assert request_id and re.fullmatch(r"[0-9a-f-]{36}", request_id)
    assert store.first("system_logs", {"request_id": request_id}) is not None


def test_database_operation_parser_does_not_expose_values():
    assert _database_operation("SELECT * FROM [dbo].[resumes] WHERE id = ?") == ("SELECT", "resumes")
    assert _database_operation("INSERT INTO [dbo].[system_logs] (id) VALUES (?)") == ("INSERT", "system_logs")
    assert _database_operation("UPDATE [dbo].[employees] SET role = ?") == ("UPDATE", "employees")
    assert _database_operation("DELETE FROM [dbo].[audit_logs] WHERE id = ?") == ("DELETE", "audit_logs")


def test_log_metrics_count_requests_and_slow_calls(store):
    record_log({
        "module": "http",
        "log_level": "INFO",
        "status": "200",
        "execution_time": 1200,
        "message": "request completed",
    })
    record_log({
        "module": "http",
        "log_level": "ERROR",
        "status": "500",
        "message": "request failed",
    })
    record_log({
        "module": "database",
        "log_level": "ERROR",
        "status": "failed",
        "message": "database failed",
    })

    totals = metrics()
    assert totals["totalRequests"] == 2
    assert totals["successfulRequests"] == 1
    assert totals["failedRequests"] == 1
    assert totals["slowApis"] == 1
    assert totals["databaseErrors"] == 1
