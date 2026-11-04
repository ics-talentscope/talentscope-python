import csv
import io
import logging
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response

from core.auth import acting_admin_email, require_any_permission, require_permission
from core.config import env
from core.db import get_store
from core.errors import json_error, public_error
from services import admin_accounts, corp_pool, docs_storage, portal_roster as pr, test_video
from services import employee_tests as et
from services.products import format_topic_title_for_display
from services.structured_log import write_log

log = logging.getLogger("talentscope")
router = APIRouter(
    prefix="/api/admin/employees",
    tags=["admin-employees"],
    dependencies=[Depends(require_any_permission("corpPool", "employeePortal"))],
)

TEST_COLUMNS = ["id", "employee_id", "employee_code", "status", "topic_id", "subject_id", "difficulty", "total_questions", "score_correct",
                "score_total", "score_percent", "session_recording_url", "proctoring", "started_at", "completed_at", "topic_title", "subject_title"]
EDITABLE = ("full_name", "email", "department", "skills", "designation", "grade", "status", "product")


def _round(value: float) -> int:
    return int(value + 0.5)


def _test_results(store) -> list[dict]:
    employees = store.select("employees", columns=["id", "employee_id", "full_name"])
    by_key: dict = {}
    for row in employees:
        by_key[str(row["id"]).lower()] = row
        code = pr.normalize_employee_id(row.get("employee_id"))
        if code:
            by_key[code] = row
    recordings = test_video.list_ids()
    results = []
    for test in store.select("tests", columns=TEST_COLUMNS, order="completed_at desc"):
        owner = str(test.get("employee_id") or "")
        linked = by_key.get(owner.lower()) or by_key.get(pr.normalize_employee_id(test.get("employee_code")))
        emp_id = pr.display_employee_code(test.get("employee_code"), owner, (linked or {}).get("employee_id"), (linked or {}).get("full_name"))
        if not emp_id or pr.is_employee_uuid(emp_id):
            continue
        total = test.get("score_total") or test.get("total_questions") or 25
        correct = test.get("score_correct") or 0
        percent = test.get("score_percent")
        percent = percent if percent is not None else (_round(correct / total * 100) if total > 0 else 0)
        results.append({
            "id": test["id"], "employeeUuid": owner if pr.is_employee_uuid(owner) or linked else None, "employeeId": emp_id,
            "employeeName": (linked or {}).get("full_name") or emp_id, "topicId": test.get("topic_id"),
            "topicTitle": format_topic_title_for_display(test.get("topic_title") or "Unknown Topic"), "subjectId": test.get("subject_id"),
            "subjectTitle": test.get("subject_title") or "Unknown Subject", "difficulty": test.get("difficulty") or "medium",
            "totalQuestions": total, "status": test.get("status"), "answeredCount": 0, "correctCount": correct, "score": correct,
            "scorePercent": percent, "videoUrl": test.get("session_recording_url"), "proctoring": et.normalize_proctoring(test.get("proctoring")),
            "startedAt": test.get("started_at"), "completedAt": test.get("completed_at"), "hasRecording": test["id"] in recordings,
        })
    attempts = store.select("test_attempts", columns=["test_id"])
    counts: dict = {}
    for a in attempts:
        counts[a["test_id"]] = counts.get(a["test_id"], 0) + 1
    for r in results:
        r["answeredCount"] = counts.get(r["id"], 0)
    return results


def _csv(employees: list[dict]) -> Response:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Employee ID", "Name", "Department", "Designation", "Skills", "Status", "Grade", "Match Score", "Shortlisted"])
    for e in employees:
        writer.writerow([e.get("employee_id", ""), e.get("full_name", ""), e.get("department", ""), e.get("designation", ""), e.get("skills", ""),
                         e.get("status", ""), e.get("grade", ""), e.get("score", 0), "Yes" if e.get("shortlisted") else "No"])
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="employee_pool.csv"'})


def _apply_jd_match(roster: list[dict], jd_id) -> list[dict]:
    if not jd_id or jd_id == "all" or not roster or not pr.UUID_RE.match(jd_id):
        return roster
    from services import skill_match

    try:
        jd = get_store().first("job_descriptions", {"id": jd_id}, ["jd_text"])
        if not jd or not (jd.get("jd_text") or "").strip():
            return roster
        results = skill_match.score_people(jd["jd_text"], roster)
    except Exception as err:
        log.warning("Failed to recalculate employee skill match: %s", err)
        return roster
    out = []
    for emp, res in zip(roster, results):
        override = emp.get("score_override") if emp.get("score_override_jd_id") == jd_id and isinstance(emp.get("score_override"), (int, float)) else None
        out.append({**emp, "score": override if override is not None else res["score"], "matchingSkills": res.get("matchingSkills") or []})
    return out


