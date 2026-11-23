"""Slalom-only bootcamp management API.

The routes in this module are the backend counterpart to the Slalom bootcamp
admin screen. Authorisation is deliberately separate from the shared employee
portal config permission: every route requires a Slalom admin with the
``bootcampManagement`` permission.
"""

import logging
from functools import wraps

from fastapi import APIRouter, Body, Depends, Request

from core.auth import require_slalom_bootcamp_admin
from core.db import get_store
from core.errors import json_error, public_error
from core.security import get_client_ip
from services import audit, employee_portal_admin, slalom_bootcamp as bootcamp

log = logging.getLogger("talentscope")

BASE = "/api/admin/slalom/bootcamp"
ALIASES = (
    "/api/admin/slalom/bootcamps",
    "/api/admin/slalom-bootcamp",
    "/api/admin/bootcamp",
)


def _first(body: dict, *names, default=None):
    for name in names:
        if isinstance(body, dict) and body.get(name) is not None:
            return body[name]
    return default


def _collection(body: dict, *names) -> list:
    value = _first(body, *names, default=None)
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    if value not in (None, ""):
        return [value]
    return []


def _content_type(value) -> str:
    raw = str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")
    return bootcamp.CONTENT_TYPE_ALIASES.get(raw, raw if raw in bootcamp.CONTENT_TYPES else "")


def _audit(admin: str, request: Request, action: str, detail: str) -> None:
    try:
        audit.add_log(admin, action, "Slalom Bootcamp", detail, get_client_ip(request))
    except Exception as err:
        log.warning("Could not write bootcamp audit log: %s", err)


def _guard(fallback: str):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except PermissionError as err:
                return json_error(403, str(err) or "Slalom bootcamp access is required.")
            except LookupError as err:
                return json_error(404, str(err) or "Bootcamp record not found.")
            except ValueError as err:
                return json_error(400, str(err) or "Invalid bootcamp request.")
            except Exception as err:
                return public_error(err, fallback)

        return wrapper

    return decorator


def _body_for_update(existing: dict, body: dict) -> dict:
    """Merge a partial PATCH body over the stored read model."""
    return {**existing, **body}


