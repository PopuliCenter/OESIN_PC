"""
Test fixtures.

Tests run against a real PostgreSQL + PostGIS (the compose `db` service):
case-level ABAC and geofencing live in SQL/PostGIS, so mocking the database
would test nothing. Each run creates a throwaway database and drops it at the
end; the real `osint` database is never touched.

Run:  docker compose run --rm app pytest
"""

import os
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

# --- throwaway database --------------------------------------------------------
# Must happen before the app is imported: database.DSN is read at import time.
_BASE = conninfo_to_dict(
    os.environ.get("DATABASE_URL", "postgresql://osint:osint@localhost:5432/osint"))
_TEST_DB = f"osint_test_{uuid.uuid4().hex[:10]}"
_ADMIN_DSN = make_conninfo(**{**_BASE, "dbname": "postgres"})
os.environ["DATABASE_URL"] = make_conninfo(**{**_BASE, "dbname": _TEST_DB})
os.environ.setdefault("JWT_SECRET", "test-secret-" + uuid.uuid4().hex)

TEST_PASSWORD = "test-pass-123"   # for users created by the fixtures below


@pytest.fixture(scope="session")
def _test_database():
    with psycopg.connect(_ADMIN_DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_TEST_DB)))
    yield
    with psycopg.connect(_ADMIN_DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                     .format(sql.Identifier(_TEST_DB)))


@pytest.fixture(scope="session")
def client(_test_database):
    from fastapi.testclient import TestClient

    import app as app_module

    # Host must be on the TrustedHost allowlist ("testserver" is not).
    with TestClient(app_module.app, base_url="http://localhost") as c:   # runs startup
        yield c


