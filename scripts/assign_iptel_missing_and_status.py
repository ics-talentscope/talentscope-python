"""Assign missing IPTel roster tests (MRF / TPM Upskilling), then print status for all 198."""
from __future__ import annotations

import os
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MSSQL_TARGET", "remote")
os.environ.setdefault("MSSQL_REMOTE_DATABASE", "TalentScope_DB_Live")

# Import assign helpers from Handoff kit
HANDOFF_SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "Handoff"
    / "test schedule file"
    / "scripts"
)
sys.path.insert(0, str(HANDOFF_SCRIPTS))

from assign_product_tests import (  # noqa: E402
    assign_assessment,
    ensure_account,
    load_bank_sheet,
    load_wide_mcqs,
)
from core.config import mssql_settings  # noqa: E402
from core.db import MssqlStore, set_store  # noqa: E402
from services import accounts  # noqa: E402

ROSTER = (
    Path(__file__).resolve().parents[2]
    / "Handoff"
    / "test schedule file"
    / "iptel_check_roster.tsv"
)
QB = (
    Path(__file__).resolve().parents[2]
    / "Handoff"
    / "test schedule file"
    / "question-banks"
    / "Question_Bank_Filled.xlsx"
)
TPM_MCQ = (
    Path(__file__).resolve().parents[2]
    / "Handoff"
    / "test schedule file"
    / "question-banks"
    / "TPM_Upskilling_MCQs.xlsx"
)
OUT_DIR = Path(__file__).resolve().parents[2] / "Handoff" / "test schedule file" / "reports"
PER_TEST = 25
TOPIC_ID = accounts.PRODUCT_ASSESSMENT_TOPIC_ID


def load_roster(path: Path) -> list[dict]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines()):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if i == 0 and parts[0].lower().startswith("emp"):
            continue
        if len(parts) < 5:
            continue
        emp_id, name, product, project, email = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip(),
            parts[3].strip(),
            parts[4].strip().lower(),
        )
        if not emp_id or not email:
            continue
        rows.append(
            {
                "employee_id": emp_id,
                "full_name": name or emp_id,
                "listed_product": product,
                "project": project,
                "email": email,
            }
        )
    return rows


def normalize_assign_product(listed: str) -> str | None:
    key = (listed or "").strip().upper().replace(" ", "")
    if listed.strip().upper() in ("MRF",):
        return "MRF"
    if listed.strip().upper() in ("TPM", "TPM UPSKILLING"):
        return "TPM Upskilling"
    if key in ("MSS-MGW", "MSS/MGW", "MSS", "MGW") or "MSS" in listed.upper():
        return None  # no question bank
    return None


def test_status(store: MssqlStore, emp_id: str) -> dict:
    account = accounts.get_account(emp_id)
    if not account:
        return {
            "account": False,
            "product_db": "",
            "status": "NO_ACCOUNT",
            "topic": "",
            "qcount": 0,
            "score": "",
            "completed_at": "",
        }
    emp_uuid = accounts.employee_uuid(emp_id)
    test = None
    if emp_uuid:
        test = store.first(
            "tests",
            {"employee_id": emp_uuid, "topic_id": TOPIC_ID},
            [
                "id",
                "topic_title",
                "status",
                "total_questions",
                "score_percent",
                "score_correct",
                "score_total",
                "completed_at",
            ],
        )
    if not test:
        # fallback any product assessment by employee_code
        rows = store.query(
            """
            SELECT TOP 1 id, topic_title, status, total_questions,
                   score_percent, score_correct, score_total, completed_at
            FROM dbo.tests
            WHERE employee_code = ? AND topic_id = ?
            ORDER BY created_at DESC
            """,
            (emp_id, TOPIC_ID),
        )
        test = rows[0] if rows else None
    if not test:
        return {
            "account": True,
            "product_db": account.get("product") or "",
            "status": "NO_TEST",
            "topic": "",
            "qcount": 0,
            "score": "",
            "completed_at": "",
        }
    qcount = store.query(
        "SELECT COUNT(*) AS c FROM dbo.test_questions WHERE test_id = ?",
        (test["id"],),
    )[0]["c"]
    st = (test.get("status") or "").lower()
    if qcount <= 0:
        bucket = "NO_QS"
    elif st in ("completed", "complete", "submitted", "scored"):
        bucket = "COMPLETED"
    elif st in ("in_progress", "started", "active"):
        bucket = "IN_PROGRESS"
    else:
        bucket = "READY"
    score = ""
    if test.get("score_percent") is not None:
        score = f"{test['score_percent']}%"
    elif test.get("score_correct") is not None and test.get("score_total"):
        score = f"{test['score_correct']}/{test['score_total']}"
    return {
        "account": True,
        "product_db": account.get("product") or "",
        "status": bucket,
        "topic": test.get("topic_title") or "",
        "qcount": int(qcount or 0),
        "score": score,
        "completed_at": test.get("completed_at") or "",
        "raw_status": test.get("status") or "",
    }


