import hashlib
import hmac
import logging
import re
import time
from typing import Optional

from core import tenancy
from core.config import env
from services import settings
from services.accounts import hash_password, verify_password

log = logging.getLogger("talentscope")

# One super admin per client environment. Both live in the same app and DB;
# their settings, named admins and data are scoped by client_id.
DEFAULT_SUPER_ADMINS = {
    "infinite": "admin@infinite.com",
    "slalom": "admin@slalom.com",
}
# Legacy Infinite super-admin identities preserved for existing deployments/tests.
LEGACY_SUPER_ADMINS = {
    "infinite": {"boss@infinite.com"},
    "slalom": set(),
}
SUPER_ADMIN_ENVS = {
    "infinite": "ADMIN_EMAIL",
    "slalom": "SLALOM_ADMIN_EMAIL",
}
ADMIN_PASSWORD_ENVS = {
    "infinite": "ADMIN_PASSWORD",
    "slalom": "SLALOM_ADMIN_PASSWORD",
}
SUPERVISOR_PASSWORD_ENVS = {
    "infinite": "SUPERVISOR_PASSWORD",
    "slalom": "SLALOM_SUPERVISOR_PASSWORD",
}
SCREENING_ONLY_ADMIN_EMAILS = {"ramendras@infinite.com", "nehalathar@infinite.com", "shwethab@infinite.com"}
PERMISSIONS = (
    ("requirements", "Requirements (BR / JD) tab"),
    ("corpPool", "Corp Pool tab"),
    ("candidates", "Suitable & Non-Suitable Candidates tabs"),
    ("employeePortal", "Employee Portal tab (employee tests, scores, videos)"),
    ("emailOutbox", "Email Outbox tab"),
    ("allScreeningData", "Everyone's requirements, candidates and emails (untick = only their own)"),
    ("adminControls", "Admin Controls (ingestion, uploads, match scores, reset candidate)"),
    ("systemLogs", "System Logs viewer"),
    ("systemMonitor", "System Monitor"),
    ("portalConfig", "Portal Config (feature toggles & manage who has admin access)"),
    ("bootcampManagement", "Slalom Bootcamp Management (assessments, cohorts and leaderboards)"),
    ("changePassword", "Change their own password"),
)
PERMISSION_KEYS = tuple(key for key, _ in PERMISSIONS)
ACCOUNTS_KEY = "admin_named_accounts"
LEGACY_KEY = "admin_named_passwords"
CONSOLE_PASSWORD_KEY = "admin_console_password"

_cache: dict[str, tuple[float, dict]] = {}

ADMIN_PASSWORD_NOT_SETUP = "Admin password not setup"


class AdminPasswordNotSetup(Exception):
    pass


def is_admin_email(email: str) -> bool:
    return tenancy.is_admin_email(email)


def client_for_admin(email: str) -> str:
    return tenancy.client_for_email(email) or tenancy.current_client()


def console_secret(client: Optional[str] = None) -> Optional[dict]:
    key = tenancy.normalize_client(client or tenancy.current_client())
    try:
        with tenancy.client_context(key):
            raw = settings.get_setting(CONSOLE_PASSWORD_KEY)
    except Exception as err:
        log.warning("Failed to load the admin console password: %s", err)
        return None
    return raw if isinstance(raw, dict) and _secret_ok(raw) else None


def set_console_password(password: str, client: Optional[str] = None) -> None:
    key = tenancy.normalize_client(client or tenancy.current_client())
    hashed, salt = hash_password(password)
    with tenancy.client_context(key):
        settings.set_setting(CONSOLE_PASSWORD_KEY, {"hash": hashed, "salt": salt})
    invalidate_cache()


def ensure_console_password(client: Optional[str] = None) -> Optional[dict]:
    """Seed the hashed console password from the client's env var when absent."""
    key = tenancy.normalize_client(client or tenancy.current_client())
    stored = console_secret(key)
    if stored:
        return stored
    bootstrap = env(ADMIN_PASSWORD_ENVS[key])
    if not bootstrap:
        return None
    try:
        set_console_password(bootstrap, key)
    except Exception as err:
        log.warning("Failed to seed admin console password from %s: %s", ADMIN_PASSWORD_ENVS[key], err)
        return None
    return console_secret(key)


def _screening_password() -> str:
    value = env("SCREENING_ADMIN_PASSWORD")
    if not value:
        raise AdminPasswordNotSetup(ADMIN_PASSWORD_NOT_SETUP)
    return value