def build_router(prefix: str = BASE, tag: str = "slalom-bootcamp") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[tag])

    @router.get("")
    @_guard("Failed to load bootcamp management data")
    def overview(request: Request, admin: str = Depends(require_slalom_bootcamp_admin)):
        return {
            "assessments": bootcamp.list_assessments(),
            "cohorts": bootcamp.list_cohorts(),
            "content": bootcamp.list_content(),
            "leaderboard": bootcamp.leaderboard()["entries"],
        }

    # ------------------------------------------------------------------ #
    # Assessments
    # ------------------------------------------------------------------ #

    @router.get("/assessments")
    @_guard("Failed to load bootcamp assessments")
    def list_assessments(request: Request, active_only: bool = False,
                         admin: str = Depends(require_slalom_bootcamp_admin)):
        items = bootcamp.list_assessments(active_only=active_only)
        return {"assessments": items, "items": items}

    @router.post("/assessments")
    @_guard("Failed to save bootcamp assessment")
    def save_assessment(request: Request, body: dict = Body(default_factory=dict),
                        admin: str = Depends(require_slalom_bootcamp_admin)):
        result = bootcamp.save_assessment(body, admin)
        _audit(admin, request, "SLALOM_BOOTCAMP_ASSESSMENT_SAVED", f"Saved assessment {result['id']}.")
        return {"success": True, **result, "assessment": result}

    @router.get("/assessments/{assessment_id}")
    @_guard("Failed to load bootcamp assessment")
    def get_assessment(assessment_id: str, request: Request,
                       admin: str = Depends(require_slalom_bootcamp_admin)):
        result = bootcamp.map_assessment(bootcamp.get_assessment(assessment_id))
        return {"success": True, **result, "assessment": result}

    @router.patch("/assessments/{assessment_id}")
    @_guard("Failed to update bootcamp assessment")
    def update_assessment(assessment_id: str, request: Request, body: dict = Body(default_factory=dict),
                          admin: str = Depends(require_slalom_bootcamp_admin)):
        existing = bootcamp.map_assessment(bootcamp.get_assessment(assessment_id))
        payload = _body_for_update(existing, body)
        action = str(body.get("action") or "").strip().casefold()
        if action in ("publish", "published"):
            payload["status"] = "published"
            payload["isActive"] = True
        elif action in ("close", "closed"):
            payload["status"] = "closed"
            payload["isActive"] = False
        elif action == "draft":
            payload["status"] = "draft"
        result = bootcamp.save_assessment(payload, admin)
        _audit(admin, request, "SLALOM_BOOTCAMP_ASSESSMENT_UPDATED", f"Updated assessment {assessment_id}.")
        return {"success": True, **result, "assessment": result}

    @router.delete("/assessments/{assessment_id}")
    @_guard("Failed to delete bootcamp assessment")
    def delete_assessment(assessment_id: str, request: Request,
                          admin: str = Depends(require_slalom_bootcamp_admin)):
        deleted = bootcamp.delete_assessment(assessment_id)
        _audit(admin, request, "SLALOM_BOOTCAMP_ASSESSMENT_DELETED", f"Deleted assessment {assessment_id}.")
        return {"success": True, "deleted": deleted}

    @router.post("/assessments/{assessment_id}/assign")
    @_guard("Failed to assign bootcamp assessment")
    def assign_assessment(assessment_id: str, request: Request, body: dict = Body(default_factory=dict),
                          admin: str = Depends(require_slalom_bootcamp_admin)):
        cohort_id = str(_first(body, "cohortId", "cohort_id", default="") or "").strip()
        employees = _collection(body, "employeeIds", "employee_ids", "employees", "members")
        single = _first(body, "employeeId", "employee_id", default="")
        if single not in (None, ""):
            employees.append(single)
        results = bootcamp.assign_assessment(assessment_id, employees, cohort_id, admin)
        _audit(admin, request, "SLALOM_BOOTCAMP_ASSESSMENT_ASSIGNED",
               f"Assigned assessment {assessment_id} to {len(results)} employee(s).")
        return {"success": True, "assignments": results, "assigned": len(results)}

    # ------------------------------------------------------------------ #
    # Cohorts and roster
    # ------------------------------------------------------------------ #

    @router.get("/cohorts")
    @_guard("Failed to load bootcamp cohorts")
    def list_cohorts(request: Request, admin: str = Depends(require_slalom_bootcamp_admin)):
        items = bootcamp.list_cohorts()
        return {"cohorts": items, "items": items}

    @router.post("/cohorts")
    @_guard("Failed to save bootcamp cohort")
    def save_cohort(request: Request, body: dict = Body(default_factory=dict),
                    admin: str = Depends(require_slalom_bootcamp_admin)):
        result = bootcamp.save_cohort(body, admin)
        _audit(admin, request, "SLALOM_BOOTCAMP_COHORT_SAVED", f"Saved cohort {result['id']}.")
        return {"success": True, **result, "cohort": result}

    @router.get("/cohorts/{cohort_id}")
    @_guard("Failed to load bootcamp cohort")
    def get_cohort(cohort_id: str, request: Request,
                   admin: str = Depends(require_slalom_bootcamp_admin)):
        result = bootcamp.map_cohort(bootcamp.get_cohort(cohort_id))
        result["members"] = bootcamp.list_members(cohort_id)
        return {"success": True, **result, "cohort": result}

    @router.patch("/cohorts/{cohort_id}")
    @_guard("Failed to update bootcamp cohort")
    def update_cohort(cohort_id: str, request: Request, body: dict = Body(default_factory=dict),
                      admin: str = Depends(require_slalom_bootcamp_admin)):
        existing = bootcamp.map_cohort(bootcamp.get_cohort(cohort_id))
        result = bootcamp.save_cohort(_body_for_update(existing, body), admin)
        _audit(admin, request, "SLALOM_BOOTCAMP_COHORT_UPDATED", f"Updated cohort {cohort_id}.")
        return {"success": True, **result, "cohort": result}

    @router.delete("/cohorts/{cohort_id}")
    @_guard("Failed to delete bootcamp cohort")
    def delete_cohort(cohort_id: str, request: Request,
                      admin: str = Depends(require_slalom_bootcamp_admin)):
        deleted = bootcamp.delete_cohort(cohort_id)
        _audit(admin, request, "SLALOM_BOOTCAMP_COHORT_DELETED", f"Deleted cohort {cohort_id}.")
        return {"success": True, "deleted": deleted}

    @router.get("/cohorts/{cohort_id}/members")
    @_guard("Failed to load cohort members")
    def list_members(cohort_id: str, request: Request,
                     admin: str = Depends(require_slalom_bootcamp_admin)):
        return {"members": bootcamp.list_members(cohort_id)}

    @router.post("/cohorts/{cohort_id}/members/bulk")
    @_guard("Failed to add cohort members")
    def add_members(cohort_id: str, request: Request, body: dict = Body(default_factory=dict),
                    admin: str = Depends(require_slalom_bootcamp_admin)):
        employees = _collection(body, "members", "employeeIds", "employee_ids", "employees")
        results = bootcamp.add_members(cohort_id, employees, admin)
        _audit(admin, request, "SLALOM_BOOTCAMP_MEMBERS_ADDED",
               f"Added {len(results)} member(s) to cohort {cohort_id}.")
        return {"success": True, "members": results, "added": len(results)}

    @router.delete("/cohorts/{cohort_id}/members")
    @_guard("Failed to remove cohort member")
    def remove_member(cohort_id: str, request: Request, body: dict = Body(default_factory=dict),
                      admin: str = Depends(require_slalom_bootcamp_admin)):
        employee_id = str(_first(body, "employeeId", "employee_id", default="") or "").strip()
        if not employee_id:
            employee_id = str(request.query_params.get("employee_id") or "").strip()
        if not employee_id:
            return json_error(400, "An Employee ID is required.")
        deleted = bootcamp.remove_member(cohort_id, employee_id)
        return {"success": True, "deleted": deleted}

    # ------------------------------------------------------------------ #
    # Assignments
    # ------------------------------------------------------------------ #

    @router.get("/assignments")
    @_guard("Failed to load bootcamp assignments")
    def list_assignments(request: Request, admin: str = Depends(require_slalom_bootcamp_admin)):
        query = request.query_params
        items = bootcamp.list_assignments(
            assessment_id=query.get("assessment_id") or query.get("assessmentId") or "",
            cohort_id=query.get("cohort_id") or query.get("cohortId") or "",
            employee_id=query.get("employee_id") or query.get("employeeId") or "",
        )
        return {"assignments": items, "items": items}

    @router.delete("/assignments/{assignment_id}")
    @_guard("Failed to delete bootcamp assignment")
    def delete_assignment(assignment_id: str, request: Request,
                          admin: str = Depends(require_slalom_bootcamp_admin)):
        deleted = bootcamp.delete_assignment(assignment_id)
        _audit(admin, request, "SLALOM_BOOTCAMP_ASSIGNMENT_DELETED", f"Deleted assignment {assignment_id}.")
        return {"success": True, "deleted": deleted}

    # ------------------------------------------------------------------ #
    # Tenant-scoped learning content
    # ------------------------------------------------------------------ #

    @router.get("/content")
    @_guard("Failed to load bootcamp learning content")
    def list_content(request: Request, admin: str = Depends(require_slalom_bootcamp_admin)):
        query = request.query_params
        raw_type = query.get("content_type") or query.get("contentType") or query.get("type") or ""
        content_type = _content_type(raw_type) if raw_type else ""
        items = bootcamp.list_content(content_type)
        return {"content": items, "items": items, "sources": items}

    @router.post("/content")
    @_guard("Failed to save bootcamp learning content")
    def save_content(request: Request, body: dict = Body(default_factory=dict),
                     admin: str = Depends(require_slalom_bootcamp_admin)):
        # Keep the submitted type intact so the service can normalise it and
        # preserve the original friendly label in the stored payload.
        result = bootcamp.save_content(dict(body), admin)
        _audit(admin, request, "SLALOM_BOOTCAMP_CONTENT_SAVED", f"Saved content {result['id']}.")
        return {"success": True, **result, "content": result}

    @router.patch("/content")
    @_guard("Failed to update bootcamp learning content")
    def update_content(request: Request, body: dict = Body(default_factory=dict),
                       admin: str = Depends(require_slalom_bootcamp_admin)):
        content_id = str(request.query_params.get("id") or _first(body, "id", "contentId", "content_id", default="") or "")
        if not content_id:
            return json_error(400, "A content id is required.")
        existing = bootcamp.map_content(bootcamp.get_content(content_id))
        result = bootcamp.save_content(_body_for_update(existing, body), admin)
        _audit(admin, request, "SLALOM_BOOTCAMP_CONTENT_UPDATED", f"Updated content {content_id}.")
        return {"success": True, **result, "content": result}

    @router.delete("/content")
    @_guard("Failed to delete bootcamp learning content")
    def delete_content(request: Request, body: dict = Body(default_factory=dict),
                       admin: str = Depends(require_slalom_bootcamp_admin)):
        content_id = str(request.query_params.get("id") or _first(body, "id", "contentId", "content_id", default="") or "")
        if not content_id:
            return json_error(400, "A content id is required.")
        deleted = bootcamp.delete_content(content_id)
        _audit(admin, request, "SLALOM_BOOTCAMP_CONTENT_DELETED", f"Deleted content {content_id}.")
        return {"success": True, "deleted": deleted}

    # ------------------------------------------------------------------ #
    # Read-only helpers
    # ------------------------------------------------------------------ #

    @router.get("/leaderboard")
    @_guard("Failed to load bootcamp leaderboard")
    def leaderboard(request: Request, admin: str = Depends(require_slalom_bootcamp_admin)):
        query = request.query_params
        assessment_id = query.get("assessment_id") or query.get("assessmentId") or ""
        cohort_id = query.get("cohort_id") or query.get("cohortId") or ""
        try:
            limit = int(query.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        return bootcamp.leaderboard(assessment_id, cohort_id, limit)

    @router.get("/employees")
    @_guard("Failed to load bootcamp employees")
    def list_employees(request: Request, admin: str = Depends(require_slalom_bootcamp_admin)):
        rows = get_store().select(
            "employees", {"role": "employee", "product_qb_eligible": True},
            ["employee_id", "full_name", "email", "designation", "department", "product"],
            "full_name asc",
        )
        return {"employees": [{
            "employeeId": row["employee_id"], "name": row["full_name"],
            "email": row.get("email") or "", "designation": row.get("designation") or "",
            "department": row.get("department") or "", "product": row.get("product") or "",
        } for row in rows]}

    @router.get("/employees/{employee_id}/assignment")
    @_guard("Failed to load employee bootcamp assignment")
    def employee_assignment(employee_id: str, request: Request,
                            admin: str = Depends(require_slalom_bootcamp_admin)):
        return {"assignment": bootcamp.employee_assignment(employee_id)}

    @router.get("/question-banks")
    @_guard("Failed to load question banks")
    def question_banks(request: Request, admin: str = Depends(require_slalom_bootcamp_admin)):
        return {"products": employee_portal_admin.product_configs()}

    return router


router = build_router()
alias_routers = tuple(build_router(prefix, f"slalom-bootcamp-alias-{index + 1}")
                      for index, prefix in enumerate(ALIASES))