@router.get("")
def list_employees(request: Request):
    try:
        email = acting_admin_email(request) or ""
        hide_portal = bool(email) and not admin_accounts.get_access(email)["canViewEmployeePortal"]
        deleted = corp_pool.load_deleted()
        roster = [e for e in corp_pool.load_roster() if not corp_pool.is_deleted(deleted, e["employee_id"], e.get("source_file", ""))]
        results = _test_results(get_store())
        by_emp: dict = {}
        for r in results:
            by_emp.setdefault(r["employeeId"], []).append({"status": r["status"], "score": r["scorePercent"], "completedAt": r["completedAt"]})
        roster = _apply_jd_match(roster, request.query_params.get("activeJdId"))
        employees = [{**e, "testResults": by_emp.get(e["employee_id"], [])} for e in roster]
        if request.query_params.get("export") == "true":
            return _csv(employees)
        portal = []
        try:
            portal = pr.build_portal_employees(results)
        except Exception as err:
            log.warning("Failed to load employee portal mapping: %s", err)
        if hide_portal:
            return {"employees": employees, "allTestResults": [], "resourcePortalEmployees": []}
        return {"employees": employees, "allTestResults": results, "resourcePortalEmployees": portal}
    except Exception as err:
        return public_error(err, "Failed to load employees")


@router.post("")
def shortlist(body: dict = Body(default_factory=dict)):
    employee_id = body.get("employeeId")
    try:
        changes = [{"id": str(c.get("employeeId") or "").strip(), "value": c["shortlisted"]} for c in (body.get("changes") or [])
                   if isinstance(c, dict) and isinstance(c.get("shortlisted"), bool) and str(c.get("employeeId") or "").strip()]
        ids = [str(i or "").strip() for i in (body.get("employeeIds") or []) if str(i or "").strip()]
        if employee_id:
            ids.append(str(employee_id).strip())
        ids += [c["id"] for c in changes]
        unique = list(dict.fromkeys(ids))
        if not unique:
            return json_error(400, "Employee ID is required")
        roster = corp_pool.load_roster()
        if not roster:
            return json_error(404, "Employees not loaded")
        set_to = body.get("shortlisted") if isinstance(body.get("shortlisted"), bool) else None
        change_map = {c["id"]: c["value"] for c in changes}
        updated = []
        for ident in unique:
            row = next((e for e in roster if e["employee_id"] == ident), None)
            if not row:
                continue
            row["shortlisted"] = bool(change_map[ident]) if ident in change_map else (not row.get("shortlisted") if set_to is None else set_to)
            updated.append(row)
        if not updated:
            return json_error(404, "Employee not found")
        corp_pool.save_roster(roster)
        write_log("employee", "SHORTLIST_EMPLOYEES_BULK" if len(unique) > 1 else "SHORTLIST_EMPLOYEE", "success",
                  f"Set shortlist={set_to} for {len(updated)} employee(s)" if len(unique) > 1
                  else f"Toggled shortlist for employee ID {unique[0]}: shortlisted={updated[0]['shortlisted']}")
        return {"success": True, "employee": updated[0], "employees": updated, "updatedCount": len(updated)}
    except Exception as err:
        write_log("employee", "SHORTLIST_EMPLOYEE_FAILED", "failed", f"Failed to toggle shortlist for employee ID {employee_id or 'unknown'}")
        return public_error(err, "Failed to update shortlist")


