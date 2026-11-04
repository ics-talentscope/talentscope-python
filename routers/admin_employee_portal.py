import html
import logging

from fastapi import APIRouter, Body, Depends, File, Form, Request, UploadFile

from core.auth import acting_admin_email, require_permission
from core.db import get_store
from core.errors import json_error, public_error
from core.security import get_client_ip
from services import accounts, audit, employee_portal_admin as portal_admin, mailer

log = logging.getLogger("talentscope")
router = APIRouter(
    prefix="/api/admin/employee-portal",
    tags=["admin-employee-portal"],
    dependencies=[Depends(require_permission("portalConfig"))],
)
UPLOAD_MAX_BYTES = 10 * 1024 * 1024


def _employee_rows() -> list[dict]:
    rows = get_store().select(
        "employees",
        {"role": "employee", "product_qb_eligible": True},
        ["employee_id", "full_name", "email", "email_verified", "designation", "product", "is_first_login"],
        "full_name asc",
    )
    return [{
        "employeeId": row["employee_id"], "name": row["full_name"], "email": row["email"],
        "emailVerified": bool(row.get("email_verified")),
        "designation": row.get("designation") or "", "product": row.get("product") or "",
        "firstLogin": bool(row.get("is_first_login")),
    } for row in rows]


def _record_result(row: dict, actor: str, request: Request) -> dict:
    try:
        result = portal_admin.provision_employee(row)
        audit.add_log(actor, "EMPLOYEE_PORTAL_USER_PROVISIONED", "Employee Portal",
                      f"{'Created' if result['created'] else 'Updated'} {result['employeeId']} and assigned "
                      f"{result['questionCount']} questions for {result['product']}.", get_client_ip(request))
        return {"success": True, **result}
    except (ValueError, RuntimeError) as err:
        return {"success": False, "employeeId": str(row.get("employee_id") or ""), "error": str(err)}


@router.get("")
def overview():
    try:
        return {"employees": _employee_rows(), "products": portal_admin.product_configs()}
    except Exception as err:
        return public_error(err, "Failed to load employee portal management")


@router.post("/users")
def create_user(request: Request, body: dict = Body(default_factory=dict)):
    actor = acting_admin_email(request) or "unknown"
    result = _record_result({
        "employee_id": body.get("employeeId") or body.get("employee_id"),
        "full_name": body.get("name") or body.get("full_name"),
        "email": body.get("email"), "designation": body.get("designation") or body.get("role"),
        "product": body.get("product"),
    }, actor, request)
    if not result["success"]:
        return json_error(400, result["error"])
    return result


@router.patch("/users/{employee_id}")
def update_user(employee_id: str, request: Request, body: dict = Body(default_factory=dict)):
    actor = acting_admin_email(request) or "unknown"
    account = accounts.get_account(employee_id)
    if not account or account.get("role") != "employee" or not account.get("product_qb_eligible"):
        return json_error(404, "Employee portal user not found")
    result = _record_result({
        "employee_id": employee_id,
        "full_name": body.get("name", body.get("full_name", account.get("full_name"))),
        "email": body.get("email", account.get("email")),
        "designation": body.get("designation", account.get("designation")),
        "department": body.get("department", account.get("department")),
        "product": body.get("product", account.get("product")),
    }, actor, request)
    if not result["success"]:
        return json_error(400, result["error"])
    audit.add_log(actor, "EMPLOYEE_PORTAL_PROFILE_UPDATED", "Employee Portal",
                  f"Updated employee portal profile for {employee_id}.", get_client_ip(request))
    return result


