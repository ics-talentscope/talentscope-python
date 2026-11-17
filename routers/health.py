import time
from datetime import datetime, timezone

from fastapi import APIRouter

import httpx

from core.config import db_target, env, mssql_settings
from core.db import get_store
from services import face, llm

router = APIRouter(prefix="/api", tags=["health"])
STARTED = time.time()


def supabase_up() -> bool:
    url = env("NEXT_PUBLIC_SUPABASE_URL").rstrip("/")
    key = env("SUPABASE_SERVICE_ROLE_KEY") or env("NEXT_PUBLIC_SUPABASE_ANON_KEY")
    if not url or not key:
        return False
    try:
        response = httpx.get(
            f"{url}/rest/v1/",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=5,
        )
        return response.status_code < 500
    except Exception:
        return False


def database_report() -> dict:
    if db_target() == "SUPABASE":
        url = (env("SUPABASE_URL") or env("NEXT_PUBLIC_SUPABASE_URL")).rstrip("/")
        return {"provider": "supabase", "url": url, "up": get_store().ping()}
    settings = mssql_settings()
    return {
        "provider": "mssql",
        "target": settings["target"],
        "server": settings["server"],
        "port": settings["port"],
        "database": settings["database"],
        "up": get_store().ping(),
    }


@router.get("/health")
def health():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": time.time() - STARTED,
        "service": "talentscope-api",
        "database": database_report(),
        "localLlm": {"url": llm.base_url(), "up": llm.is_up(), "model": llm.model_name()},
        "faceMatch": {"url": face.base_url(), "up": face.is_up()},
    }