# --- authenticated actors ------------------------------------------------------------
class Actor:
    """A logged-in user. Thin wrapper that adds the bearer token."""

    def __init__(self, client, username: str, password: str):
        r = client.post("/v1/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"login {username}: {r.status_code} {r.text}"
        self.client = client
        self.username = username
        self.user_id = r.json()["user"]["id"]
        self.role = r.json()["user"]["role"]
        self.headers = {"Authorization": f"Bearer {r.json()['token']}"}

    def get(self, path: str, **kw):
        return self.client.get(path, headers=self.headers, **kw)

    def post(self, path: str, json: Any = None, **kw):
        return self.client.post(path, headers=self.headers, json=json, **kw)

    def delete(self, path: str, **kw):
        return self.client.delete(path, headers=self.headers, **kw)


# Seed accounts from auth.SEED_USERS (dev-only passwords).
SEED = {
    "admin": ("admin", "admin123"),
    "investigator": ("investigator", "invest123"),
    "location_analyst": ("analyst", "analyst123"),
    "case_manager": ("manager", "manager123"),
    "auditor": ("auditor", "audit123"),
}


@pytest.fixture(scope="session")
def seed(client) -> dict[str, Actor]:
    """One logged-in actor per role (seed accounts, members of no case)."""
    return {role: Actor(client, u, p) for role, (u, p) in SEED.items()}


@pytest.fixture(scope="session")
def make_user(client, seed):
    """Create (via the admin API) and log in a fresh user with a given role."""
    def _make(prefix: str, role: str) -> Actor:
        username = f"{prefix}_{uuid.uuid4().hex[:6]}"
        r = seed["admin"].post("/v1/users", json={
            "username": username, "password": TEST_PASSWORD, "role": role})
        assert r.status_code == 200, r.text
        return Actor(client, username, TEST_PASSWORD)
    return _make


def start_session(client, consent_id: str) -> tuple[str, str]:
    r = client.post("/v1/location/sessions", json={"consent_id": consent_id})
    assert r.status_code == 200, r.text
    return r.json()["session_id"], r.json()["session_token"]


def send_point(client, session_id: str, token: str, lat: float, lng: float):
    return client.post("/v1/location/points", json={
        "session_id": session_id, "session_token": token,
        "latitude": lat, "longitude": lng, "accuracy_m": 5})


# --- the shared scenario ----------------------------------------------------------------
# Three geofences far enough apart (> 5 km) that a point is inside at most one.
FENCE_A = (-6.20, 106.80)
FENCE_B = (-6.30, 106.90)
FENCE_SHARED = (-6.40, 107.00)
OUTSIDE = (-6.25, 106.85)


@dataclass
class World:
    manager: Actor
    inv_a: Actor
    ana_a: Actor
    inv_b: Actor
    ana_b: Actor
    case_a: str
    case_b: str
    consent_a: str
    consent_b: str
    consent_none: str      # consent in no case (managers only)
    finding_a: str
    finding_b: str
    finding_none: str
    fence_a: str
    fence_b: str
    fence_shared: str


@pytest.fixture(scope="session")
def world(client, seed, make_user) -> World:
    """
    Case A (inv_a, ana_a) and case B (inv_b, ana_b), each with one consent, one
    OSINT finding and one geofence; plus a shared geofence and a consent and a
    finding that belong to no case. Location points arrive through the public
    consent-agent API, so geofence events are produced by the real evaluator.

    Tests must NOT add data to case A or B: several assert exact counts. Tests
    that mutate create their own cases.
    """
    m = seed["case_manager"]
    inv_a, ana_a = make_user("inv_a", "investigator"), make_user("ana_a", "location_analyst")
    inv_b, ana_b = make_user("inv_b", "investigator"), make_user("ana_b", "location_analyst")

    def case(title: str, *members: Actor) -> str:
        r = m.post("/v1/cases", json={"title": title})
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        for a in members:
            assert m.post(f"/v1/cases/{cid}/members", json={"username": a.username}).status_code == 200
        return cid

    case_a, case_b = case("Case A", inv_a, ana_a), case("Case B", inv_b, ana_b)

    def consent(actor: Actor, case_id, label: str) -> str:
        r = actor.post("/v1/consents", json={
            "subject_label": label, "purpose": "test", "case_id": case_id})
        assert r.status_code == 200, r.text
        return r.json()["consent_id"]

    def finding(actor: Actor, case_id, phone: str) -> str:
        r = actor.post("/v1/lookup", json={
            "value": phone, "default_region": "ID", "case_id": case_id})
        assert r.status_code == 200, r.text
        return r.json()["finding_id"]

    def fence(actor: Actor, case_id, name: str, center) -> str:
        r = actor.post("/v1/geofences", json={
            "name": name, "kind": "circle", "center_lat": center[0],
            "center_lng": center[1], "radius_m": 500, "case_id": case_id})
        assert r.status_code == 200, r.text
        return r.json()["geofence_id"]

    w = World(
        manager=m, inv_a=inv_a, ana_a=ana_a, inv_b=inv_b, ana_b=ana_b,
        case_a=case_a, case_b=case_b,
        consent_a=consent(inv_a, case_a, "Subject A"),
        consent_b=consent(inv_b, case_b, "Subject B"),
        consent_none=consent(m, None, "Subject without case"),
        finding_a=finding(inv_a, case_a, "081211110001"),
        finding_b=finding(inv_b, case_b, "081222220002"),
        finding_none=finding(m, None, "081233330003"),
        fence_a=fence(ana_a, case_a, "Fence A", FENCE_A),
        fence_b=fence(ana_b, case_b, "Fence B", FENCE_B),
        fence_shared=fence(m, None, "Shared fence", FENCE_SHARED),
    )

    # Consent A: enter A, leave, enter shared  -> 3 points, 3 events.
    sid, tok = start_session(client, w.consent_a)
    for lat, lng in (FENCE_A, OUTSIDE, FENCE_SHARED):
        assert send_point(client, sid, tok, lat, lng).status_code == 200

    # Consent B: enter B, then a point INSIDE fence A (which belongs to case A and
    # must not be evaluated for B), then enter shared -> 3 points.
    sid, tok = start_session(client, w.consent_b)
    for lat, lng in (FENCE_B, FENCE_A, FENCE_SHARED):
        assert send_point(client, sid, tok, lat, lng).status_code == 200

    return w
