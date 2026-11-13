"""
Spatial analytics over consent-based location data, computed in PostGIS.

All functions take an open psycopg connection and return plain dicts/lists ready
for JSON. Every query is scoped to a consent (or a geofence) so analytics stay
tied to a lawful basis, never a blanket sweep.
"""

import json
from typing import Any, Optional


def _time_clause(from_ts: Optional[str], to_ts: Optional[str], params: dict) -> str:
    clause = ""
    if from_ts:
        clause += " AND captured_at >= %(from_ts)s"
        params["from_ts"] = from_ts
    if to_ts:
        clause += " AND captured_at <= %(to_ts)s"
        params["to_ts"] = to_ts
    return clause


def movement_summary(conn, consent_id: str,
                     from_ts: Optional[str] = None,
                     to_ts: Optional[str] = None) -> dict[str, Any]:
    """
    Point count, time span, total path length (ST_Length over an ordered line),
    average accuracy, and bounding box for one consent's track.
    """
    params: dict[str, Any] = {"cid": consent_id}
    tc = _time_clause(from_ts, to_ts, params)
    row = conn.execute(f"""
        SELECT
          COUNT(*)                                                   AS points,
          MIN(captured_at)                                           AS first_seen,
          MAX(captured_at)                                           AS last_seen,
          AVG(accuracy_m)                                            AS avg_accuracy_m,
          MIN(latitude)  AS min_lat, MAX(latitude)  AS max_lat,
          MIN(longitude) AS min_lng, MAX(longitude) AS max_lng,
          ST_Length(ST_MakeLine(geom::geometry ORDER BY captured_at)::geography)
                                                                     AS distance_m
        FROM location_points
        WHERE consent_id = %(cid)s {tc}
    """, params).fetchone()

    d = dict(row)
    d["distance_m"] = round(d["distance_m"] or 0.0, 1)
    d["distance_km"] = round((d["distance_m"] or 0.0) / 1000.0, 3)
    if d["avg_accuracy_m"] is not None:
        d["avg_accuracy_m"] = round(float(d["avg_accuracy_m"]), 1)
    if d["points"] and d["first_seen"] and d["last_seen"]:
        span = (d["last_seen"] - d["first_seen"]).total_seconds()
        d["span_seconds"] = int(span)
        d["span_hours"] = round(span / 3600.0, 2)
    else:
        d["span_seconds"] = 0
        d["span_hours"] = 0.0
    return d


def activity_area(conn, consent_id: str) -> dict[str, Any]:
    """
    Convex hull of a consent's points = the area they moved within.
    Returns the hull as GeoJSON plus its area in m^2/km^2.
    """
    row = conn.execute("""
        WITH pts AS (
          SELECT geom::geometry AS g FROM location_points WHERE consent_id = %(cid)s
        )
        SELECT
          ST_AsGeoJSON(ST_ConvexHull(ST_Collect(g)))        AS hull_geojson,
          ST_Area(ST_ConvexHull(ST_Collect(g))::geography)  AS area_m2,
          COUNT(*)                                          AS points
        FROM pts
    """, {"cid": consent_id}).fetchone()

    d = dict(row)
    d["area_m2"] = round(d["area_m2"] or 0.0, 1)
    d["area_km2"] = round((d["area_m2"] or 0.0) / 1_000_000.0, 4)
    d["hull"] = json.loads(d.pop("hull_geojson")) if d.get("hull_geojson") else None
    return d


def points_in_geofence(conn, geofence_id: str,
                       from_ts: Optional[str] = None,
                       to_ts: Optional[str] = None,
                       limit: int = 1000) -> dict[str, Any]:
    """
    Points that fall inside a geofence (polygon via ST_Contains, circle via
    ST_DWithin), optionally within a time window.
    """
    params: dict[str, Any] = {"gid": geofence_id, "limit": limit}
    tc = _time_clause(from_ts, to_ts, params).replace("captured_at", "p.captured_at")
    rows = conn.execute(f"""
        SELECT p.id, p.consent_id, p.latitude, p.longitude, p.captured_at
        FROM location_points p
        JOIN geofences g ON g.id = %(gid)s
        WHERE (
            (g.kind = 'polygon' AND g.polygon_geom IS NOT NULL
             AND ST_Contains(g.polygon_geom, p.geom::geometry))
         OR (g.kind = 'circle'
             AND ST_DWithin(
                 ST_SetSRID(ST_MakePoint(g.center_lng, g.center_lat), 4326)::geography,
                 p.geom, g.radius_m))
        ) {tc}
        ORDER BY p.captured_at DESC
        LIMIT %(limit)s
    """, params).fetchall()
    return {"geofence_id": geofence_id, "count": len(rows),
            "points": [dict(r) for r in rows]}