def _supervisor_password() -> str:
    client = tenancy.current_client()
    value = env(SUPERVISOR_PASSWORD_ENVS.get(client, "SUPERVISOR_PASSWORD"))
    if client == "slalom" and not value:
        value = env("SUPERVISOR_PASSWORD")
    if not value:
        raise AdminPasswordNotSetup("Supervisor password not setup in .env.local")
    return value


def verify_supervisor(password: str) -> bool:
    return bool(password and password == _supervisor_password())


def clean_email(email: str) -> str:
    return str(email or "").strip().lower()


def is_screening_only(email: str) -> bool:
    user = clean_email(email)
    return tenancy.client_for_email(user) == "infinite" and user in SCREENING_ONLY_ADMIN_EMAILS


def _secret_ok(stored: Optional[dict]) -> bool:
    return bool(stored and stored.get("hash") and stored.get("salt"))


def _matches(password: str, stored: dict) -> bool:
    try:
        return verify_password(password, stored["salt"], stored["hash"])
    except Exception:
        return False


def _clean_aliases(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        alias = clean_email(item)
        if not tenancy.is_admin_email(alias) or alias in seen:
            continue
        seen.add(alias)
        out.append(alias)
    return out


def _normalize(raw: dict) -> tuple[dict, bool]:
    plaintext = str((raw or {}).get("password") or "").strip()
    hashed = hash_password(plaintext) if plaintext else None
    ok = _secret_ok(raw)
    record = {
        "hash": raw.get("hash") if ok else (hashed[0] if hashed else None),
        "salt": raw.get("salt") if ok else (hashed[1] if hashed else None),
        "canViewEmployeePortal": raw.get("canViewEmployeePortal") is not False,
        "canChangePassword": raw.get("canChangePassword") is not False,
        "canViewOrgScreeningData": raw.get("canViewOrgScreeningData") is not False,
        "aliases": _clean_aliases(raw.get("aliases")),
    }
    if isinstance(raw.get("permissions"), dict):
        record["permissions"] = {k: bool(v) for k, v in raw["permissions"].items() if k in PERMISSION_KEYS}
    return record, bool(plaintext)


def _parse(value) -> tuple[dict, bool]:
    if not isinstance(value, dict):
        return {}, False
    accounts, stripped = {}, False
    for email, raw in value.items():
        user = clean_email(email)
        if not tenancy.is_admin_email(user) or not isinstance(raw, dict):
            continue
        record, had_plain = _normalize(raw)
        accounts[user] = record
        stripped = stripped or had_plain
    return accounts, stripped


def _read_map(key: str) -> dict:
    try:
        value = settings.get_setting(key)
        return value if isinstance(value, dict) else {}
    except Exception as err:
        log.warning("Failed to load %s: %s", key, err)
        return {}


def invalidate_cache() -> None:
    _cache.clear()


def load_accounts(client: Optional[str] = None) -> dict:
    key = tenancy.normalize_client(client or tenancy.current_client())
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 4:
        return hit[1]
    with tenancy.client_context(key):
        accounts, dirty = _parse(_read_map(ACCOUNTS_KEY))
        for email, secret in _read_map(LEGACY_KEY).items():
            user = clean_email(email)
            if user not in accounts or not _secret_ok(secret) or _secret_ok(accounts[user]):
                continue
            accounts[user] = {**accounts[user], "hash": secret["hash"], "salt": secret["salt"]}
            dirty = True
        if key == "infinite":
            for email in SCREENING_ONLY_ADMIN_EMAILS:
                if accounts.get(email, {}).get("canViewEmployeePortal"):
                    accounts[email] = {**accounts[email], "canViewEmployeePortal": False}
                    dirty = True
        if dirty:
            save_accounts(accounts, key)
        else:
            _cache[key] = (time.time(), accounts)
    return accounts


def save_accounts(accounts: dict, client: Optional[str] = None) -> None:
    key = tenancy.normalize_client(client or tenancy.current_client())
    _cache[key] = (time.time(), accounts)
    try:
        with tenancy.client_context(key):
            settings.set_setting(ACCOUNTS_KEY, accounts)
    except Exception as err:
        log.warning("Failed to save named admin accounts: %s", err)


def get_named_record(email: str) -> Optional[dict]:
    return load_accounts(client_for_admin(email)).get(clean_email(email))


def find_named_by_alias(email: str) -> Optional[dict]:
    """Return named-account record matched by aliases[], with canonical `_email` set."""
    user = clean_email(email)
    if not user:
        return None
    for account_email, record in load_accounts(client_for_admin(user)).items():
        aliases = record.get("aliases") if isinstance(record.get("aliases"), list) else []
        if user == account_email or user in aliases:
            return {**record, "_email": account_email}
    return None


def all_permissions(value: bool) -> dict:
    return {key: value for key in PERMISSION_KEYS}


def super_admin_email(client: Optional[str] = None) -> str:
    key = tenancy.normalize_client(client or tenancy.current_client())
    return clean_email(env(SUPER_ADMIN_ENVS[key])) or DEFAULT_SUPER_ADMINS[key]


def super_admin_emails(client: Optional[str] = None) -> set:
    """All identities treated as the client's super admin.

    The configured env email wins; when unset, the default plus legacy
    Infinite identities are accepted so existing deployments keep working.
    """
    key = tenancy.normalize_client(client or tenancy.current_client())
    configured = clean_email(env(SUPER_ADMIN_ENVS[key]))
    emails = {configured or DEFAULT_SUPER_ADMINS[key]}
    if not configured:
        emails |= set(LEGACY_SUPER_ADMINS.get(key, set()))
    return emails


def is_super_admin(email: str) -> bool:
    user = clean_email(email)
    client = tenancy.client_for_email(user)
    return bool(client) and user in super_admin_emails(client)


def default_admin_email(client: Optional[str] = None) -> str:
    """Fallback RM/actor identity for the active client environment."""
    return super_admin_email(client)


def _record_permissions(record: dict) -> dict:
    stored = record.get("permissions") if isinstance(record.get("permissions"), dict) else {}
    # Fail closed: start denied, then apply explicit ticks (or legacy flags when no permissions map).
    out = all_permissions(False)
    if stored:
        for key, value in stored.items():
            if key in PERMISSION_KEYS and isinstance(value, bool):
                out[key] = value
    else:
        out["employeePortal"] = bool(record.get("canViewEmployeePortal", False))
        out["changePassword"] = bool(record.get("canChangePassword", False))
        out["allScreeningData"] = bool(record.get("canViewOrgScreeningData", False))
    return out


def _access(client: str, email: str, super_admin: bool, permissions: dict) -> dict:
    return {
        "email": email, "client": client, "isSuperAdmin": super_admin, "permissions": permissions,
        "canViewEmployeePortal": permissions["employeePortal"], "canChangePassword": permissions["changePassword"],
    }


def get_access(email: str) -> dict:
    user = clean_email(email)
    client = tenancy.client_for_email(user) or tenancy.current_client()
    denied = _access(client, user, False, all_permissions(False))
    with tenancy.client_context(client):
        if user in super_admin_emails(client):
            return _access(client, user, True, {**all_permissions(True), "changePassword": False})
        named = get_named_record(user) or find_named_by_alias(user)
        if named:
            canonical = named.get("_email") or user
            perms = _record_permissions(named)
            if not _secret_ok(named):
                perms = {**perms, "changePassword": False}
            return _access(client, canonical, False, perms)
        if is_screening_only(user):
            # Password configuration is checked during authentication; access
            # mapping itself must work for SSO/impersonated filtered views too.
            return _access(client, user, False, {**all_permissions(True), "employeePortal": False, "changePassword": False})
    return denied


def has_permission(email: str, permission: str) -> bool:
    return bool(get_access(email)["permissions"].get(permission))


def can_change_password(email: str) -> bool:
    named = get_named_record(email) or find_named_by_alias(email)
    # SSO-only accounts have no password to change from the UI.
    return bool(named and _secret_ok(named) and _record_permissions(named)["changePassword"])


def can_view_org_screening_data(email: str) -> bool:
    return has_permission(email, "allScreeningData")


def can_manage_portal_config(email: str) -> bool:
    return is_super_admin(email) or has_permission(email, "portalConfig")


def can_manage_slalom_bootcamp(email: str) -> bool:
    """Slalom-only bootcamp administration gate.

    The permission alone is not enough: Infinite admins must never be able to
    read or manage the Slalom bootcamp environment, even if a legacy permission
    map happens to contain the key. Slalom's super admin already receives every
    permission, so this also covers them without a special case list.
    """
    user = clean_email(email)
    if tenancy.client_for_email(user) != "slalom":
        return False
    return is_super_admin(user) or has_permission(user, "bootcampManagement")


def _env_match(password: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(expected.encode()).digest())


def authenticate(email: str, password: str) -> Optional[dict]:
    user = clean_email(email)
    client = tenancy.client_for_email(user)
    if not client or not password:
        return None
    with tenancy.client_context(client):
        if user in super_admin_emails(client):
            stored = ensure_console_password(client)
            if not stored:
                raise AdminPasswordNotSetup(ADMIN_PASSWORD_NOT_SETUP)
            return get_access(user) if _matches(password, stored) else None
        named = get_named_record(user)
        if named and _secret_ok(named) and _matches(password, named):
            return get_access(user)
        if is_screening_only(user) and _env_match(password, _screening_password()):
            return get_access(user)
    return None


def list_named(client: Optional[str] = None) -> list[dict]:
    key = tenancy.normalize_client(client or tenancy.current_client())
    with tenancy.client_context(key):
        return sorted(({"email": email, "hasPassword": _secret_ok(record), "permissions": _record_permissions(record),
                        "aliases": list(record.get("aliases") or [])}
                       for email, record in load_accounts(key).items()), key=lambda item: item["email"])


def save_named(email: str, permissions: dict, password: Optional[str], aliases: Optional[list] = None) -> tuple[bool, str, bool]:
    user = clean_email(email)
    client = tenancy.client_for_email(user)
    if not client:
        return False, "Use an @infinite.com or @slalom.com email address.", False
    with tenancy.client_context(client):
        if not re.fullmatch(r"[^\s@]+@(?:infinite|slalom)\.com", user):
            return False, "Use an @infinite.com or @slalom.com email address.", False
        if user in super_admin_emails(client):
            return False, "The main admin account always has full access.", False
        password = str(password or "")
        if password and len(password) < 8:
            return False, "The password must be at least 8 characters.", False
        accounts = dict(load_accounts(client))
        existing = accounts.get(user)
        cleaned_aliases = _clean_aliases(aliases if aliases is not None else (existing or {}).get("aliases"))
        cleaned_aliases = [a for a in cleaned_aliases if a != user and tenancy.client_for_email(a) == client]
        for alias in cleaned_aliases:
            if alias == super_admin_email(client) or alias in accounts:
                return False, f"Alias {alias} is already an admin account email. Use a different alias.", False
            for other_email, other in accounts.items():
                if other_email == user:
                    continue
                if alias in (other.get("aliases") or []):
                    return False, f"Alias {alias} is already linked to {other_email}.", False
        # Password is optional: SSO-only admins need only an Admin Panel Access row.
        merged = {key: (permissions or {}).get(key) is True for key in PERMISSION_KEYS}
        if password:
            hashed, salt = hash_password(password)
        else:
            hashed = (existing or {}).get("hash")
            salt = (existing or {}).get("salt")
        accounts[user] = {"hash": hashed, "salt": salt, "permissions": merged, "aliases": cleaned_aliases,
                          "canViewEmployeePortal": merged["employeePortal"],
                          "canChangePassword": merged["changePassword"], "canViewOrgScreeningData": merged["allScreeningData"]}
        save_accounts(accounts, client)
    return True, "", existing is None


def remove_named(email: str) -> bool:
    user = clean_email(email)
    client = tenancy.client_for_email(user)
    if not client:
        return False
    with tenancy.client_context(client):
        accounts = dict(load_accounts(client))
        if user not in accounts:
            return False
        del accounts[user]
        save_accounts(accounts, client)
    return True


def change_password(email: str, current_password: str, new_password: str) -> tuple[bool, str]:
    user = clean_email(email)
    client = tenancy.client_for_email(user)
    if not client:
        return False, "This account cannot change its password here."
    with tenancy.client_context(client):
        named = get_named_record(user)
        if not named or not named.get("canChangePassword"):
            return False, "This account cannot change its password here."
        if len(str(new_password or "")) < 8:
            return False, "New password must be at least 8 characters."
        if not _secret_ok(named):
            return False, "This account signs in with Microsoft only. Ask Portal Config to set a backup password."
        if not authenticate(user, current_password):
            return False, "Current password is incorrect."
        accounts = load_accounts(client)
        current = accounts.get(user)
        if not current:
            return False, "This account cannot change its password here."
        hashed, salt = hash_password(new_password)
        accounts[user] = {**current, "hash": hashed, "salt": salt}
        save_accounts(accounts, client)
    return True, ""
