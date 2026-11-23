"""
Role gates (RBAC): which role may call which endpoint at all.

Seed actors belong to no case, so this file checks the role gate only; which
records a role sees is tested in test_abac.py. Endpoints that take a body get
an empty one: a denied role must be refused (403) before the body is
validated, while an allowed role passes the gate and fails validation (422).
That keeps these tests free of side effects.
"""

import pytest

GET, POST = "GET", "POST"

DENIED = [
    # role, method, path
    ("investigator", GET, "/v1/location/points"),        # raw GPS: analysts/managers only
    ("auditor", GET, "/v1/location/points"),
    ("location_analyst", POST, "/v1/lookup"),            # OSINT collection
    ("auditor", POST, "/v1/lookup"),
    ("location_analyst", POST, "/v1/consents"),
    ("auditor", POST, "/v1/consents"),
    ("investigator", POST, "/v1/geofences"),
    ("investigator", POST, "/v1/cases"),                 # cases: managers only
    ("location_analyst", POST, "/v1/cases"),
    ("auditor", GET, "/v1/cases"),
    ("investigator", GET, "/v1/audit"),                  # audit: auditors only
    ("case_manager", GET, "/v1/audit"),
    ("case_manager", GET, "/v1/users"),                  # users: admin only
    ("investigator", GET, "/v1/retention/preview"),      # retention: admin only
    ("case_manager", POST, "/v1/retention/run"),
    ("investigator", GET, "/v1/analytics/heatmap"),
    ("auditor", GET, "/v1/graph"),
    ("investigator", GET, "/v1/reports/consent/cns_x/points.csv"),
    ("case_manager", GET, "/v1/reports/consent/cns_x/points.csv"),
    ("location_analyst", GET, "/v1/reports/findings.csv"),
]

ALLOWED = [
    # role, method, path, expected status once past the gate
    ("auditor", GET, "/v1/audit", 200),
    ("admin", GET, "/v1/audit", 200),
    ("admin", GET, "/v1/users", 200),
    ("admin", GET, "/v1/retention/preview", 200),
    ("case_manager", POST, "/v1/cases", 422),            # empty body
    ("investigator", POST, "/v1/lookup", 422),
    ("case_manager", POST, "/v1/consents", 422),
    ("location_analyst", POST, "/v1/geofences", 422),
    ("location_analyst", GET, "/v1/location/points", 200),
    ("case_manager", GET, "/v1/location/points", 200),
    ("investigator", GET, "/v1/consents", 200),
    ("location_analyst", GET, "/v1/graph", 200),
    ("auditor", GET, "/v1/summary", 200),                # any authenticated user
]


def _call(actor, method, path):
    return actor.get(path) if method == GET else actor.post(path, json={})


@pytest.mark.parametrize("role,method,path", DENIED,
                         ids=[f"{r}-{m}-{p}" for r, m, p in DENIED])
def test_role_is_denied(seed, role, method, path):
    r = _call(seed[role], method, path)
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("role,method,path,expected", ALLOWED,
                         ids=[f"{r}-{m}-{p}" for r, m, p, _ in ALLOWED])
def test_role_passes_gate(seed, role, method, path, expected):
    r = _call(seed[role], method, path)
    assert r.status_code == expected, r.text


def test_no_token_is_401(client):
    assert client.get("/v1/consents").status_code == 401


def test_bad_token_is_401(client):
    r = client.get("/v1/consents", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_wrong_password_is_401(client):
    r = client.post("/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
