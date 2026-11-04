import logging
import re
import traceback
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("talentscope")

LEAK_RE = re.compile(
    r"at\s+\S+\s+\(|[A-Z]:\\|/home/|/var/|/tmp/|site-packages|pyodbc|odbc driver|sqlstate|permission denied|"
    r"ECONNREFUSED|ENOTFOUND|ETIMEDOUT|traceback|\[Microsoft\]",
    re.I,
)


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def is_safe_client_message(message: str) -> bool:
    msg = (message or "").strip()
    if not msg or len(msg) > 320:
        return False
    if LEAK_RE.search(msg):
        return False
    return not ("\n" in msg and len(msg) > 160)


def public_error_message(error: Any, fallback: str) -> str:
    msg = str(error) if isinstance(error, (Exception, str)) else ""
    return msg if is_safe_client_message(msg) else fallback


def error_body(message: str, **extra: Any) -> dict:
    return {**extra, "error": message, "message": message}


def json_error(http_status: int, message: str, headers: dict | None = None, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=http_status, content=error_body(message, **extra), headers=headers)


def public_error(error: Any, fallback: str, http_status: int = 500, **extra: Any) -> JSONResponse:
    log.error("api error: %s", error)
    return json_error(http_status, public_error_message(error, fallback), **extra)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return json_error(exc.status, exc.message, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return json_error(exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, __: RequestValidationError):
        return json_error(400, "Invalid request")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        request.state.error_message = f"{type(exc).__name__}: {exc}"[:1000]
        request.state.stack_trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        log.exception("unhandled error: %s", exc)
        return json_error(500, "Internal server error")
