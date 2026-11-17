import logging
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Optional

from core.config import env, env_int
from core.db import get_store

log = logging.getLogger("talentscope")


def configured() -> bool:
    # Authenticated mailboxes need a user and password.
    # The company relay (webmail.infinite.com:25) accepts the sender address with no password.
    if env("SMTP_HOST") and (env("SMTP_USER") or env("SMTP_FROM")):
        return True
    return bool(env("SMTP_USER") and env("SMTP_PASS"))


def _from_address() -> str:
    return env("SMTP_FROM") or (f'"TalentScope" <{env("SMTP_USER")}>' if env("SMTP_USER") else "")


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if env("SMTP_SSL_VERIFY").lower() in ("0", "false", "off", "no"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def send(to: str, subject: str, html: str) -> None:
    host = env("SMTP_HOST") or "smtp.gmail.com"
    port = env_int("SMTP_PORT", 465)
    password = env("SMTP_PASS")
    use_ssl = env("SMTP_USE_SSL").lower() not in ("0", "false", "off", "no") if env("SMTP_USE_SSL") else (port == 465)
    message = EmailMessage()
    message["From"] = _from_address()
    message["To"] = to
    message["Subject"] = subject
    message.set_content("This message requires an HTML capable mail client.")
    message.add_alternative(html, subtype="html")
    context = _ssl_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            if password:
                server.login(env("SMTP_USER"), password)
            server.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        # Port 25 is the internal relay: no login, and TLS only if the server offers it.
        want_tls = env_int("SMTP_STARTTLS", 0) or (port != 25)
        if want_tls and server.has_extn("starttls"):
            server.starttls(context=context)
            server.ehlo()
        if password:
            server.login(env("SMTP_USER"), password)
        server.send_message(message)


def dispatch(to: str, subject: str, html: str) -> tuple[str, Optional[str]]:
    if not configured():
        log.warning("SMTP not configured; recording invite as simulated for %s", to)
        return "simulated", "SMTP is not configured (set SMTP_HOST and SMTP_FROM/SMTP_USER)."
    try:
        send(to, subject, html)
        return "sent", None
    except Exception as err:
        log.error("SMTP delivery failed, recording as simulated: %s", err)
        return "failed_real_fallback_simulated", str(err) or "SMTP delivery failed"


def record(to: str, full_name: str, subject: str, html: str, status: str, rm_email: str) -> dict:
    row = {"id": str(uuid.uuid4()), "candidate_email": to, "full_name": full_name, "subject": subject,
           "body": html, "status": status, "rm_email": rm_email}
    try:
        get_store().insert("simulated_emails", row)
    except Exception as err:
        log.error("Failed to write to simulated_emails table: %s", err)
    return row
