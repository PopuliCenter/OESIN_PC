"""
Per-consent (case) report generation: CSV data exports + a formatted PDF summary.

CSV uses stdlib csv. PDF uses fpdf2 (pure-Python, no system deps). Text is kept
ASCII-safe so the PDF core fonts (latin-1) never choke on it.
"""

import csv
import io
from typing import Any, Optional

import analytics
import database as db


def _ascii(s: Any) -> str:
    return str(s if s is not None else "").encode("ascii", "replace").decode("ascii")


# --- CSV --------------------------------------------------------------------
def _csv_from_rows(header: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def points_csv(conn, consent_id: str) -> str:
    rows = conn.execute(
        "SELECT id, latitude, longitude, accuracy_m, captured_at, created_at"
        " FROM location_points WHERE consent_id=%s ORDER BY captured_at",
        (consent_id,)).fetchall()
    return _csv_from_rows(
        ["point_id", "latitude", "longitude", "accuracy_m", "captured_at", "created_at"],
        [[r["id"], r["latitude"], r["longitude"], r["accuracy_m"],
          r["captured_at"], r["created_at"]] for r in rows])


def events_csv(conn, consent_id: str) -> str:
    rows = conn.execute(
        "SELECT e.id, e.event_type, e.latitude, e.longitude, e.created_at, g.name"
        " FROM geofence_events e LEFT JOIN geofences g ON g.id=e.geofence_id"
        " WHERE e.consent_id=%s ORDER BY e.created_at", (consent_id,)).fetchall()
    return _csv_from_rows(
        ["event_id", "event_type", "geofence", "latitude", "longitude", "created_at"],
        [[r["id"], r["event_type"], r["name"], r["latitude"], r["longitude"],
          r["created_at"]] for r in rows])


def findings_csv(conn) -> str:
    rows = conn.execute(
        "SELECT id, entity_id, finding_type, source, source_kind, confidence, created_at"
        " FROM findings ORDER BY created_at DESC").fetchall()
    return _csv_from_rows(
        ["finding_id", "entity_id", "finding_type", "source", "source_kind",
         "confidence", "created_at"],
        [[r["id"], r["entity_id"], r["finding_type"], r["source"],
          r["source_kind"], r["confidence"], r["created_at"]] for r in rows])


# --- PDF ---------------------------------------------------------------------
def build_pdf(conn, consent_id: str, generated_by: str, generated_at: str) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    NX, NY = XPos.LMARGIN, YPos.NEXT

    consent = conn.execute("SELECT * FROM consents WHERE id=%s", (consent_id,)).fetchone()
    if not consent:
        raise ValueError("Consent not found")
    status = "revoked" if consent["revoked_at"] else "active"

    mv = analytics.movement_summary(conn, consent_id)
    area = analytics.activity_area(conn, consent_id)
    dwell = analytics.dwell_by_geofence(conn, consent_id)["dwell"]
    audit_rows = conn.execute(
        "SELECT action, actor, detail, created_at FROM audit_events"
        " WHERE resource_id=%s OR detail LIKE %s ORDER BY created_at DESC LIMIT 25",
        (consent_id, f"%{consent_id}%")).fetchall()

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def h(txt, size=13):
        pdf.ln(2); pdf.set_font("Helvetica", "B", size)
        pdf.cell(0, 8, _ascii(txt), new_x=NX, new_y=NY)
        pdf.set_font("Helvetica", "", 10)

    def kv(k, v):
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "B", 10); pdf.write(6, _ascii(k) + ": ")
        pdf.set_font("Helvetica", "", 10); pdf.write(6, _ascii(v))
        pdf.ln(6)

    pdf.set_font("Helvetica", "B", 17)
    pdf.cell(0, 10, "Case Report - Consent-Based Location", new_x=NX, new_y=NY)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120)
    pdf.cell(0, 6, _ascii(f"Generated {generated_at} by {generated_by}"),
             new_x=NX, new_y=NY)
    pdf.set_text_color(0)

    h("Subject / Consent")
    kv("Subject", consent["subject_label"])
    kv("Purpose", consent["purpose"])
    kv("Consent ID", consent["id"])
    kv("Status", status)
    kv("Policy version", consent["policy_version"])
    kv("Retention (days)", consent.get("retention_days", 30))
    kv("Granted at", consent["granted_at"])
    if consent["revoked_at"]:
        kv("Revoked at", consent["revoked_at"])

    h("Movement Summary")
    kv("Points", mv["points"])
    kv("Distance", f"{mv['distance_km']} km")
    kv("Activity area", f"{area['area_km2']} km2 ({area['points']} pts hull)")
    kv("Time span", f"{mv['span_hours']} hours")
    kv("Avg accuracy", f"{mv['avg_accuracy_m']} m" if mv["avg_accuracy_m"] is not None else "-")
    kv("First seen", mv["first_seen"])
    kv("Last seen", mv["last_seen"])
    if mv["points"]:
        kv("Bounding box", f"lat {mv['min_lat']}..{mv['max_lat']}, "
                           f"lng {mv['min_lng']}..{mv['max_lng']}")

    def para(txt, size=10, style=""):
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", style, size)
        pdf.multi_cell(pdf.epw, 5, _ascii(txt))

    h("Geofence Dwell")
    if dwell:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_x(pdf.l_margin)
        pdf.cell(90, 6, "Geofence"); pdf.cell(30, 6, "Visits")
        pdf.cell(0, 6, "Dwell (min)", new_x=NX, new_y=NY)
        pdf.set_font("Helvetica", "", 9)
        for d in dwell:
            pdf.set_x(pdf.l_margin)
            pdf.cell(90, 6, _ascii(d["geofence_name"] or d["geofence_id"]))
            pdf.cell(30, 6, str(d["visits"]))
            pdf.cell(0, 6, str(d["dwell_minutes"]), new_x=NX, new_y=NY)
    else:
        para("No geofence enter/exit events for this consent.")

    h("Audit Trail (related, latest 25)")
    if audit_rows:
        for a in audit_rows:
            para(f"{a['created_at']}  {a['actor']}  {a['action']}  {a['detail'] or ''}", 8)
    else:
        para("No audit events reference this consent.")

    pdf.ln(4)
    pdf.set_text_color(120)
    para("Data collected under explicit consent. Location is self-reported by the "
         "subject's device; the system does not derive GPS from a phone number.", 8, "I")

    return bytes(pdf.output())
