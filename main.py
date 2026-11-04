import logging
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anyio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import env, env_int, frontend_origins, mssql_settings
from core.errors import install_error_handlers
from core.logger import configure_logging, logger as structured_logger, run_log_archiver
from core.request_logging import install_request_logging
from core.security import install_traffic_controls
from core.tokens import require_secret_in_production
from routers import ROUTERS
from services import admin_accounts, employee_tests, monitor, question_queue, saml, storage, system_logs

LOG_DIRECTORY = Path(env("LOG_DIR") or Path(__file__).resolve().parent / "logs")
LOG_ENVIRONMENT = env("NODE_ENV") or "development"
configure_logging(LOG_DIRECTORY, env("LOG_LEVEL") or "INFO", LOG_ENVIRONMENT)
log = logging.getLogger("talentscope")
RETENTION_INTERVAL_S = 6 * 3600


def run_retention() -> None:
    days = env_int("RECORDING_RETENTION_DAYS", 0)
    while days > 0:
        try:
            removed = storage.prune_older_than("recordings", days)
            if removed:
                log.info("retention: removed %s recordings older than %s days", removed, days)
        except Exception as err:
            log.warning("retention sweep failed: %s", err)
        time.sleep(RETENTION_INTERVAL_S)


def run_system_log_retention() -> None:
    while True:
        try:
            removed = system_logs.prune_logs()
            if removed:
                log.info("retention: removed %s expired system log entries", removed)
        except Exception:
            log.exception("system log retention sweep failed")
        time.sleep(24 * 60 * 60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    anyio.to_thread.current_default_thread_limiter().total_tokens = env_int("API_THREADS", 24)
    try:
        if admin_accounts.ensure_console_password():
            log.info("admin console password ready")
        else:
            log.warning("admin console password not setup (set ADMIN_PASSWORD)")
    except Exception as err:
        log.warning("admin console password bootstrap failed: %s", err)
    threading.Thread(target=run_retention, daemon=True, name="retention").start()
    threading.Thread(target=run_system_log_retention, daemon=True, name="system-log-retention").start()
    threading.Thread(
        target=run_log_archiver,
        args=(LOG_DIRECTORY, LOG_ENVIRONMENT),
        daemon=True,
        name="log-archiver",
    ).start()
    question_queue.start_workers()
    monitor.start_db_sampler()
    threading.Thread(target=saml.get_config, daemon=True, name="saml-metadata").start()
    employee_tests.start_expiry_sweeper()
    structured_logger.bind(module="application", action="application_started").info("TalentScope API is ready")
    yield
    question_queue.stop_workers()


require_secret_in_production()
if env("DB_PROVIDER").lower() == "supabase":
    log.info("database: supabase %s", env("NEXT_PUBLIC_SUPABASE_URL"))
else:
    _db = mssql_settings()
    log.info("database: %s %s:%s/%s", _db["target"], _db["server"], _db["port"], _db["database"])

docs_enabled = env("ENABLE_API_DOCS") == "1"
app = FastAPI(title="TalentScope API", version="1.0.0", lifespan=lifespan,
              docs_url="/docs" if docs_enabled else None, redoc_url=None, openapi_url="/openapi.json" if docs_enabled else None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
install_error_handlers(app)
install_traffic_controls(app)
install_request_logging(app)


for router in ROUTERS:
    app.include_router(router)
