"""Send product-assessment portal invites to IPTel roster (Live). Run on VM / prod API."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import mssql_settings
from core.db import MssqlStore, set_store
from services import accounts, mailer
from services.portal_invite_mail import build_portal_invite_html

ROSTER = Path(os.environ.get("IPTEL_ROSTER") or "/tmp/iptel_recheck_roster.tsv")
PORTAL_URL = "https://talentscope.infinite.com/"
TOPIC_ID = accounts.PRODUCT_ASSESSMENT_TOPIC_ID
SKIP_PRODUCTS = {"MSS/MGW", "MSS-MGW", "MSS", "MGW"}
SLEEP_S = float(os.environ.get("MAIL_SLEEP_S") or "0.35")


def load_roster(path: Path) -> list[dict]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines()):
        if not line.strip():
            continue
        parts = line.split("\t")
        if i == 0 and parts[0].lower().startswith("emp"):
            continue
        if len(parts) < 5:
            continue
        rows.append(
            {
                "employee_id": parts[0].strip(),
                "full_name": parts[1].strip(),
                "product": parts[2].strip(),
                "email": parts[4].strip().lower(),
            }
        )
    return rows


def has_usable_test(store: MssqlStore, emp_id: str) -> tuple[bool, str]:
    account = accounts.get_account(emp_id)
    if not account:
        return False, "NO_ACCOUNT"
    emp_uuid = accounts.employee_uuid(emp_id)
    test = None
    if emp_uuid:
        test = store.first(
            "tests",
            {"employee_id": emp_uuid, "topic_id": TOPIC_ID},
            ["id", "status"],
        )
    if not test:
        found = store.query(
            """
            SELECT TOP 1 id, status FROM dbo.tests
            WHERE employee_code = ? AND topic_id = ?
            ORDER BY created_at DESC
            """,
            (emp_id, TOPIC_ID),
        )
        test = found[0] if found else None
    if not test:
        return False, "NO_TEST"
    q = store.query("SELECT COUNT(*) AS c FROM dbo.test_questions WHERE test_id = ?", (test["id"],))[0]["c"]
    if int(q or 0) <= 0:
        return False, "NO_QS"
    st = (test.get("status") or "").lower()
    if st in ("completed", "complete", "submitted", "scored"):
        return False, "COMPLETED"
    return True, "READY"


def main() -> int:
    set_store(None)
    store = MssqlStore(mssql_settings())
    set_store(store)
    print("DB", mssql_settings()["database"], "ping", store.ping(), flush=True)
    print("roster_file", ROSTER, "exists", ROSTER.is_file(), flush=True)
    roster = load_roster(ROSTER)
    print("roster", len(roster), flush=True)

    ok = fail = skip = 0
    for row in roster:
        prod = row["product"].strip().upper().replace(" ", "")
        if row["product"].strip().upper() in SKIP_PRODUCTS or "MSS" in row["product"].upper():
            skip += 1
            print("SKIP_PRODUCT", row["employee_id"], row["product"], flush=True)
            continue
        usable, reason = has_usable_test(store, row["employee_id"])
        if not usable:
            skip += 1
            print("SKIP", reason, row["employee_id"], row["full_name"], flush=True)
            continue
        email = row["email"]
        if not email or "@" not in email:
            fail += 1
            print("FAIL_NO_EMAIL", row["employee_id"], flush=True)
            continue
        subject = "Product Assessment Assignment - Employee Learning Portal"
        html = build_portal_invite_html(
            row["full_name"],
            row["employee_id"],
            temporary_password=row["employee_id"],
            portal_url=PORTAL_URL,
            product_hint=row["product"] or "your assigned product",
        )
        try:
            status, err = mailer.dispatch(email, subject, html)
            mailer.record(email, row["full_name"], subject, html, status, "iptel_bulk_mail")
            if status in ("sent", "ok") or (status and "sent" in status.lower() and "fail" not in status.lower()):
                ok += 1
                print("OK", row["employee_id"], email, status, flush=True)
            elif status and "simulat" in status.lower():
                fail += 1
                print("SIMULATED", row["employee_id"], email, status, err, flush=True)
            else:
                # treat unknown non-fail as ok if no error
                if err:
                    fail += 1
                    print("FAIL", row["employee_id"], email, status, err, flush=True)
                else:
                    ok += 1
                    print("OK", row["employee_id"], email, status, flush=True)
        except Exception as exc:
            fail += 1
            print("FAIL", row["employee_id"], email, exc, flush=True)
        time.sleep(SLEEP_S)

    print(f"DONE ok={ok} fail={fail} skip={skip}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
