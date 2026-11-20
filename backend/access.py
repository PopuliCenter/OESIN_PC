"""
Case-level ABAC (PRD UC-08 / FR-07, SDD "RBAC + case-level ABAC").

Role checks (auth.require_roles) decide WHAT a user may do; this module decides
WHICH data they may do it to. Policy:

  * admin and case_manager see every case, plus data that belongs to no case.
  * Every other role only sees data in cases they are a member of. Data with no
    case (case_id IS NULL) is invisible to them. They must name one of their
    cases when creating findings or consents.
  * Geofences belong to a case, or are shared (case_id IS NULL). Shared
    geofences are visible to everyone but only managers may create or delete
    them. A geofence is only evaluated against consents in its own case
    (shared geofences: against every consent). See geo.evaluate_point.

Location points and geofence events have no case_id of their own: they inherit
it from their consent.

Anything the caller may not see is reported as 404, never 403, so ids of other
cases' records cannot be probed. 403 is only used when the record is visible
but the action is not allowed (e.g. an analyst deleting a shared geofence).
"""

from typing import Any, Optional

from fastapi import HTTPException

MANAGER_ROLES = {"admin", "case_manager"}


def is_manager(user: dict) -> bool:
    return user["role"] in MANAGER_ROLES


def allowed_cases(conn, user: dict) -> Optional[list[str]]:
    """None = unrestricted (managers). Otherwise the ids of the cases the user
    is a member of, possibly empty. Membership is read from the database on
    every request, so removing a member takes effect immediately."""
    if is_manager(user):
        return None
    rows = conn.execute("SELECT case_id FROM case_members WHERE user_id=%s",
                        (user["id"],)).fetchall()
    return [r["case_id"] for r in rows]


def can_see_case_id(allowed: Optional[list[str]], case_id: Optional[str]) -> bool:
    return allowed is None or (case_id is not None and case_id in allowed)


# --- SQL fragments -------------------------------------------------------------
# Each returns (sql, params) using positional %s placeholders, or ("TRUE", ())
# when unrestricted. `col` must be a trusted column reference, never user input.

def case_filter(allowed: Optional[list[str]], col: str = "case_id") -> tuple[str, tuple]:
    """Rows whose own case column is visible (consents, findings)."""
    if allowed is None:
        return "TRUE", ()
    return f"{col} = ANY(%s)", (allowed,)


def consent_filter(allowed: Optional[list[str]], col: str = "consent_id") -> tuple[str, tuple]:
    """Rows linked to a visible consent (location points, sessions, events)."""
    if allowed is None:
        return "TRUE", ()
    return f"{col} IN (SELECT id FROM consents WHERE case_id = ANY(%s))", (allowed,)


def geofence_filter(allowed: Optional[list[str]], col: str = "case_id") -> tuple[str, tuple]:
    """Geofences that are shared or belong to a visible case."""
    if allowed is None:
        return "TRUE", ()
    return f"({col} IS NULL OR {col} = ANY(%s))", (allowed,)


# --- single-record guards ----------------------------------------------------------
def consent_or_404(conn, consent_id: str, user: dict) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM consents WHERE id=%s", (consent_id,)).fetchone()
    if not row or not can_see_case_id(allowed_cases(conn, user), row["case_id"]):
        raise HTTPException(404, "Consent not found.")
    return dict(row)


def geofence_or_404(conn, geofence_id: str, user: dict, *, write: bool = False) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM geofences WHERE id=%s", (geofence_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Geofence not found.")
    allowed = allowed_cases(conn, user)
    if row["case_id"] is not None and not can_see_case_id(allowed, row["case_id"]):
        raise HTTPException(404, "Geofence not found.")
    if write and row["case_id"] is None and not is_manager(user):
        raise HTTPException(403, "Shared geofences can only be changed by a case manager.")
    return dict(row)


def require_case_for_non_manager(user: dict, case_id: Optional[str]) -> None:
    """Strict policy: non-managers may only create data inside one of their cases."""
    if not case_id and not is_manager(user):
        raise HTTPException(422, "case_id is required: choose one of your cases.")
