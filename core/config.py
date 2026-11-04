import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

# In local dev, ROOT is the repo root (two levels above python/core/).
# In Docker, the app lives at /app/ so parents[2] resolves to / — fall back
# to the directory that contains main.py (one level up from core/).
_env_candidates = [ROOT / ".env.local", Path(__file__).resolve().parents[1] / ".env.local"]
for _candidate in _env_candidates:
    if _candidate.is_file():
        load_dotenv(_candidate, override=False)
        break

_default_uploads = ROOT / "python" / "recordings"
if not _default_uploads.parent.is_dir():
    _default_uploads = Path(__file__).resolve().parents[1] / "recordings"
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or str(_default_uploads)).resolve()


def env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def app_url() -> str:
    return (env("NEXT_PUBLIC_APP_URL") or "http://localhost:3000").rstrip("/")


def frontend_origins() -> list[str]:
    raw = env("FRONTEND_ORIGIN") or app_url()
    return [item.strip() for item in raw.split(",") if item.strip()]


def db_target() -> str:
    target = env("DB_TARGET", "MSSQL").upper()
    if target not in {"MSSQL", "SUPABASE"}:
        raise ValueError("DB_TARGET must be MSSQL or SUPABASE")
    return target


def mssql_settings() -> dict:
    target = (env("MSSQL_TARGET") or "local").lower()
    prefix = f"MSSQL_{target.upper()}_"
    return {
        "target": target,
        "server": env(prefix + "SERVER", "localhost"),
        "port": env_int(prefix + "PORT", 1433),
        "database": env(prefix + "DATABASE", "Talentscope"),
        "user": env(prefix + "USER"),
        "password": env(prefix + "PASSWORD"),
        "encrypt": env_bool(prefix + "ENCRYPT", True),
        "trust_cert": env_bool(prefix + "TRUST_SERVER_CERTIFICATE", target == "local"),
        "driver": env(prefix + "ODBC_DRIVER") or env("MSSQL_ODBC_DRIVER") or "ODBC Driver 18 for SQL Server",
        "pool_max": env_int("MSSQL_POOL_MAX", 10),
        "pool_wait_s": env_int("MSSQL_POOL_WAIT_S", 20),
    }


def supabase_settings() -> dict:
    return {
        "url": (env("SUPABASE_URL") or env("NEXT_PUBLIC_SUPABASE_URL")).rstrip("/"),
        "service_role_key": env("SUPABASE_SERVICE_ROLE_KEY"),
        "timeout_s": env_int("SUPABASE_API_TIMEOUT_S", 20),
    }


def active_profile() -> str:
    return env("APP_PROFILE", "local").lower()


def _expand_property(value: str) -> str:
    return re.sub(r"\$\{([A-Za-z0-9_]+)\}", lambda match: os.environ.get(match.group(1), ""), value)


def _profile_properties(profile: str) -> dict:
    path = Path(__file__).resolve().parents[1] / "conf" / "application.properties"
    if not path.is_file():
        return {}
    prefix = f"database.{profile}."
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith(prefix):
            values[key[len(prefix):]] = _expand_property(value.strip())
    return values


def postgres_settings() -> dict:
    props = _profile_properties("vercel")
    port = props.get("port") or env("SUPABASE_DB_PORT", "5432")
    try:
        port_num = int(port)
    except ValueError:
        port_num = 5432
    return {
        "host": props.get("host") or env("SUPABASE_DB_HOST"),
        "port": port_num,
        "database": props.get("database") or env("SUPABASE_DB_NAME", "postgres"),
        "user": props.get("user") or env("SUPABASE_DB_USER", "postgres"),
        "password": props.get("password") or env("SUPABASE_DB_PASSWORD"),
        "sslmode": props.get("sslmode") or "require",
    }


def supabase_db_settings() -> dict:
    return postgres_settings()
