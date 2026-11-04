import csv
import io
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

PORTAL_TZ = "Asia/Kolkata"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
THIN = Side(style="thin", color="FFE2E8F0")
BLUE_THIN = Side(style="thin", color="FF8FAADC")


def to_bytes(workbook: Workbook) -> bytes:
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v)
    return str(value).replace("\u00a0", " ").strip()


def format_timestamp(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    local = moment.astimezone(ZoneInfo(PORTAL_TZ))
    hour = local.hour % 12 or 12
    return f"{local.day}/{local.month}/{local.year}, {hour}:{local.minute:02d}:{local.second:02d} {'am' if local.hour < 12 else 'pm'}"


def format_completed_on(iso: Optional[str]) -> str:
    return format_timestamp(iso) or "—"


def attempt_result(is_correct) -> str:
    return "Correct" if is_correct is True else "Incorrect" if is_correct is False else ""


def question_answer_block(questions: list[dict]) -> str:
    if not questions:
        return ""
    lines = [f"Assigned Questions ({len(questions)})"]
    for q in questions:
        lines.append(q["question_text"])
        if q.get("selected_option_text"):
            suffix = attempt_result(q.get("is_correct"))
            lines.append(f"Selected: {q['selected_option_text']}{f' ({suffix})' if suffix else ''}")
        else:
            lines.append("Not answered")
        if q.get("submitted_at"):
            lines.append(f"Submitted: {format_timestamp(q['submitted_at'])}")
        lines.append("")
    return "\n".join(lines).rstrip()


STATUS_LABELS = {"completed": "Completed", "in_progress": "In Progress", "pending": "Pending", "not_started": "Not Started", "unassigned": "Unassigned"}


def status_label(status) -> str:
    return STATUS_LABELS.get(status, str(status) if status else "—")


def score_percent(score, score_max=25) -> int:
    if score is None or score_max is None or score_max <= 0:
        return 0
    return int(score / score_max * 100 + 0.5)


def score_font_argb(percent: int) -> str:
    return "FF059669" if percent >= 85 else "FFD97706" if percent >= 50 else "FFE11D48"


def proctoring_summary(account: dict) -> Optional[dict]:
    tests = account.get("tests") or []
    primary = next((t for t in tests if t["id"] == account.get("test_id")), None) or next((t for t in tests if t.get("status") == "completed"), None) \
        or (tests[0] if tests else None)
    proctoring = (primary or {}).get("proctoring")
    if not proctoring:
        return None
    violations = proctoring.get("violations") if isinstance(proctoring.get("violations"), list) else []
    flags = proctoring.get("warningCount")
    flags = flags if flags is not None else len(violations)
    if flags <= 0 and not violations:
        return None
    return {"flagCount": max(flags, len(violations)), "violations": violations}


def violations_text(violations: list[dict]) -> str:
    if not violations:
        return "—"
    lines = []
    for i, v in enumerate(violations, 1):
        when = format_timestamp(v.get("timestamp"))
        lines.append(f"{i}. {v.get('type')}{f' ({when})' if when else ''}")
    return "\n".join(lines)


def _header_row(sheet, color: str, height: int = 25) -> None:
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFFFF")
        cell.fill = PatternFill("solid", fgColor=color)
        cell.alignment = Alignment(vertical="center", horizontal="left", wrap_text=True)
    sheet.row_dimensions[1].height = height


def _borders(sheet) -> None:
    border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", horizontal="left", wrap_text=True)
            cell.border = border


def shortlisted_workbook(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Shortlisted Interviews"
    columns = [("Name", 28), ("Employee ID", 14), ("Email", 28), ("Designation", 26), ("Department", 18), ("Grade", 10), ("Match Score", 14),
               ("Decision", 12), ("Matching Skills", 36), ("Skills", 42), ("Status", 12)]
    ws.append([c[0] for c in columns])
    for i, (_, width) in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFFFF")
        cell.fill = PatternFill("solid", fgColor="FF4C1D95")
        cell.alignment = Alignment(vertical="center")
    for emp in rows:
        try:
            score = f"{int(float(emp.get('score')) + 0.5)}%"
        except (TypeError, ValueError):
            score = ""
        ws.append([_cell_text(emp.get("full_name")), _cell_text(emp.get("employee_id")), _cell_text(emp.get("email")), _cell_text(emp.get("designation")),
                   _cell_text(emp.get("department")), _cell_text(emp.get("grade")), score, _cell_text(emp.get("matchDecision")).upper(),
                   _cell_text(emp.get("matchingSkills")), _cell_text(emp.get("skills")), _cell_text(emp.get("status")) or "Shortlisted"])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}1"
    return to_bytes(wb)


def portal_workbook(employees: list[dict], attempts_by_test: dict[str, list[dict]]) -> bytes:
    from services.products import format_product_display_name

    wb = Workbook()
    summary = wb.active
    summary.title = "Employee Portal Results"
    headers = ["Employee Name", "Employee ID", "Role", "Domain", "Product", "Email", "DDH", "Test Status", "Completed On", "Score",
               "Proctor Flags", "Proctor Violations", "Remarks", "Assigned Questions & Answers"]
    summary.append(headers)
    wide = {"Email": 28, "Assigned Questions & Answers": 80, "Employee Name": 25, "Remarks": 30, "Proctor Violations": 50}
    for i, h in enumerate(headers, 1):
        summary.column_dimensions[get_column_letter(i)].width = wide.get(h, 16)
    _header_row(summary, "FF4F46E5")
    detail = wb.create_sheet("Question Details")
    detail_headers = ["Employee ID", "Employee Name", "Question #", "Question", "Selected Answer", "Result", "Submitted At"]
    detail.append(detail_headers)
    for i, h in enumerate(detail_headers, 1):
        detail.column_dimensions[get_column_letter(i)].width = 60 if h == "Question" else 40 if h == "Selected Answer" else 18
    _header_row(detail, "FF4F46E5")
    score_col = headers.index("Score") + 1
    for account in employees:
        name = account.get("full_name") or account["employee_id"]
        score_max = account.get("score_max") or account.get("assigned_question_count") or 25
        completed = bool(account.get("test_id") and account.get("test_status") == "completed")
        attempts = attempts_by_test.get(account["test_id"]) if completed else None
        if attempts:
            block = question_answer_block(attempts)
        elif account.get("assigned_questions"):
            block = "\n".join([f"Assigned Questions ({account.get('assigned_question_count')})", *account["assigned_questions"]])
        else:
            block = ""
        proctor = proctoring_summary(account)
        score = account.get("score")
        summary.append([
            name, account["employee_id"], account.get("role") or "—", account.get("domain") or "—",
            format_product_display_name(account.get("product")) or "—", account.get("email") or "—", account.get("ddh") or "—",
            status_label(account.get("test_status")), format_completed_on(account.get("completed_at")),
            f"{score}/{score_max} ({score_percent(score, score_max)}%)" if score is not None else "—",
            str(proctor["flagCount"]) if proctor else "—", violations_text(proctor["violations"]) if proctor else "—",
            (account.get("remarks") or "").strip() or "—", block])
        if score is not None:
            summary.cell(row=summary.max_row, column=score_col).font = Font(bold=True, color=score_font_argb(score_percent(score, score_max)))
        if attempts:
            for q in attempts:
                detail.append([account["employee_id"], name, q["question_index"] + 1, q["question_text"], q.get("selected_option_text") or "Not answered",
                               (attempt_result(q.get("is_correct")) or "—") if q.get("selected_option_text") else "—",
                               format_timestamp(q["submitted_at"]) if q.get("submitted_at") else "—"])
        else:
            for idx, text in enumerate(account.get("assigned_questions") or [], 1):
                detail.append([account["employee_id"], name, idx, text, "—", "—", "—"])
    _borders(summary)
    _borders(detail)
    return to_bytes(wb)


def portal_summary_workbook(detail_workbook: bytes, source_name: str) -> bytes:
    from collections import defaultdict

    detail = load_workbook(io.BytesIO(detail_workbook), read_only=True, data_only=True)
    if "Employee Portal Results" not in detail.sheetnames:
        raise ValueError("Detailed Employee Portal export is missing its results sheet.")
    source_sheet = detail["Employee Portal Results"]
    header = [_cell_text(cell) for cell in next(source_sheet.iter_rows(min_row=1, max_row=1, values_only=True))]
    columns = {name: index for index, name in enumerate(header)}
    required = ("Employee ID", "Domain", "Product", "Test Status")
    missing = [name for name in required if name not in columns]
    if missing:
        raise ValueError(f"Detailed Employee Portal export is missing columns: {', '.join(missing)}")

    records = []
    for row in source_sheet.iter_rows(min_row=2, values_only=True):
        employee_id = _cell_text(row[columns["Employee ID"]] if columns["Employee ID"] < len(row) else "")
        if not employee_id:
            continue
        records.append({
            "employee_id": employee_id,
            "domain": _cell_text(row[columns["Domain"]] if columns["Domain"] < len(row) else "") or "—",
            "product": _cell_text(row[columns["Product"]] if columns["Product"] < len(row) else "") or "—",
            "status": _cell_text(row[columns["Test Status"]] if columns["Test Status"] < len(row) else "").casefold(),
        })
    detail.close()

    def counts(members: list[dict]) -> dict:
        completed = sum(member["status"] == "completed" for member in members)
        in_progress = sum(member["status"] in {"in progress", "pending"} for member in members)
        not_started = sum(member["status"] == "not started" for member in members)
        scheduled = completed + in_progress + not_started
        return {
            "scheduled": scheduled,
            "completed": completed,
            "in_progress": in_progress,
            "not_started": not_started,
            "completion": round(completed / scheduled * 100, 1) if scheduled else 0,
        }

    wb = Workbook()
    overview = wb.active
    overview.title = "Summary"
    overview.append(["TalentScope Employee Portal — Status Summary"])
    overview.merge_cells("A1:B1")
    overview.append([f"Source: {source_name} | Total people scheduled: {counts(records)['scheduled']}"])
    overview.merge_cells("A2:B2")
    overview["A1"].font = Font(bold=True, color="FF1F4E79", underline="single")
    overview["A2"].font = Font(italic=True, color="FF1F4E79")
    overview.append([])
    overview.append(["Overall"])
    overview["A4"].font = Font(bold=True, color="FF1F4E79")
    overview.append(["Metric", "Count"])
    for cell in overview[5]:
        cell.font = Font(bold=True, color="FFFFFFFF")
        cell.fill = PatternFill("solid", fgColor="FF4F46E5")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    totals = counts(records)
    for label, value in (
        ("Total Scheduled", totals["scheduled"]),
        ("Completed (took the test)", totals["completed"]),
        ("In Progress", totals["in_progress"]),
        ("Not Started", totals["not_started"]),
        ("Completion %", totals["completion"] / 100),
    ):
        overview.append([label, value])
    overview["B7"].fill = PatternFill("solid", fgColor="FFD9FBE8")
    overview["B9"].fill = PatternFill("solid", fgColor="FFFFF2CC")
    overview["B10"].number_format = "0.0%"
    overview.column_dimensions["A"].width = 38
    overview.column_dimensions["B"].width = 16
    overview.freeze_panes = "A5"
    _borders(overview)

    def add_breakdown(title: str, key: str, note: str) -> None:
        sheet = wb.create_sheet(title)
        sheet.append([f"Counts by {title.removeprefix('By ')}"])
        sheet.append([note])
        sheet.append([])
        sheet.append([title.removeprefix("By "), "Scheduled", "Completed", "In Progress", "Not Started", "Completion %"])
        for cell in sheet[4]:
            cell.font = Font(bold=True, color="FFFFFFFF")
            cell.fill = PatternFill("solid", fgColor="FF4F46E5")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        groups: dict[str, list[dict]] = defaultdict(list)
        for record in records:
            groups[record[key]].append(record)
        ordered = sorted(groups.items(), key=lambda item: (-counts(item[1])["scheduled"], item[0].casefold()))
        for label, members in ordered:
            group = counts(members)
            sheet.append([label, group["scheduled"], group["completed"], group["in_progress"], group["not_started"], group["completion"]])
            sheet.cell(row=sheet.max_row, column=3).fill = PatternFill("solid", fgColor="FFD9FBE8")
            sheet.cell(row=sheet.max_row, column=5).fill = PatternFill("solid", fgColor="FFFFF2CC")
            sheet.cell(row=sheet.max_row, column=6).number_format = "0.0"
        total_row = counts(records)
        sheet.append(["TOTAL", total_row["scheduled"], total_row["completed"], total_row["in_progress"],
                      total_row["not_started"], total_row["completion"]])
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="FFF1F5F9")
        sheet.cell(row=sheet.max_row, column=6).number_format = "0.0"
        sheet.column_dimensions["A"].width = 34
        for column in range(2, 7):
            sheet.column_dimensions[get_column_letter(column)].width = 18
        sheet.freeze_panes = "B5"
        _borders(sheet)

    add_breakdown("By Domain", "domain", "Scheduled = assigned on portal. Completed = took/finished the test.")
    add_breakdown("By Product", "product", "Use this sheet for TPM Upskilling / MSS-MGW / SBC etc.")

    cross = wb.create_sheet("Domain x Product")
    cross.append(["Counts by Domain and Product"])
    cross.append([])
    cross.append(["Domain", "Product", "Scheduled", "Completed", "In Progress", "Not Started", "Completion %"])
    for cell in cross[3]:
        cell.font = Font(bold=True, color="FFFFFFFF")
        cell.fill = PatternFill("solid", fgColor="FF4F46E5")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    pairs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        pairs[(record["domain"], record["product"])].append(record)
    for domain, product in sorted(pairs, key=lambda pair: (pair[0].casefold(), pair[1].casefold())):
        group = counts(pairs[(domain, product)])
        cross.append([domain, product, group["scheduled"], group["completed"], group["in_progress"],
                      group["not_started"], group["completion"]])
        cross.cell(row=cross.max_row, column=7).number_format = "0.0"
    cross.column_dimensions["A"].width = 28
    cross.column_dimensions["B"].width = 28
    for column in range(3, 8):
        cross.column_dimensions[get_column_letter(column)].width = 18
    cross.freeze_panes = "C4"
    _borders(cross)

    instructions = wb.create_sheet("How to use")
    for row in (
        ["How to refresh this summary"],
        [""],
        ["1. In TalentScope Admin -> Employee Portal, click Export Summary."],
        ["2. The summary is calculated from the same Employee Portal Results data as Export to Excel."],
        ["3. Click Export to Excel if you also need employee and question-level details."],
        ["4. Use the By Domain, By Product, and Domain x Product sheets for breakdowns."],
        ["5. Use the By Product sheet to review a specific product."],
        [""],
        ["Status meanings:"],
        ["- Scheduled = person appears on the portal with an assignment (total rows)."],
        ["- Completed = took and finished the test."],
        ["- In Progress = started but not finished (includes partial-answer Pending status)."],
        ["- Not Started = assigned but has not begun."],
    ):
        instructions.append(row)
    instructions["A1"].font = Font(bold=True, size=12, color="FF1F4E79", underline="single")
    instructions.column_dimensions["A"].width = 110
    instructions.freeze_panes = "A3"
    _borders(instructions)
    return to_bytes(wb)


