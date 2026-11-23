"""Client (tenant) resolution and request-scoped isolation.

TalentScope runs multiple client environments in the same application and the
same database. Every client-owned row carries a ``client_id`` and every query
is scoped to the client resolved from the signed bearer token. This module is
the single source of truth for that client identity.
"""

import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

DEFAULT_CLIENT = "infinite"
CLIENTS = {
    "infinite": "Infinite",
    "slalom": "Slalom",
}
CLIENT_BY_DOMAIN = {
    "infinite.com": "infinite",
    "slalom.com": "slalom",
}
ADMIN_DOMAINS = tuple(CLIENT_BY_DOMAIN)

_client: ContextVar[str] = ContextVar("talentscope_client", default=DEFAULT_CLIENT)
_unscoped: ContextVar[bool] = ContextVar("talentscope_unscoped", default=False)


def normalize_client(value: Optional[str]) -> str:
    """Return a canonical client id, falling back to the legacy default."""
    key = str(value or "").strip().lower()
    return key if key in CLIENTS else DEFAULT_CLIENT


def client_for_email(email: Optional[str]) -> Optional[str]:
    """Resolve a client from an email domain, or None for unknown domains."""
    clean = str(email or "").strip().lower()
    if "@" not in clean:
        return None
    return CLIENT_BY_DOMAIN.get(clean.rsplit("@", 1)[1])


def is_admin_email(email: Optional[str]) -> bool:
    return client_for_email(email) is not None


def current_client() -> str:
    """Client used for writes and for explicitly client-aware lookups."""
    return normalize_client(_client.get())


def client_filter() -> Optional[str]:
    """Client used to scope reads/writes; None means scoping is disabled."""
    if _unscoped.get():
        return None
    return current_client()


def set_client(value: Optional[str]):
    return _client.set(normalize_client(value))


def reset_client(token) -> None:
    _client.reset(token)


@contextmanager
def client_context(value: Optional[str]):
    """Run a block against one client environment (used by jobs/bootstrap)."""
    token = set_client(value)
    try:
        yield
    finally:
        reset_client(token)


@contextmanager
def unscoped():
    """Run a block without automatic client filtering (admin bootstrap only).

    Writes still stamp rows with ``current_client()`` unless the row carries an
    explicit ``client_id``. Never expose this to request data.
    """
    token = _unscoped.set(True)
    try:
        yield
    finally:
        _unscoped.reset(token)


def client_from_claims(claims: Optional[dict]) -> str:
    """Derive the request client from a signed token's claims."""
    if not claims:
        return DEFAULT_CLIENT
    explicit = normalize_client(claims.get("client")) if claims.get("client") in CLIENTS else None
    return explicit or client_for_email(claims.get("email")) or DEFAULT_CLIENT


def sql_filter(alias: Optional[str] = None, prefix: str = "AND") -> tuple[str, tuple[str, ...]]:
    """Return a parameterized client predicate for explicit raw SQL.

    The structured store API adds this automatically. Raw SQL must call this
    helper and append the returned fragment/params itself. ``unscoped()`` jobs
    intentionally return an empty fragment so they can process every client.
    """
    client = client_filter()
    if not client:
        return "", ()
    if prefix not in ("AND", "WHERE"):
        raise ValueError("Invalid SQL prefix")
    column = f"{alias}.client_id" if alias else "client_id"
    return f" {prefix} {column} = ?", (client,)


def client_root(base) -> Path:
    """Return the legacy root for Infinite and a dedicated subroot for others."""
    client = current_client()
    return Path(base) if client == DEFAULT_CLIENT else Path(base) / "_clients" / client