@router.patch("")
def update_employee(body: dict = Body(default_factory=dict)):
    employee_id = str(body.get("employeeId") or "").strip()
    updates = body.get("updates") if isinstance(body.get("updates"), dict) else {}
    if not employee_id:
        return json_error(400, "Employee ID is required")
    if {"score", "score_override", "score_override_jd_id"}.intersection(updates):
        return json_error(400, "Score is calculated automatically and cannot be edited.")
    try:
        roster = corp_pool.load_roster()
        if not roster:
            return json_error(404, "Employees not loaded")
        matched = next((e for e in roster if e["employee_id"] == employee_id), None)
        if not matched:
            return json_error(404, "Employee not found")
        next_id = str(updates.get("employee_id", matched["employee_id"]) or "").strip()
        if not next_id:
            return json_error(400, "Employee ID cannot be empty.")
        if next_id != employee_id and any(e["employee_id"] == next_id for e in roster):
            return json_error(409, f"Employee ID {next_id} already exists.")
        for field in EDITABLE:
            if field in updates:
                matched[field] = "" if updates[field] is None else str(updates[field])
        matched["employee_id"] = next_id
        matched["manually_edited"] = True
        corp_pool.save_roster(roster)
        write_log("employee", "UPDATE_CORP_POOL_EMPLOYEE", "success",
                  f"Updated Corp Pool employee {employee_id}{f' -> {next_id}' if next_id != employee_id else ''}")
        return {"success": True, "employee": matched}
    except Exception as err:
        write_log("employee", "UPDATE_CORP_POOL_EMPLOYEE_FAILED", "failed", f"Failed to update Corp Pool employee {employee_id or 'unknown'}")
        return public_error(err, "Failed to update employee")


@router.delete("")
def delete_employees(body: dict = Body(default_factory=dict)):
    employee_id, ids = body.get("employeeId"), body.get("ids")
    if not employee_id and not ids:
        return json_error(400, "Employee ID or IDs array is required")
    targets = [str(i) for i in (ids or [employee_id])]
    try:
        roster = corp_pool.load_roster()
        if not roster:
            return json_error(404, "Employees not loaded")
        wanted = set(targets)
        removed = [e for e in roster if e["employee_id"] in wanted]
        remaining = [e for e in roster if e["employee_id"] not in wanted]
        if not removed:
            return json_error(404, "Employee not found")
        corp_pool.save_roster(remaining)
        personal = lambda f: f.lower().endswith((".pdf", ".docx", ".doc", ".txt"))
        files = {str(e.get("source_file") or "").strip() for e in removed if personal(str(e.get("source_file") or ""))}
        stored = docs_storage.list_files("Corp Pool")
        for emp in removed:
            ident = str(emp["employee_id"]).strip().lower()
            if not ident or ident.startswith("cv") and len(ident) > 9 and all(c in "0123456789abcdef" for c in ident[2:]):
                continue
            files.update(f for f in stored if personal(f) and ident in f.lower())
        for name in files:
            if name:
                docs_storage.delete_file("Corp Pool", name)
        corp_pool.mark_deleted([e["employee_id"] for e in removed])
        write_log("employee", "DELETE_EMPLOYEE", "success",
                  f"Deleted employee IDs: {', '.join(targets)}{'; removed files: ' + ', '.join(sorted(files)) if files else ''}")
        return {"success": True, "deleted": len(removed), "employees": remaining}
    except Exception as err:
        write_log("employee", "DELETE_EMPLOYEE_FAILED", "failed", "Failed to delete employees")
        return public_error(err, "Failed to delete employees")


@router.get("/mapping-questions")
def mapping_questions(request: Request):
    employee_id = (request.query_params.get("employeeId") or "").strip()
    if not employee_id:
        return json_error(400, "employeeId is required")
    try:
        return {"employeeId": employee_id, "assigned_questions": pr.assigned_questions_for(employee_id)}
    except Exception as err:
        return public_error(err, "Failed to load questions")


def _resolve_test_id(test_id: str, employee_id: str) -> str:
    if test_id:
        return test_id if as_uuid_ok(test_id) else ""
    if not employee_id:
        return ""
    store = get_store()
    emp = store.first("employees", {"employee_id": employee_id}, ["id"])
    if not emp:
        return ""
    row = store.first("tests", {"employee_id": emp["id"], "topic_id": pr.PRODUCT_TOPIC}, ["id"])
    return row["id"] if row else ""


def as_uuid_ok(value: str) -> bool:
    return bool(pr.UUID_RE.match(value or ""))