def bucket_scores(scores: list[int]) -> dict:
    counts = {"rejected": 0, "evaluate": 0, "l1": 0, "shortlisted": 0, "total": len(scores)}
    for score in scores:
        if score < 30:
            counts["rejected"] += 1
        elif score <= 50:
            counts["evaluate"] += 1
        elif score <= 70:
            counts["l1"] += 1
        else:
            counts["shortlisted"] += 1
    return counts


def _header_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_-]", " ", text.lower())).strip()


def _find_col(headers: list[str], names: list[str]) -> int:
    for name in names:
        for i, h in enumerate(headers):
            if h == name or name in h:
                return i
    return -1


def _parse_score(raw: str) -> Optional[int]:
    try:
        return max(0, min(100, int(float(str(raw or "").replace("%", "").strip()) + 0.5)))
    except ValueError:
        return None


def parse_match_workbook(data: bytes, file_name: str) -> dict:
    title = re.sub(r"^.*[\\/]", "", file_name or "").strip() or "Requirement"
    lower = (file_name or "").lower()
    if lower.endswith((".csv", ".txt")):
        text = data.decode("utf-8-sig", errors="replace")
        table = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
    else:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheet = workbook.worksheets[0] if workbook.worksheets else None
        table = []
        if sheet is not None:
            for row in sheet.iter_rows(values_only=True):
                cells = [_cell_text(v) for v in row]
                if any(cells):
                    table.append(cells)
    if len(table) < 2:
        return {"title": title, "demand": None, "scores": []}
    headers = [_header_key(h) for h in table[0]]
    score_col = _find_col(headers, ["match score", "percentage", "score %", "score"])
    if score_col < 0:
        return {"title": title, "demand": None, "scores": []}
    scores = []
    for row in table[1:]:
        if not any(str(v or "").strip() for v in row):
            continue
        value = _parse_score(row[score_col] if score_col < len(row) else "")
        scores.append(0 if value is None else value)
    return {"title": title, "demand": None, "scores": scores}


