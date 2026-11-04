import json
import re
import time
import traceback
import uuid

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from core import request_context
from core.logger import logger
from core.security import get_client_ip
from core.tokens import token_claims
from services.system_logs import record_log, safe_payload

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SENSITIVE_PATH_RE = re.compile(
    r"/(?:auth|login|logout|password|reset|resume|upload|interview|assessment|test|candidate|employee|ai|llm)(?:/|$)",
    re.I,
)
BODY_LIMIT = 32 * 1024


def _identity(request: Request) -> tuple[str | None, str | None]:
    header = request.headers.get("authorization") or ""
    token = header[7:] if header.lower().startswith("bearer ") else header
    claims = token_claims(token) if token else None
    if not claims:
        return None, None
    email = claims.get("email")
    role = claims.get("role") or claims.get("user_role")
    if claims.get("employee_id") == "ADMIN":
        role = role or "admin"
    elif claims.get("employee_id"):
        role = role or "employee"
    return (str(email)[:320] if email else None, str(role)[:80] if role else None)


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        request_id = str(uuid.uuid4())
        user_email, user_role = _identity(request)
        client_ip = get_client_ip(request)
        request.state.request_id = request_id
        request.state.user_email = user_email
        request.state.user_role = user_role
        request.state.client_ip = client_ip
        request_id_token = request_context.set_request_id(request_id)

        started = time.perf_counter()
        sensitive_path = bool(SENSITIVE_PATH_RE.search(request.url.path))
        capture_body = not sensitive_path and request.headers.get("content-type", "").lower().startswith("application/json")
        declared_length = request.headers.get("content-length", "")
        if not declared_length.isdigit() or int(declared_length) > BODY_LIMIT:
            capture_body = False
        request_chunks: list[bytes] = []
        request_size = 0
        response_chunks: list[bytes] = []
        response_size = 0
        response_is_json = False
        status_code = 500
        error_message = None
        error_stack = None
        if request.url.path.lower().endswith("/login"):
            await run_in_threadpool(record_log, {
                "module": "auth",
                "action": "login_attempt",
                "log_level": "INFO",
                "status": "attempted",
                "ip_address": client_ip,
                "user_agent": request.headers.get("user-agent", "")[:512],
                "request_id": request_id,
                "endpoint": request.url.path[:512],
                "message": f"Login attempt at {request.url.path}",
            })

        async def receive_wrapper():
            nonlocal request_size, capture_body
            message = await receive()
            if capture_body and message["type"] == "http.request":
                chunk = message.get("body", b"")
                request_size += len(chunk)
                if request_size <= BODY_LIMIT:
                    request_chunks.append(chunk)
                else:
                    request_chunks.clear()
                    capture_body = False
            return message

        async def send_wrapper(message):
            nonlocal status_code, response_is_json, response_size
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
                response_is_json = any(
                    key.lower() == b"content-type" and b"application/json" in value.lower()
                    for key, value in headers
                )
            elif (
                message["type"] == "http.response.body"
                and response_is_json
                and not sensitive_path
            ):
                chunk = message.get("body", b"")
                response_size += len(chunk)
                if response_size <= BODY_LIMIT:
                    response_chunks.append(chunk)
                else:
                    response_chunks.clear()
                    response_is_json = False
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"[:1000]
            error_stack = traceback.format_exc()
            logger.bind(
                module="http",
                action="request_failed",
                request_id=request_id,
                endpoint=request.url.path,
                user_email=user_email,
                user_role=user_role,
                ip_address=client_ip,
            ).exception("Unhandled request exception")
            raise
        finally:
            error_message = error_message or getattr(request.state, "error_message", None)
            error_stack = error_stack or getattr(request.state, "stack_trace", None)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            level = "INFO"
            if status_code >= 500 or elapsed_ms > 3000:
                level = "CRITICAL"
            elif status_code >= 400 or elapsed_ms > 1000:
                level = "WARNING"
            request_payload = None
            response_payload = None
            if request_chunks:
                try:
                    request_payload = safe_payload(json.loads(b"".join(request_chunks)))
                except (ValueError, UnicodeDecodeError):
                    pass
            if response_chunks:
                try:
                    response_payload = safe_payload(json.loads(b"".join(response_chunks)))
                except (ValueError, UnicodeDecodeError):
                    pass

            entry = {
                "module": "http",
                "action": "api_request",
                "log_level": level,
                "user_email": user_email,
                "user_role": user_role,
                "ip_address": client_ip,
                "user_agent": request.headers.get("user-agent", "")[:512],
                "request_id": request_id,
                "endpoint": request.url.path[:512],
                "execution_time": elapsed_ms,
                "status": str(status_code),
                "message": f"{request.method} {request.url.path} completed with {status_code}",
                "error_message": error_message,
                "stack_trace": error_stack,
                "request_payload": request_payload,
                "response_payload": response_payload,
            }
            logger.bind(**entry).log(level, entry["message"])
            try:
                await run_in_threadpool(record_log, entry)
                path_lower = request.url.path.lower()
                if path_lower.endswith("/login"):
                    outcome = "login_failure" if status_code >= 400 else "login_success"
                    await run_in_threadpool(record_log, {
                        "module": "auth",
                        "action": outcome,
                        "log_level": "WARNING" if status_code >= 400 else "INFO",
                        "status": "failed" if status_code >= 400 else "success",
                        "user_email": user_email,
                        "user_role": user_role,
                        "ip_address": client_ip,
                        "user_agent": request.headers.get("user-agent", "")[:512],
                        "request_id": request_id,
                        "endpoint": request.url.path[:512],
                        "message": f"{outcome.replace('_', ' ')} at {request.url.path}",
                    })
                elif path_lower.endswith("/logout"):
                    await run_in_threadpool(record_log, {
                        "module": "auth",
                        "action": "logout",
                        "log_level": "INFO",
                        "status": "success",
                        "user_email": user_email,
                        "user_role": user_role,
                        "ip_address": client_ip,
                        "request_id": request_id,
                        "endpoint": request.url.path[:512],
                        "message": "User logged out",
                    })
                elif status_code == 401 and request.headers.get("authorization"):
                    await run_in_threadpool(record_log, {
                        "module": "security",
                        "action": "session_expired_or_invalid",
                        "log_level": "WARNING",
                        "status": "failed",
                        "user_email": user_email,
                        "ip_address": client_ip,
                        "request_id": request_id,
                        "endpoint": request.url.path[:512],
                        "message": "An expired or invalid session was rejected",
                    })
                if status_code in (401, 403, 429):
                    security_action = "request_throttled" if status_code == 429 else "unauthorized_access"
                    await run_in_threadpool(record_log, {
                        "module": "security",
                        "action": security_action,
                        "log_level": "WARNING",
                        "user_email": user_email,
                        "user_role": user_role,
                        "ip_address": client_ip,
                        "user_agent": request.headers.get("user-agent", "")[:512],
                        "request_id": request_id,
                        "endpoint": request.url.path[:512],
                        "status": "failed",
                        "message": f"{security_action.replace('_', ' ')}: {request.method} {request.url.path}",
                    })
            finally:
                request_context.reset_request_id(request_id_token)


def install_request_logging(app) -> None:
    app.add_middleware(RequestLoggingMiddleware)