@router.post("/users/{employee_id}/reset-password")
def reset_user_password(employee_id: str, request: Request):
    account = accounts.get_account(employee_id)
    if not account or account.get("role") != "employee" or not account.get("product_qb_eligible"):
        return json_error(404, "Employee portal user not found")
    email = str(account.get("email") or "").strip()
    if not email:
        return json_error(400, "The employee has no registered email address. Update their record before resetting the password.")

    password_hash, password_salt = accounts.hash_password(account["employee_id"])
    account.update({
        "password_hash": password_hash,
        "password_salt": password_salt,
        "is_first_login": False,
    })
    if not accounts.upsert_account(account):
        return json_error(500, "Password reset could not be saved.")

    subject = "TalentScope Employee Portal password reset"
    employee_name = html.escape(account.get("full_name") or account["employee_id"])
    safe_employee_id = html.escape(account["employee_id"])
    body = (
        f"<p>Hello {employee_name},</p>"
        "<p>An administrator has reset your TalentScope Employee Portal password.</p>"
        f"<p><strong>Employee ID:</strong> {safe_employee_id}<br>"
        f"<strong>Temporary password:</strong> {safe_employee_id}</p>"
        "<p>Sign in using your Employee ID as the temporary password. For your security, we recommend changing it after signing in.</p>"
        "<p>If you did not expect this change, please contact your administrator.</p>"
    )
    status, send_error = mailer.dispatch(email, subject, body)
    actor = acting_admin_email(request) or "unknown"
    ip = get_client_ip(request)
    audit.add_log(actor, "EMPLOYEE_PORTAL_PASSWORD_RESET", account["employee_id"],
                  "Reset employee portal password to the Employee ID and recommended changing it after signing in.", ip)
    if status != "sent":
        log.error("Employee portal password reset email failed for %s: %s", employee_id, send_error or status)
        return {
            "success": True,
            "emailSent": False,
            "message": "Password reset successfully, but the notification email could not be delivered. Contact the employee through another channel.",
        }
    return {"success": True, "emailSent": True, "message": "Password reset and notification email sent."}


@router.post("/users/bulk")
async def bulk_users(request: Request, file: UploadFile = File(...)):
    actor = acting_admin_email(request) or "unknown"
    try:
        content = await file.read(UPLOAD_MAX_BYTES + 1)
        if len(content) > UPLOAD_MAX_BYTES:
            return json_error(413, "Roster file must be 10 MB or smaller.")
        rows = portal_admin.parse_employee_upload(content, file.filename or "")
        seen: set[str] = set()
        results = []
        for row in rows:
            key = str(row.get("employee_id") or "").strip().casefold()
            if key in seen:
                results.append({"success": False, "employeeId": row.get("employee_id"), "error": "Duplicate Employee ID in upload.",
                                "row": row["_row"]})
                continue
            seen.add(key)
            result = _record_result(row, actor, request)
            result["row"] = row["_row"]
            results.append(result)
        return {
            "results": results, "created": sum(1 for row in results if row["success"] and row["created"]),
            "updated": sum(1 for row in results if row["success"] and not row["created"]),
            "failed": sum(1 for row in results if not row["success"]),
        }
    except ValueError as err:
        return json_error(400, str(err))
    except Exception as err:
        log.exception("Employee portal roster upload failed")
        return public_error(err, "Failed to process employee roster")


@router.post("/question-banks")
async def upload_question_bank(
    request: Request,
    product: str = Form(...),
    question_count: int = Form(...),
    sheet_name: str = Form(""),
    file: UploadFile = File(...),
):
    actor = acting_admin_email(request) or "unknown"
    try:
        content = await file.read(UPLOAD_MAX_BYTES + 1)
        if len(content) > UPLOAD_MAX_BYTES:
            return json_error(413, "Question bank file must be 10 MB or smaller.")
        questions = portal_admin.parse_question_bank(content, file.filename or "", sheet_name.strip())
        config = portal_admin.save_question_bank(product.strip(), question_count, questions)
        audit.add_log(actor, "EMPLOYEE_PORTAL_QUESTION_BANK_UPLOADED", "Employee Portal",
                      f"Uploaded {len(questions)} questions for {config['product']}; {question_count} assigned per employee.",
                      get_client_ip(request))
        return {"success": True, "product": config}
    except ValueError as err:
        return json_error(400, str(err))
    except Exception as err:
        log.exception("Employee portal question-bank upload failed")
        return public_error(err, "Failed to upload question bank")