def heatmap_grid(conn, consent_id: Optional[str] = None, cell_m: int = 250,
                 geofence_id: Optional[str] = None) -> dict[str, Any]:
    """
    Point-density grid computed in PostGIS: snap every point to a grid cell
    (ST_SnapToGrid), then GROUP BY cell counting points. Each cell returns the
    centroid of its points + a weight (count). Rendered as a heat layer client
    side. Cell size is given in metres and converted to ~degrees.
    """
    cell_deg = max(int(cell_m), 10) / 111_320.0    # ~metres per degree of latitude
    params: dict[str, Any] = {"cell": cell_deg}
    where = "WHERE TRUE"
    if consent_id:
        where += " AND consent_id = %(cid)s"
        params["cid"] = consent_id
    if geofence_id:
        params["gid"] = geofence_id
        where += """
          AND EXISTS (SELECT 1 FROM geofences g WHERE g.id = %(gid)s AND (
              (g.kind='polygon' AND g.polygon_geom IS NOT NULL
               AND ST_Contains(g.polygon_geom, location_points.geom::geometry))
           OR (g.kind='circle'
               AND ST_DWithin(
                   ST_SetSRID(ST_MakePoint(g.center_lng, g.center_lat), 4326)::geography,
                   location_points.geom, g.radius_m))
          ))"""

    rows = conn.execute(f"""
        WITH snapped AS (
          SELECT ST_SnapToGrid(geom::geometry, %(cell)s) AS cell,
                 geom::geometry AS g
          FROM location_points
          {where}
        )
        SELECT ST_Y(ST_Centroid(ST_Collect(g))) AS lat,
               ST_X(ST_Centroid(ST_Collect(g))) AS lng,
               COUNT(*)                          AS weight
        FROM snapped
        GROUP BY cell
        ORDER BY weight DESC
    """, params).fetchall()

    cells = [dict(r) for r in rows]
    max_weight = max((c["weight"] for c in cells), default=0)
    return {"cells": cells, "max_weight": max_weight,
            "cell_m": int(cell_m), "count_cells": len(cells)}


def dwell_by_geofence(conn, consent_id: Optional[str] = None) -> dict[str, Any]:
    """
    Dwell time per geofence, derived from enter/exit event pairs. Computed in
    Python by pairing each 'enter' with the next 'exit' within a session.
    """
    params: dict[str, Any] = {}
    where = ""
    if consent_id:
        where = "WHERE e.consent_id = %(cid)s"
        params["cid"] = consent_id
    rows = conn.execute(f"""
        SELECT e.geofence_id, e.session_id, e.event_type, e.created_at,
               g.name AS geofence_name
        FROM geofence_events e
        LEFT JOIN geofences g ON g.id = e.geofence_id
        {where}
        ORDER BY e.geofence_id, e.session_id, e.created_at
    """, params).fetchall()

    agg: dict[str, dict[str, Any]] = {}
    open_enter: dict[tuple, Any] = {}
    for r in rows:
        gid = r["geofence_id"]
        key = (gid, r["session_id"])
        agg.setdefault(gid, {"geofence_id": gid, "geofence_name": r["geofence_name"],
                             "visits": 0, "dwell_seconds": 0})
        if r["event_type"] == "enter":
            open_enter[key] = r["created_at"]
        elif r["event_type"] == "exit" and key in open_enter:
            secs = (r["created_at"] - open_enter.pop(key)).total_seconds()
            agg[gid]["visits"] += 1
            agg[gid]["dwell_seconds"] += int(secs)

    out = []
    for v in agg.values():
        v["dwell_minutes"] = round(v["dwell_seconds"] / 60.0, 1)
        out.append(v)
    out.sort(key=lambda x: x["dwell_seconds"], reverse=True)
    return {"dwell": out}
