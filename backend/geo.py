"""
Geofencing via PostGIS spatial queries (SDD UC-07, §8).

Circle containment  -> ST_DWithin(center::geography, point::geography, radius_m)
Polygon containment -> ST_Contains(polygon_geom, point)

Enter/exit events are emitted on state transitions per (session, geofence).
"""

from typing import Any

import database as db

# lat/lng -> geography point literal (note: PostGIS takes lon, lat order)
_POINT_GEOG = "ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography"
_POINT_GEOM = "ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)"

# A geofence only applies to consents in its own case; shared geofences
# (case_id IS NULL) apply to every consent. Same scope for _FENCES_SQL.
_FENCE_SCOPE = "(case_id IS NULL OR case_id = %(case_id)s)"

_MATCH_SQL = f"""
SELECT id, name FROM geofences
WHERE {_FENCE_SCOPE} AND (
      (kind = 'circle' AND center_lat IS NOT NULL AND radius_m IS NOT NULL
       AND ST_DWithin(
           ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography,
           {_POINT_GEOG},
           radius_m))
   OR (kind = 'polygon' AND polygon_geom IS NOT NULL
       AND ST_Contains(polygon_geom, {_POINT_GEOM})))
"""
_FENCES_SQL = f"SELECT id, name FROM geofences WHERE {_FENCE_SCOPE}"


def polygon_wkt(polygon: list[list[float]]) -> str:
    """Build a closed POLYGON WKT from [[lat,lng], ...] vertices (lon lat order)."""
    pts = list(polygon)
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    coords = ", ".join(f"{lng} {lat}" for lat, lng in pts)
    return f"POLYGON(({coords}))"


def evaluate_point(conn, session_id: str, consent_id: str, point_id: str,
                   lat: float, lng: float) -> list[dict[str, Any]]:
    """
    PostGIS check of a freshly-ingested point against the geofences in scope
    for its consent (its case's geofences + shared ones); emit enter/exit
    events on state changes. Returns the generated events.
    """
    row = conn.execute("SELECT case_id FROM consents WHERE id=%s", (consent_id,)).fetchone()
    params = {"lat": lat, "lng": lng, "case_id": row["case_id"] if row else None}
    matched = {r["id"]: r["name"] for r in conn.execute(_MATCH_SQL, params).fetchall()}
    all_fences = conn.execute(_FENCES_SQL, params).fetchall()

    events: list[dict[str, Any]] = []
    for fence in all_fences:
        fid, fname = fence["id"], fence["name"]
        now_inside = fid in matched

        state = conn.execute(
            "SELECT inside FROM geofence_state WHERE session_id=%s AND geofence_id=%s",
            (session_id, fid)).fetchone()
        was_inside = bool(state["inside"]) if state else False

        if now_inside != was_inside:
            event_type = "enter" if now_inside else "exit"
            eid = db.new_id("gfe")
            conn.execute(
                "INSERT INTO geofence_events (id, geofence_id, session_id, consent_id,"
                " point_id, event_type, latitude, longitude, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (eid, fid, session_id, consent_id, point_id, event_type,
                 lat, lng, db.now_iso()))
            events.append({"id": eid, "geofence_id": fid,
                           "geofence_name": fname, "event_type": event_type})

        if state:
            conn.execute(
                "UPDATE geofence_state SET inside=%s, updated_at=%s"
                " WHERE session_id=%s AND geofence_id=%s",
                (now_inside, db.now_iso(), session_id, fid))
        else:
            conn.execute(
                "INSERT INTO geofence_state (session_id, geofence_id, inside, updated_at)"
                " VALUES (%s,%s,%s,%s)",
                (session_id, fid, now_inside, db.now_iso()))
    return events


def close_out_of_scope(conn, consent_id: str, new_case_id) -> list[dict[str, Any]]:
    """
    A consent moving to another case takes the old case's geofences out of its
    scope (evaluate_point no longer looks at them). Close every still-open
    "inside" state for those fences with an exit event at the session's last
    known point, so enter/exit pairs and dwell stay correct. Call inside the
    same transaction as the case change.
    """
    rows = conn.execute("""
        SELECT st.session_id, st.geofence_id, g.name
        FROM geofence_state st
        JOIN location_sessions ls ON ls.id = st.session_id
        JOIN geofences g ON g.id = st.geofence_id
        WHERE ls.consent_id = %s AND st.inside
          AND g.case_id IS NOT NULL AND g.case_id IS DISTINCT FROM %s
    """, (consent_id, new_case_id)).fetchall()
    events: list[dict[str, Any]] = []
    for r in rows:
        last = conn.execute(
            "SELECT id, latitude, longitude FROM location_points WHERE session_id=%s"
            " ORDER BY captured_at DESC LIMIT 1", (r["session_id"],)).fetchone()
        if last:   # points may already be purged by retention; then just clear state
            eid = db.new_id("gfe")
            conn.execute(
                "INSERT INTO geofence_events (id, geofence_id, session_id, consent_id,"
                " point_id, event_type, latitude, longitude, created_at)"
                " VALUES (%s,%s,%s,%s,%s,'exit',%s,%s,%s)",
                (eid, r["geofence_id"], r["session_id"], consent_id, last["id"],
                 last["latitude"], last["longitude"], db.now_iso()))
            events.append({"id": eid, "geofence_id": r["geofence_id"],
                           "geofence_name": r["name"], "event_type": "exit"})
        conn.execute(
            "UPDATE geofence_state SET inside=FALSE, updated_at=%s"
            " WHERE session_id=%s AND geofence_id=%s",
            (db.now_iso(), r["session_id"], r["geofence_id"]))
    return events
