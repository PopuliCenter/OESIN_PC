"""
Case-level ABAC: which records a user sees and may act on.

Policy under test (backend/access.py):
  * admin/case_manager see every case plus data that belongs to no case.
  * Other roles see only data in cases they are a member of; data with no case
    is invisible to them, and they must name one of their cases when creating.
  * Location points, sessions and geofence events inherit their consent's case.
  * Geofences belong to a case or are shared (NULL); a case geofence is only
    evaluated for consents of that case.
  * Records outside the caller's cases are 404, never 403 (ids can't be probed).

The `world` fixture (conftest.py) is shared and must stay unchanged: tests
that mutate data create their own cases and users.
"""

import uuid

from conftest import FENCE_A, FENCE_B, FENCE_SHARED, start_session, send_point


def ids(items, key="id"):
    return {i[key] for i in items}


def new_case(manager, *members):
    r = manager.post("/v1/cases", json={"title": f"tmp-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    for a in members:
        assert manager.post(f"/v1/cases/{cid}/members", json={"username": a.username}).status_code == 200
    return cid


# --- reading: a member sees their case, nothing else ------------------------------------
def test_case_list_and_detail_are_scoped(world):
    listed = ids(world.inv_a.get("/v1/cases").json()["cases"])
    assert world.case_a in listed and world.case_b not in listed
    assert world.inv_a.get(f"/v1/cases/{world.case_a}").status_code == 200
    assert world.inv_a.get(f"/v1/cases/{world.case_b}").status_code == 404   # not 403
    assert world.manager.get(f"/v1/cases/{world.case_b}").status_code == 200


def test_consents_are_scoped(world):
    seen = ids(world.inv_a.get("/v1/consents").json()["consents"])
    assert seen == {world.consent_a}
    all_ = ids(world.manager.get("/v1/consents").json()["consents"])
    assert {world.consent_a, world.consent_b, world.consent_none} <= all_


def test_findings_are_scoped(world):
    rows = world.inv_a.get("/v1/findings").json()["findings"]
    assert ids(rows) == {world.finding_a}
    assert {r["case_id"] for r in rows} == {world.case_a}
    all_ = ids(world.manager.get("/v1/findings").json()["findings"])
    assert {world.finding_a, world.finding_b, world.finding_none} <= all_


def test_user_in_no_case_sees_nothing(seed, world):
    outsider = seed["investigator"]
    assert outsider.get("/v1/consents").json()["consents"] == []
    assert outsider.get("/v1/findings").json()["findings"] == []
    assert outsider.get(f"/v1/cases/{world.case_a}").status_code == 404


def test_location_points_are_scoped(world):
    pts = world.ana_a.get("/v1/location/points").json()["points"]
    assert len(pts) == 3 and {p["consent_id"] for p in pts} == {world.consent_a}
    assert world.ana_a.get(f"/v1/location/points?consent_id={world.consent_b}").status_code == 404
    pts_b = world.ana_b.get("/v1/location/points").json()["points"]
    assert len(pts_b) == 3 and {p["consent_id"] for p in pts_b} == {world.consent_b}


def test_analytics_are_scoped(world):
    a = world.ana_a
    assert a.get(f"/v1/analytics/movement?consent_id={world.consent_b}").status_code == 404
    assert a.get(f"/v1/analytics/activity-area?consent_id={world.consent_b}").status_code == 404
    mv = a.get(f"/v1/analytics/movement?consent_id={world.consent_a}")
    assert mv.status_code == 200 and mv.json()["points"] == 3

    # Heatmap with no filter only aggregates the caller's points.
    heat = a.get("/v1/analytics/heatmap").json()
    assert sum(c["weight"] for c in heat["cells"]) == 3
    assert a.get(f"/v1/analytics/heatmap?geofence_id={world.fence_b}").status_code == 404

    # Points inside the shared fence: only case A's one, not case B's.
    inside = a.get(f"/v1/analytics/in-geofence?geofence_id={world.fence_shared}").json()
    assert inside["count"] == 1 and {p["consent_id"] for p in inside["points"]} == {world.consent_a}
    assert a.get(f"/v1/analytics/in-geofence?geofence_id={world.fence_b}").status_code == 404

    dwell = ids(a.get("/v1/analytics/dwell").json()["dwell"], "geofence_id")
    assert world.fence_a in dwell and dwell <= {world.fence_a, world.fence_shared}


def test_geofences_and_events_are_scoped(world):
    fences = ids(world.ana_a.get("/v1/geofences").json()["geofences"])
    assert {world.fence_a, world.fence_shared} <= fences and world.fence_b not in fences

    events = world.ana_a.get("/v1/geofence/events").json()["events"]
    assert {e["consent_id"] for e in events} == {world.consent_a}
    assert {(e["geofence_id"], e["event_type"]) for e in events} == {
        (world.fence_a, "enter"), (world.fence_a, "exit"), (world.fence_shared, "enter")}


def test_case_geofence_is_only_evaluated_for_its_own_case(world):
    """Consent B passed through fence A's area: fence A belongs to case A, so
    no event may be recorded for consent B on it."""
    events = [e for e in world.manager.get("/v1/geofence/events?limit=1000").json()["events"]
              if e["consent_id"] == world.consent_b]
    assert {(e["geofence_id"], e["event_type"]) for e in events} == {
        (world.fence_b, "enter"), (world.fence_b, "exit"), (world.fence_shared, "enter")}


def test_exports_are_scoped(world):
    a = world.ana_a
    for path in (f"/v1/reports/consent/{world.consent_b}/points.csv",
                 f"/v1/reports/consent/{world.consent_b}/events.csv",
                 f"/v1/reports/consent/{world.consent_b}/report.pdf"):
        assert a.get(path).status_code == 404, path
    csv_a = a.get(f"/v1/reports/consent/{world.consent_a}/points.csv")
    assert csv_a.status_code == 200 and len(csv_a.text.strip().splitlines()) == 1 + 3
    pdf_a = a.get(f"/v1/reports/consent/{world.consent_a}/report.pdf")
    assert pdf_a.status_code == 200 and pdf_a.content[:5] == b"%PDF-"

    findings_csv = world.inv_a.get("/v1/reports/findings.csv").text
    assert world.finding_a in findings_csv
    assert world.finding_b not in findings_csv and world.finding_none not in findings_csv


def test_graph_is_scoped(world):
    nodes = ids(world.inv_a.get("/v1/graph").json()["nodes"])
    assert world.consent_a in nodes and world.fence_a in nodes
    assert not nodes & {world.consent_b, world.consent_none, world.fence_b}


def test_summary_is_scoped(world, seed):
    s = world.inv_a.get("/v1/summary").json()
    assert (s["consents_total"], s["findings"], s["location_points"], s["geofences"]) == (1, 1, 3, 2)
    assert s["audit_events"] is None                       # org-wide counter hidden
    assert isinstance(seed["auditor"].get("/v1/summary").json()["audit_events"], int)


# --- acting on other cases' records ---------------------------------------------------------
def test_cannot_act_on_another_cases_consent(world):
    a = world.inv_a
    assert a.post(f"/v1/consents/{world.consent_b}/revoke").status_code == 404
    assert a.post(f"/v1/consents/{world.consent_b}/link-phone",
                  json={"phone": "081299990000"}).status_code == 404
    assert a.post(f"/v1/cases/{world.case_a}/consents/{world.consent_b}").status_code == 404
    b = next(c for c in world.manager.get("/v1/consents").json()["consents"]
             if c["id"] == world.consent_b)
    assert b["status"] == "active" and b["case_id"] == world.case_b and b["phone_e164"] is None


# --- creating: non-managers must create inside one of their cases --------------------------------
def test_non_manager_must_name_one_of_their_cases(world):
    a = world.inv_a
    body = {"subject_label": "x", "purpose": "x"}
    assert a.post("/v1/consents", json=body).status_code == 422
    assert a.post("/v1/consents", json={**body, "case_id": world.case_b}).status_code == 404
    assert a.post("/v1/lookup", json={"value": "081200000000", "default_region": "ID"}).status_code == 422
    fence = {"name": "x", "kind": "circle", "center_lat": 0, "center_lng": 0, "radius_m": 10}
    assert world.ana_a.post("/v1/geofences", json=fence).status_code == 403        # shared: managers only
    assert world.ana_a.post("/v1/geofences", json={**fence, "case_id": world.case_b}).status_code == 404


def test_geofence_write_rules(world, make_user):
    assert world.ana_a.delete(f"/v1/geofences/{world.fence_shared}").status_code == 403
    assert world.ana_a.delete(f"/v1/geofences/{world.fence_b}").status_code == 404
    ana = make_user("ana_w", "location_analyst")
    case = new_case(world.manager, ana)
    r = ana.post("/v1/geofences", json={"name": "tmp", "kind": "circle", "center_lat": 1,
                                        "center_lng": 1, "radius_m": 10, "case_id": case})
    assert r.status_code == 200
    assert ana.delete(f"/v1/geofences/{r.json()['geofence_id']}").status_code == 200


# --- membership, closed cases, moving consents ---------------------------------------------------
def test_removing_a_member_takes_effect_immediately(world, make_user):
    m, inv = world.manager, make_user("inv_rm", "investigator")
    case = new_case(m, inv)
    consent = inv.post("/v1/consents", json={"subject_label": "x", "purpose": "x",
                                             "case_id": case}).json()["consent_id"]
    assert inv.get(f"/v1/cases/{case}").status_code == 200
    assert m.delete(f"/v1/cases/{case}/members/{inv.user_id}").status_code == 200
    # same token, no re-login: membership is re-read per request
    assert inv.get(f"/v1/cases/{case}").status_code == 404
    assert consent not in ids(inv.get("/v1/consents").json()["consents"])


def test_closed_case_rejects_new_data(world, make_user):
    m, inv = world.manager, make_user("inv_cl", "investigator")
    case = new_case(m, inv)
    assert m.post(f"/v1/cases/{case}/close").status_code == 200
    body = {"subject_label": "x", "purpose": "x", "case_id": case}
    assert inv.post("/v1/consents", json=body).status_code == 409
    assert inv.post("/v1/lookup", json={"value": "081200000001", "default_region": "ID",
                                        "case_id": case}).status_code == 409
    assert m.post(f"/v1/cases/{case}/reopen").status_code == 200
    assert inv.post("/v1/consents", json=body).status_code == 200


def test_attaching_a_consent_changes_who_sees_it(world, make_user):
    m, inv = world.manager, make_user("inv_at", "investigator")
    case = new_case(m, inv)
    orphan = m.post("/v1/consents", json={"subject_label": "x", "purpose": "x"}).json()["consent_id"]
    assert orphan not in ids(inv.get("/v1/consents").json()["consents"])
    assert m.post(f"/v1/cases/{case}/consents/{orphan}").status_code == 200
    assert orphan in ids(inv.get("/v1/consents").json()["consents"])


def test_moving_a_consent_closes_its_open_geofence_states(client, world, make_user):
    """A consent inside a case-G fence moves to case H: G's fence leaves its
    scope, so an exit event closes the open visit (dwell stays correct), and
    G's analyst may no longer delete a fence whose events now belong to case H."""
    m = world.manager
    ana_g, inv_g = make_user("ana_g", "location_analyst"), make_user("inv_g", "investigator")
    case_g, case_h = new_case(m, ana_g, inv_g), new_case(m)
    spot = (-6.55, 106.55)
    fence = ana_g.post("/v1/geofences", json={
        "name": "G", "kind": "circle", "center_lat": spot[0], "center_lng": spot[1],
        "radius_m": 300, "case_id": case_g}).json()["geofence_id"]
    consent = inv_g.post("/v1/consents", json={"subject_label": "x", "purpose": "x",
                                               "case_id": case_g}).json()["consent_id"]
    sid, tok = start_session(client, consent)
    assert send_point(client, sid, tok, *spot).status_code == 200          # enter G

    assert m.post(f"/v1/cases/{case_h}/consents/{consent}").status_code == 200
    events = [e for e in m.get("/v1/geofence/events?limit=1000").json()["events"]
              if e["consent_id"] == consent]
    assert sorted(e["event_type"] for e in events) == ["enter", "exit"]
    assert ana_g.get(f"/v1/location/points?consent_id={consent}").status_code == 404
    assert ana_g.delete(f"/v1/geofences/{fence}").status_code == 409


# --- regressions from the independent review of this branch ----------------------------------------
def test_closed_case_freezes_location_collection(client, world, make_user):
    m, inv = world.manager, make_user("inv_fz", "investigator")
    case = new_case(m, inv)
    consent = inv.post("/v1/consents", json={"subject_label": "x", "purpose": "x",
                                             "case_id": case}).json()["consent_id"]
    sid, tok = start_session(client, consent)
    assert m.post(f"/v1/cases/{case}/close").status_code == 200
    assert send_point(client, sid, tok, -6.6, 106.6).status_code == 409
    assert client.post("/v1/location/sessions", json={"consent_id": consent}).status_code == 409
    assert m.post(f"/v1/cases/{case}/reopen").status_code == 200
    assert send_point(client, sid, tok, -6.6, 106.6).status_code == 200


def test_closed_case_blocks_phone_link_and_fence_delete(world, make_user):
    m = world.manager
    inv, ana = make_user("inv_cz", "investigator"), make_user("ana_cz", "location_analyst")
    case = new_case(m, inv, ana)
    consent = inv.post("/v1/consents", json={"subject_label": "x", "purpose": "x",
                                             "case_id": case}).json()["consent_id"]
    fence = ana.post("/v1/geofences", json={"name": "z", "kind": "circle", "center_lat": 2,
                                            "center_lng": 2, "radius_m": 10,
                                            "case_id": case}).json()["geofence_id"]
    assert m.post(f"/v1/cases/{case}/close").status_code == 200
    assert inv.post(f"/v1/consents/{consent}/link-phone", json={"phone": "081200000002"}).status_code == 409
    assert ana.delete(f"/v1/geofences/{fence}").status_code == 409
    assert m.delete(f"/v1/geofences/{fence}").status_code == 409
    assert m.post(f"/v1/cases/{case}/reopen").status_code == 200
    assert ana.delete(f"/v1/geofences/{fence}").status_code == 200


def test_case_geofence_analytics_ignore_other_cases(world, seed):
    """Consent B passed through fence A's area. Fence A belongs to case A, so even
    a viewer who sees both cases must not count consent B's point in it."""
    admin = seed["admin"]
    inside = admin.get(f"/v1/analytics/in-geofence?geofence_id={world.fence_a}").json()
    assert {p["consent_id"] for p in inside["points"]} == {world.consent_a}
    heat = admin.get(f"/v1/analytics/heatmap?geofence_id={world.fence_a}").json()
    assert sum(c["weight"] for c in heat["cells"]) == 1


def test_moving_a_consent_audits_its_source_case(world, seed):
    m = world.manager
    src, dst = new_case(m), new_case(m)
    consent = m.post("/v1/consents", json={"subject_label": "x", "purpose": "x",
                                           "case_id": src}).json()["consent_id"]
    assert m.post(f"/v1/cases/{dst}/consents/{consent}").status_code == 200
    rows = seed["auditor"].get("/v1/audit?limit=500").json()["events"]
    assert any(r["action"] == "CASE_ATTACH_CONSENT" and r["resource_id"] == dst
               and r["detail"] == f"{consent} from {src}" for r in rows)


def test_csv_export_neutralises_formulas(client, world, make_user):
    m, ana = world.manager, make_user("ana_csv", "location_analyst")
    case = new_case(m, ana)
    spot = (-6.75, 106.75)
    ana.post("/v1/geofences", json={"name": '=HYPERLINK("http://evil.example","x")',
                                    "kind": "circle", "center_lat": spot[0],
                                    "center_lng": spot[1], "radius_m": 100, "case_id": case})
    consent = m.post("/v1/consents", json={"subject_label": "x", "purpose": "x",
                                           "case_id": case}).json()["consent_id"]
    sid, tok = start_session(client, consent)
    assert send_point(client, sid, tok, *spot).status_code == 200
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(ana.get(f"/v1/reports/consent/{consent}/events.csv").text)))
    assert rows and all(r["geofence"].startswith("'=HYPERLINK") for r in rows)
    assert all(r["latitude"] == "-6.75" for r in rows)   # negative numbers are not touched


def test_world_points_landed_where_expected(world):
    """Sanity check of the fixture itself, so a broken fixture fails loudly."""
    pts = world.manager.get("/v1/location/points?limit=1000").json()["points"]
    by_consent = {}
    for p in pts:
        by_consent.setdefault(p["consent_id"], set()).add((p["latitude"], p["longitude"]))
    assert FENCE_A in by_consent[world.consent_a] and FENCE_B in by_consent[world.consent_b]
    assert FENCE_SHARED in by_consent[world.consent_a] and FENCE_SHARED in by_consent[world.consent_b]