@router.post("/reset-test", dependencies=[Depends(require_permission("employeePortal"))])
def reset_test(body: dict = Body(default_factory=dict)):
    test_id, employee_id = str(body.get("testId") or "").strip(), str(body.get("employeeId") or "").strip()
    if not test_id and not employee_id:
        return json_error(400, "testId or employeeId is required")
    try:
        resolved = _resolve_test_id(test_id, employee_id)
        if not resolved:
            return json_error(404, "Assigned test not found for this employee")
        preserved = len(et.questions_for(resolved))
        test_video.delete(resolved)
        et.reset_test(resolved)
        if employee_id:
            try:
                get_store().update("employees", {"ai_readiness_score": 0, "xp_points": 0, "skill_level": "beginner"}, {"employee_id": employee_id})
            except Exception as err:
                log.warning("Failed to reset employee counters: %s", err)
        test_video.delete(resolved)
        write_log("employee", "ADMIN_RESET_EMPLOYEE_TEST", "success",
                  f"Admin reset test {resolved}{f' for employee {employee_id}' if employee_id else ''} (questions preserved: {preserved})")
        return {"success": True, "testId": resolved, "preservedQuestions": preserved}
    except Exception as err:
        write_log("employee", "ADMIN_RESET_EMPLOYEE_TEST_FAILED", "failed", "Admin reset test failed")
        return public_error(err, "Internal error")


@router.post("/delete-video", dependencies=[Depends(require_permission("employeePortal"))])
def delete_video(body: dict = Body(default_factory=dict)):
    try:
        items = body.get("items") if isinstance(body.get("items"), list) else None
        if items:
            deleted, skipped = [], []
            for item in items:
                item = item if isinstance(item, dict) else {}
                test_id, employee_id = str(item.get("testId") or "").strip(), str(item.get("employeeId") or "").strip()
                resolved = _resolve_test_id(test_id, employee_id)
                if not resolved:
                    skipped.append({"employeeId": employee_id or None, "testId": test_id or None, "error": "Assigned test not found"})
                    continue
                try:
                    test_video.delete(resolved)
                    deleted.append(resolved)
                except Exception as err:
                    skipped.append({"employeeId": employee_id or None, "testId": resolved, "error": str(err) or "Delete failed"})
            write_log("employee", "ADMIN_BULK_DELETE_EMPLOYEE_TEST_VIDEOS", "success" if deleted else "failed",
                      f"Bulk deleted {len(deleted)} proctoring video(s); skipped {len(skipped)}")
            return {"success": bool(deleted), "deletedCount": len(deleted), "skippedCount": len(skipped), "deletedTestIds": deleted, "skipped": skipped,
                    "message": f"{len(deleted)} proctoring video(s) deleted. Test scores and status unchanged." if deleted else "No proctoring videos were deleted."}
        test_id, employee_id = str(body.get("testId") or "").strip(), str(body.get("employeeId") or "").strip()
        if not test_id and not employee_id:
            return json_error(400, "testId or employeeId is required")
        resolved = _resolve_test_id(test_id, employee_id)
        if not resolved:
            return json_error(404, "Assigned test not found for this employee")
        test_video.delete(resolved)
        write_log("employee", "ADMIN_DELETE_EMPLOYEE_TEST_VIDEO", "success",
                  f"Admin deleted proctoring video only for test {resolved}{f' (employee {employee_id})' if employee_id else ''}")
        return {"success": True, "testId": resolved, "message": "Proctoring video deleted. Test score and status unchanged."}
    except Exception as err:
        write_log("employee", "ADMIN_DELETE_EMPLOYEE_TEST_VIDEO_FAILED", "failed", "Admin delete video failed")
        return public_error(err, "Internal error")


def _recipients(raw: list) -> list[dict]:
    out = []
    for item in raw:
        item = item if isinstance(item, dict) else {}
        row = {"employee_id": str(item.get("employee_id") or "").strip(), "email": str(item.get("email") or "").strip(),
               "full_name": str(item.get("full_name") or item.get("fullName") or "").strip() or "Employee",
               "department": item.get("department"), "skills": item.get("skills"), "grade": item.get("grade"),
               "designation": item.get("designation"), "score": item.get("score")}
        if row["employee_id"] and row["email"]:
            out.append(row)
    return out