def percentage_workbook(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Percentage summary"
    for i, width in enumerate((56, 24, 28, 30, 36, 28), 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[1].height = 36
    border = Border(top=BLUE_THIN, bottom=BLUE_THIN, left=BLUE_THIN, right=BLUE_THIN)

    def fill(cell, value, bg, bold=False, wrap=False, center=False):
        cell.value = value
        cell.fill = PatternFill("solid", fgColor=f"FF{bg}")
        cell.font = Font(bold=bold, color="FF1F4E79", name="Calibri", size=11)
        cell.alignment = Alignment(vertical="center", horizontal="center" if center else "left", wrap_text=wrap)
        cell.border = border

    heads = ["Corp Pool Resource's Scores against the JD", "Demand against Each JD", "Score <30 % is Rejected", "Score 30 to 50% To evaluate",
             "Score 51 to 70% - Direct L1 Interview", "Score >70 % Shortlisted"]
    fills = ["BDD7EE", "5B9BD5", "C6E0B4", "C6E0B4", "C6E0B4", "C6E0B4"]
    for i, (text, bg) in enumerate(zip(heads, fills), 1):
        fill(ws.cell(row=1, column=i), text, bg, bold=True, wrap=True, center=True)
    for idx, row in enumerate(rows, 2):
        counts = bucket_scores(row["scores"])
        ws.row_dimensions[idx].height = 22
        fill(ws.cell(row=idx, column=1), row["title"], "BDD7EE", bold=True, wrap=True)
        fill(ws.cell(row=idx, column=2), "", "5B9BD5", center=True)
        for col, key in ((3, "rejected"), (4, "evaluate"), (5, "l1"), (6, "shortlisted")):
            fill(ws.cell(row=idx, column=col), counts[key], "C6E0B4", center=True)
    ws.freeze_panes = "A2"
    return to_bytes(wb)
