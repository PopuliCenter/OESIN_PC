"""
The internet-facing surface: requests that come through the Cloudflare tunnel
(they carry cf-ray / cf-connecting-ip) may only reach the consent agent, and
location writes need the session's own write token.
"""

import pytest

from conftest import send_point, start_session

TUNNEL = {"cf-ray": "test-ray", "cf-connecting-ip": "203.0.113.9"}


@pytest.mark.parametrize("method,path", [
    ("GET", "/"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
    ("GET", "/static/app.js"),
    ("GET", "/v1/consents"),
    ("GET", "/v1/cases"),
    ("GET", "/v1/public-url"),
    ("POST", "/v1/auth/login"),
    ("GET", "/v1/location/points"),
])
def test_tunnel_blocks_operator_surface(client, method, path):
    r = client.request(method, path, headers=TUNNEL, json={} if method == "POST" else None)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_tunnel_allows_consent_agent(client):
    assert client.get("/consent", headers=TUNNEL).status_code == 200
    assert client.get("/static/style.css", headers=TUNNEL).status_code == 200


def _own_consent(seed):
    r = seed["case_manager"].post("/v1/consents", json={"subject_label": "pub", "purpose": "pub"})
    assert r.status_code == 200, r.text
    return r.json()["consent_id"]


def test_point_needs_the_sessions_own_token(client, seed):
    consent = _own_consent(seed)
    sid, tok = start_session(client, consent)
    sid2, _ = start_session(client, consent)
    assert send_point(client, sid, "A" * 43, -6.2, 106.8).status_code == 403   # wrong token
    assert send_point(client, sid2, tok, -6.2, 106.8).status_code == 403       # other session's token
    assert send_point(client, sid, tok, -6.2, 106.8).status_code == 200


def test_closed_session_and_revoked_consent_reject_points(client, seed):
    consent = _own_consent(seed)
    sid, tok = start_session(client, consent)
    r = client.post(f"/v1/location/sessions/{sid}/close", json={"session_token": tok})
    assert r.status_code == 200
    assert send_point(client, sid, tok, -6.2, 106.8).status_code == 403

    sid, tok = start_session(client, consent)
    assert seed["case_manager"].post(f"/v1/consents/{consent}/revoke").status_code == 200
    assert send_point(client, sid, tok, -6.2, 106.8).status_code == 403
    r = client.post("/v1/location/sessions", json={"consent_id": consent})
    assert r.status_code == 403
    # a revoked consent discloses nothing but its status
    assert client.get(f"/v1/public/consent/{consent}").json() == {"id": consent, "status": "revoked"}


@pytest.mark.parametrize("payload", [
    {"latitude": float("nan")},
    {"accuracy_m": float("inf")},
    {"captured_at": "1970-01-01T00:00:00Z"},
])
def test_forged_point_values_are_rejected(client, seed, payload):
    consent = _own_consent(seed)
    sid, tok = start_session(client, consent)
    body = {"session_id": sid, "session_token": tok, "latitude": -6.2, "longitude": 106.8,
            **payload}
    # NaN/Infinity are not valid JSON; send them the way an attacker would.
    import json
    r = client.post("/v1/location/points", content=json.dumps(body, allow_nan=True),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