@router.post("/dispatch_mail")
def dispatch_mail(request: Request, body: dict = Body(default_factory=dict)):
    import uuid
    from html import escape

    from core.config import app_url
    from core.security import get_client_ip
    from services import audit, mailer, resumes, sessions
    from services.dashboard_sync import bump_sync
    from services.invite_mail import build_invite_html

    ip = get_client_ip(request)
    try:
        requester = acting_admin_email(request) or "unknown"
        portal_mode = body.get("portal") is True
        origin = app_url()
        portal_url = f"{origin}/employee?org=BizX" if portal_mode else origin
        if portal_mode and isinstance(body.get("recipients"), list) and body["recipients"]:
            targets = _recipients(body["recipients"])
        else:
            roster = corp_pool.load_roster()
            if not roster:
                return json_error(404, "Employee pool not loaded")
            shortlisted = [e for e in roster if e.get("shortlisted")]
            wanted = body.get("employeeIds")
            if isinstance(wanted, list) and wanted:
                ids = {str(i) for i in wanted}
                shortlisted = [e for e in shortlisted if str(e["employee_id"]) in ids]
            targets = _recipients(shortlisted)
        if not targets:
            return json_error(400, "No target employees found to dispatch invite emails.")
        dispatched = []
        for emp in targets:
            if not portal_mode:
                resume_id = str(uuid.uuid4())
                score = emp.get("score") or 50
                resumes.save_row({
                    "id": resume_id, "filename": f"Employee_{emp['employee_id']}.txt",
                    "originalText": f"Employee Profile\nName: {emp['full_name']}\nID: {emp['employee_id']}\nDept: {emp.get('department')}\nSkills: {emp.get('skills')}",
                    "parsed": {"personal": {"fullName": emp["full_name"], "email": emp["email"], "phone": ""},
                               "skills": {"technical": [emp["skills"]] if emp.get("skills") else [], "soft": []}, "experience": [], "education": []},
                    "analysis": {"executiveSummary": f"Employee profile matched for internal screening. Grade: {emp.get('grade') or 'N/A'}, Designation: {emp.get('designation') or 'N/A'}.",
                                 "overallScore": score, "atsScore": score, "technicalScore": score, "jdMatchScore": score},
                    "enhanced": None, "report": {"rmEmail": requester, "verification": None}, "error": None,
                    "fileHash": f"emp_{emp['employee_id']}_hash", "fileBase64": None})
                candidate_password = sessions.issue_candidate_password(emp["email"], resume_id)
            if portal_mode:
                from services.portal_invite_mail import build_portal_invite_html

                subject = "Product Assessment Assignment - Employee Learning Portal"
                html = build_portal_invite_html(
                    emp["full_name"],
                    emp["employee_id"],
                    temporary_password=emp["employee_id"],
                    portal_url=origin if origin.endswith("talentscope.infinite.com") else "https://talentscope.infinite.com/",
                    deadline=(body.get("deadline") if isinstance(body, dict) else None),
                    product_hint=(body.get("productHint") if isinstance(body, dict) else None) or emp.get("department") or "for example, HLR-HSS",
                )
            else:
                subject = "Assessment Invitation - TalentScope"
                hint = f"Login email address: <strong>{escape(emp['email'])}</strong>"
                html = build_invite_html(
                    emp["full_name"], hint, portal_url, "technical", candidate_password,
                )
            status, send_error = mailer.dispatch(emp["email"], subject, html)
            mailer.record(emp["email"], emp["full_name"], subject, html, status, requester)
            audit.add_log(requester, "ADMIN_INVITE_PORTAL_EMPLOYEE" if portal_mode else "ADMIN_INVITE_EMPLOYEE", emp["email"],
                          f"Dispatched {'employee portal' if portal_mode else 'internal'} assessment invite to {emp['full_name']} ({emp['employee_id']}). Status: {status}.", ip)
            dispatched.append({"email": emp["email"], "name": emp["full_name"], "status": status, "error": send_error})
        write_log("employee", "DISPATCH_PORTAL_EMPLOYEE_MAILS" if portal_mode else "DISPATCH_EMPLOYEE_MAILS", "success",
                  f"Successfully dispatched assessment invitations to {len(dispatched)} employees.")
        bump_sync("dispatch_mail")
        return {"success": True, "count": len(dispatched), "dispatched": dispatched}
    except Exception as err:
        write_log("employee", "DISPATCH_EMPLOYEE_MAILS_FAILED", "failed", "Failed to dispatch internal emails")
        return public_error(err, "Failed to dispatch emails")


_active_scan = {"id": ""}
MAX_BATCH = 500