def main() -> int:
    set_store(None)
    store = MssqlStore(mssql_settings())
    set_store(store)
    print("DB", mssql_settings()["database"], "ping", store.ping(), flush=True)

    roster = load_roster(ROSTER)
    print("roster", len(roster), flush=True)

    mrf_bank = load_bank_sheet(QB, "MRF")
    tpm_bank = load_wide_mcqs(TPM_MCQ)
    print("MRF bank", len(mrf_bank), "TPM bank", len(tpm_bank), flush=True)

    # First pass: who needs assign
    need: list[tuple[dict, str]] = []
    blocked: list[tuple[dict, str]] = []
    for row in roster:
        info = test_status(store, row["employee_id"])
        if info["status"] in ("READY", "COMPLETED", "IN_PROGRESS") and info["qcount"] > 0:
            continue
        assign_prod = normalize_assign_product(row["listed_product"])
        if assign_prod is None:
            # only block if listed product has no bank; others with NO_TEST may be different products
            listed = row["listed_product"].strip().upper()
            if listed in ("MRF", "TPM", "TPM UPSKILLING") or "MSS" in listed or "MGW" in listed:
                blocked.append((row, f"no question bank for {row['listed_product']}"))
            else:
                blocked.append((row, f"NO_TEST and no auto-assign for {row['listed_product']}"))
            continue
        need.append((row, assign_prod))

    print(f"to_assign={len(need)} blocked={len(blocked)}", flush=True)
    ok = fail = 0
    assign_log: list[str] = []
    for row, product in need:
        try:
            bank = mrf_bank if product == "MRF" else tpm_bank
            if len(bank) < 2:
                raise RuntimeError(f"empty bank for {product}")
            rng = random.Random(f"{row['employee_id']}:{product}:20261026")
            picked = bank[:] if len(bank) <= PER_TEST else rng.sample(bank, PER_TEST)
            account = ensure_account(
                {
                    "employee_id": row["employee_id"],
                    "full_name": row["full_name"],
                    "email": row["email"],
                    "role_label": product,
                },
                product,
            )
            result = assign_assessment(store, account, picked, product)
            ok += 1
            line = f"OK {row['employee_id']} {row['full_name']} {product} q={len(picked)} {result}"
            assign_log.append(line)
            print(line, flush=True)
        except Exception as err:
            fail += 1
            line = f"FAIL {row['employee_id']} {row['full_name']}: {err}"
            assign_log.append(line)
            print(line, flush=True)

    for row, reason in blocked:
        line = f"SKIP {row['employee_id']} {row['full_name']} {row['listed_product']}: {reason}"
        assign_log.append(line)
        print(line, flush=True)

    # Full status table
    table_rows: list[str] = []
    completed: list[str] = []
    counts: Counter[str] = Counter()
    header = (
        "Emp ID\tName\tListed Product\tDB Product\tStatus\tTopic\tQ Count\tScore\tCompleted At\tEmail"
    )
    table_rows.append(header)
    for row in roster:
        info = test_status(store, row["employee_id"])
        counts[info["status"]] += 1
        line = "\t".join(
            [
                row["employee_id"],
                row["full_name"],
                row["listed_product"],
                info["product_db"],
                info["status"],
                info["topic"],
                str(info["qcount"]),
                info["score"],
                str(info["completed_at"] or ""),
                row["email"],
            ]
        )
        table_rows.append(line)
        if info["status"] == "COMPLETED":
            completed.append(
                f"{row['employee_id']}\t{row['full_name']}\t{row['listed_product']}\t"
                f"{info['topic']}\t{info['score']}\t{info['completed_at']}\t{row['email']}"
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    status_path = OUT_DIR / f"iptel_status_{stamp}.tsv"
    completed_path = OUT_DIR / f"iptel_completed_{stamp}.tsv"
    status_path.write_text("\n".join(table_rows) + "\n", encoding="utf-8")
    completed_path.write_text(
        "Emp ID\tName\tListed Product\tTopic\tScore\tCompleted At\tEmail\n"
        + "\n".join(completed)
        + ("\n" if completed else ""),
        encoding="utf-8",
    )

    print("\n=== SUMMARY ===", flush=True)
    print(f"roster={len(roster)} assign_ok={ok} assign_fail={fail}", flush=True)
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}", flush=True)
    print(f"\n=== COMPLETED ({len(completed)}) ===", flush=True)
    for c in completed:
        print(c, flush=True)
    print(f"\nWrote {status_path}", flush=True)
    print(f"Wrote {completed_path}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