@router.post("/llm-evaluate")
def llm_evaluate(body: dict = Body(default_factory=dict)):
    import time

    from services import skill_match

    try:
        jd_id = str(body.get("jdId") or "").strip()
        if not jd_id or jd_id == "all" or "@" in jd_id:
            return json_error(400, "Select one requirement first.")
        jd = get_store().first("job_descriptions", {"id": jd_id}, ["id", "file_name", "jd_text"]) if pr.UUID_RE.match(jd_id) else None
        if not jd or not jd.get("jd_text"):
            return json_error(404, "Could not load that job description.")
        roster = corp_pool.load_roster()
        if not roster:
            return json_error(400, "Corp Pool is empty.")
        wanted = [str(i or "").strip() for i in (body.get("employeeIds") or []) if str(i or "").strip()]
        if not wanted:
            return json_error(400, "Select a pool of people first, then Analyze.")
        id_set = set(wanted)
        targets = [e for e in roster if e["employee_id"] in id_set]
        if not targets:
            return json_error(400, "None of the selected pool people are in Corp Pool.")
        force = body.get("force") is not False
        try:
            offset = max(0, int(body.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        has_limit = body.get("limit") not in (None, "")
        try:
            limit = min(MAX_BATCH, max(1, int(body.get("limit") or 250))) if has_limit else min(MAX_BATCH, len(targets))
        except (TypeError, ValueError):
            limit = 250
        scan_id = str(body.get("scanId") or "").strip()
        if offset == 0 or not _active_scan["id"]:
            _active_scan["id"] = scan_id or f"{jd_id}-{int(time.time() * 1000)}"
        elif scan_id and scan_id != _active_scan["id"]:
            return {"success": True, "aborted": True, "jdFileName": jd.get("file_name"), "scored": 0, "skipped": 0, "failed": 0,
                    "total": len(targets), "offset": offset, "nextOffset": offset, "done": False, "results": [], "elapsedMs": 0}
        batch = targets[offset:offset + limit]
        started = time.time()
        to_score = [e for e in batch if force or not (e.get("score_override_jd_id") == jd_id and isinstance(e.get("score_override"), (int, float)))]
        skipped = len(batch) - len(to_score)
        results, errors = [], []
        try:
            scored = skill_match.score_people(jd["jd_text"], to_score)
        except skill_match.SkillMatchError as err:
            return public_error(err, "Analyze failed")
        for emp, res in zip(to_score, scored):
            emp["score"] = res["score"]
            emp["score_override"] = res["score"]
            emp["score_override_jd_id"] = jd_id
            if res.get("matchingSkills"):
                emp["matchingSkills"] = res["matchingSkills"]
            emp["llm_rationale"] = res.get("rationale")
            emp["llm_best_jd"] = jd.get("file_name") or "JD"
            emp["llm_best_jd_why"] = res.get("rationale")
            results.append({"employee_id": emp["employee_id"], "full_name": emp.get("full_name"), "score": res["score"], "decision": res["decision"],
                            "rationale": res["rationale"], "bestJdFileName": jd.get("file_name") or "JD", "bestJdWhy": res["rationale"],
                            "matchingSkills": res.get("matchingSkills") or [], "engine": "chips"})
        if results or skipped or errors:
            corp_pool.save_roster(roster)
        next_offset = offset + len(batch)
        return {"success": True, "jdFileName": jd.get("file_name"), "scored": len(results), "skipped": skipped, "failed": len(errors),
                "errors": errors[:8], "total": len(targets), "offset": offset, "nextOffset": next_offset, "done": next_offset >= len(targets),
                "results": results, "elapsedMs": int((time.time() - started) * 1000)}
    except Exception as err:
        return public_error(err, "Analyze failed")


def _xlsx(data: bytes, name: str) -> Response:
    from services.exports import XLSX_MIME

    return Response(data, media_type=XLSX_MIME, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _production_portal_export(request: Request, export_name: str) -> Optional[Response]:
    base_url = env("PORTAL_EXPORT_API_URL").rstrip("/")
    if not base_url:
        if env("PORTAL_EXPORT_REQUIRE_PRODUCTION").lower() in {"1", "true", "yes", "on"}:
            log.error("PORTAL_EXPORT_API_URL is required for development portal exports")
            return json_error(503, "Production portal exports are not configured.")
        return None
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        log.error("PORTAL_EXPORT_API_URL must be an HTTPS URL without credentials, query, or fragment")
        return json_error(503, "Production portal exports are not configured correctly.")
    if parsed.hostname and parsed.hostname.casefold() == (request.url.hostname or "").casefold():
        log.error("PORTAL_EXPORT_API_URL points back to the current API host")
        return json_error(503, "Production portal exports are not configured correctly.")

    authorization = request.headers.get("authorization")
    if not authorization:
        return json_error(401, "Unauthorized")

    try:
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            upstream = client.get(
                f"{base_url}/api/admin/employees/{export_name}",
                headers={"Authorization": authorization},
            )
    except httpx.HTTPError as err:
        log.error("Production portal export request failed for %s: %s", export_name, err)
        return json_error(502, "Production portal export is unavailable.")

    if 300 <= upstream.status_code < 400:
        log.error("Production portal export unexpectedly redirected for %s", export_name)
        return json_error(502, "Production portal export is unavailable.")
    headers = {
        name: upstream.headers[name]
        for name in ("content-type", "content-disposition")
        if name in upstream.headers
    }
    return Response(content=upstream.content, status_code=upstream.status_code, headers=headers)


@router.post("/export-shortlisted")
def export_shortlisted(body: dict = Body(default_factory=dict)):
    import re
    from datetime import date

    from services import exports

    try:
        rows = [r for r in (body.get("employees") if isinstance(body.get("employees"), list) else []) if isinstance(r, dict) and str(r.get("employee_id") or "").strip()]
        if not rows:
            return json_error(400, "Shortlist at least one Corp Pool person before exporting.")
        requested = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "-", str(body.get("fileName") or "")).replace('"', "").strip()
        name = requested if requested.lower().endswith(".xlsx") else f"{requested}.xlsx" if requested else f"corp_pool_shortlisted_{date.today().isoformat()}.xlsx"
        return _xlsx(exports.shortlisted_workbook(rows), name)
    except Exception as err:
        return public_error(err, "Export failed")


@router.get("/export-portal", dependencies=[Depends(require_permission("employeePortal"))])
def export_portal(request: Request):
    from datetime import date

    from services import exports

    email = acting_admin_email(request) or ""
    if email and not admin_accounts.get_access(email)["canViewEmployeePortal"]:
        return json_error(403, "Employee Portal is not available for this login.")
    try:
        production_export = _production_portal_export(request, "export-portal")
        if production_export is not None:
            return production_export
        results = _test_results(get_store())
        employees = pr.build_portal_employees(results)
        done = [a["test_id"] for a in employees if a.get("test_id") and a.get("test_status") == "completed"]
        return _xlsx(exports.portal_workbook(employees, et.question_attempts_batch(done)), f"employee_portal_test_results_{date.today().isoformat()}.xlsx")
    except Exception as err:
        return public_error(err, "Export failed")


@router.get("/export-portal-summary", dependencies=[Depends(require_permission("employeePortal"))])
def export_portal_summary(request: Request):
    from datetime import date

    from services import exports

    email = acting_admin_email(request) or ""
    if email and not admin_accounts.get_access(email)["canViewEmployeePortal"]:
        return json_error(403, "Employee Portal is not available for this login.")
    try:
        production_export = _production_portal_export(request, "export-portal-summary")
        if production_export is not None:
            return production_export
        results = _test_results(get_store())
        employees = pr.build_portal_employees(results)
        done = [a["test_id"] for a in employees if a.get("test_id") and a.get("test_status") == "completed"]
        source_name = f"employee_portal_test_results_{date.today().isoformat()}.xlsx"
        detail_workbook = exports.portal_workbook(employees, et.question_attempts_batch(done))
        data = exports.portal_summary_workbook(detail_workbook, source_name)
        return _xlsx(data, f"employee_portal_summary_{date.today().isoformat()}.xlsx")
    except Exception as err:
        return public_error(err, "Summary export failed")


@router.post("/percentage-matching")
async def percentage_matching(request: Request):
    from services import exports

    try:
        form = await request.form()
        uploads = [f for key in ("files", "file") for f in form.getlist(key) if hasattr(f, "read")]
        files = []
        for upload in uploads:
            data = await upload.read()
            if data:
                files.append((upload.filename or "file", data))
        if not files:
            return json_error(400, "Upload one or more JD/BR vs Corp Pool score workbooks.")
        rows = []
        for name, data in files:
            parsed = exports.parse_match_workbook(data, name)
            if not parsed["scores"]:
                return json_error(400, f"No Match Score column/rows found in {name}.")
            rows.append(parsed)
        return _xlsx(exports.percentage_workbook(rows), "Percentage summary.xlsx")
    except Exception as err:
        return public_error(err, "Failed to build percentage matching file")
