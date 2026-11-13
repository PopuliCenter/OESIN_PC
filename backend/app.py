"""
OSINT & Consent-Based Location Intelligence — MVP API (Phase 1 + 2, PostGIS).

Data layer: PostgreSQL + PostGIS (see database.py). Geofencing uses real spatial
queries (geo.py: ST_DWithin / ST_Contains).

RBAC summary (admin allowed everywhere):
  lookup / consent write   -> investigator, case_manager
  findings / graph view    -> investigator, case_manager, location_analyst
  location points / fences -> location_analyst
  geofence events          -> location_analyst, case_manager
  audit                    -> auditor
  users                    -> admin
Consent-agent endpoints (public consent view, session start, point upload) are
intentionally UNauthenticated — driven by the subject's device, gated by an
active consent.

Run (docker):  docker compose up --build
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import psycopg
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

import analytics
import auth
import database as db
import enrichment as enr
import geo
import osint_online
import reports

POLICY_VERSION = "consent-policy-v1"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# --- Phase 3: precision tiers (SDD §11) -------------------------------------
# exact = raw; reduced ≈ 110 m; area ≈ 1.1 km. A role can never see data more
# precise than its cap, regardless of what it requests (FR-07).
PRECISION_DECIMALS = {"exact": None, "reduced": 3, "area": 2}
PRECISION_ORDER = ["exact", "reduced", "area"]   # most -> least precise
ROLE_MAX_PRECISION = {
    "admin": "exact", "location_analyst": "exact",
    "case_manager": "reduced", "investigator": "area", "auditor": "area",
}


def resolve_precision(requested: Optional[str], role: str) -> str:
    req = requested if requested in PRECISION_DECIMALS else "exact"
    cap = ROLE_MAX_PRECISION.get(role, "area")
    eff_idx = max(PRECISION_ORDER.index(req), PRECISION_ORDER.index(cap))
    return PRECISION_ORDER[eff_idx]


def apply_precision(points: list[dict[str, Any]], tier: str) -> list[dict[str, Any]]:
    dec = PRECISION_DECIMALS[tier]
    for p in points:
        if dec is not None:
            p["latitude"] = round(p["latitude"], dec)
            p["longitude"] = round(p["longitude"], dec)
            p["accuracy_m"] = None   # coarsened: withhold precise accuracy
        p["precision"] = tier
    return points

app = FastAPI(title="OSINT & Consent-Based Location Intelligence (MVP)",
              version="0.3.0")

# No CORS middleware on purpose: the dashboard is same-origin. With CORS '*'
# any website the operator visited could call the local API cross-origin
# (log in with the seed accounts and read every record).
#
# Host allowlist blocks DNS-rebinding: a hostile domain that resolves to
# 127.0.0.1 still sends its own Host header and is rejected. "app" is the
# internal compose hostname cloudflared forwards to.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "app", "*.trycloudflare.com"],
)


class _BodyTooLarge(StarletteHTTPException):
    """Raised mid-stream when a body passes the cap. Subclassing Starlette's
    HTTPException matters: FastAPI's body reader re-raises HTTPExceptions
    untouched but turns any other exception into a generic 400."""

    def __init__(self):
        super().__init__(status_code=413, detail="Request body too large.")


class BodyLimitMiddleware:
    """Cap request bodies, counting bytes as they stream in, so chunked bodies
    without a Content-Length are capped too. Public (tunnel) requests get a
    small cap; local operator requests a generous one."""

    def __init__(self, app, public_max: int = 16 * 1024, local_max: int = 2 * 1024 * 1024):
        self.app, self.public_max, self.local_max = app, public_max, local_max

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        public = b"cf-connecting-ip" in headers or b"cf-ray" in headers
        limit = self.public_max if public else self.local_max

        async def reject():
            await JSONResponse(status_code=413, content={"error": "Request body too large."})(
                scope, receive, send)

        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                if int(cl) > limit:
                    return await reject()
            except ValueError:
                return await reject()

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
            return msg

        async def tracking_send(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await reject()


app.add_middleware(BodyLimitMiddleware)


@app.exception_handler(RequestValidationError)
def validation_error_handler(_, exc: RequestValidationError):
    # Echo only location/message/type. The default handler echoes the raw
    # input, and a NaN/Infinity input then crashes JSON encoding (500).
    errs = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()]
    return JSONResponse(status_code=422,
                        content={"error": "Invalid request.", "detail": jsonable_encoder(errs)})


# ---------------------------------------------------------------------------
# Public tunnel guard
# ---------------------------------------------------------------------------
# Requests that arrive through the Cloudflare tunnel carry Cloudflare's headers
# (cf-connecting-ip / cf-ray). Those requests come from the public internet, so
# they may ONLY reach the consent-agent surface. The operator dashboard, login
# and every operator API stay reachable from localhost only. Local requests
# never carry these headers; spoofing them can only restrict the spoofer.
TUNNEL_METRICS_URL = os.environ.get("TUNNEL_METRICS_URL")

_PUBLIC_ALLOW = [
    ("GET", re.compile(r"^/consent$")),
    ("GET", re.compile(r"^/static/style\.css$")),
    ("GET", re.compile(r"^/v1/public/consent/[A-Za-z0-9_]{1,64}$")),
    ("POST", re.compile(r"^/v1/location/sessions$")),
    ("POST", re.compile(r"^/v1/location/sessions/[A-Za-z0-9_]{1,64}/close$")),
    ("POST", re.compile(r"^/v1/location/points$")),
]
_RL_MAX, _RL_WINDOW = 120, 60.0          # per client: 120 requests / minute
_RL_TABLE_CAP = 10_000
_rl_hits: dict[str, deque] = {}


def _via_tunnel(request) -> bool:
    h = request.headers
    return "cf-connecting-ip" in h or "cf-ray" in h


def _client_key(ip: str) -> str:
    """IPv4 per address; IPv6 per /64, since one host can rotate through a /64."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _rate_ok(key: str) -> bool:
    now = time.monotonic()
    dq = _rl_hits.get(key) or deque()
    while dq and now - dq[0] > _RL_WINDOW:
        dq.popleft()
    if len(dq) >= _RL_MAX:
        _rl_hits[key] = dq
        return False
    dq.append(now)
    _rl_hits[key] = dq
    if len(_rl_hits) > _RL_TABLE_CAP:    # evict clients idle for a full window
        for k in [k for k, v in _rl_hits.items() if not v or now - v[-1] > _RL_WINDOW]:
            del _rl_hits[k]
    return True


@app.middleware("http")
async def tunnel_guard(request, call_next):
    if _via_tunnel(request):
        method, path = request.method, request.url.path
        if not any(m == method and rx.match(path) for m, rx in _PUBLIC_ALLOW):
            return JSONResponse(status_code=403, content={
                "error": "Not available via the public link. Only the consent agent is public."})
        ip = request.headers.get("cf-connecting-ip", "unknown")
        if not _rate_ok(_client_key(ip)):
            return JSONResponse(status_code=429, content={"error": "Too many requests."})
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    auth.seed_users()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    password: str = Field(..., min_length=6)
    role: str


class LookupRequest(BaseModel):
    value: str
    default_region: Optional[str] = None


class ConsentCreate(BaseModel):
    subject_label: str
    purpose: str
    retention_days: int = Field(30, ge=1, le=3650)
    phone: Optional[str] = None            # optional: link a phone identity to the subject
    phone_region: Optional[str] = "ID"


class PhoneLink(BaseModel):
    phone: str
    phone_region: Optional[str] = "ID"


# Server-generated ids are prefix + hex (database.new_id); accept nothing else.
# This also keeps NUL and other control bytes away from Postgres.
_ID_PATTERN = r"^[A-Za-z0-9_]{1,64}$"


class SessionCreate(BaseModel):
    consent_id: str = Field(..., pattern=_ID_PATTERN)
    device_hint: Optional[str] = Field(None, max_length=200)

    @field_validator("device_hint")
    @classmethod
    def _printable_hint(cls, v: Optional[str]) -> Optional[str]:
        # Postgres text cannot hold NUL; strip all control characters.
        return None if v is None else "".join(ch for ch in v if ch.isprintable())


class SessionClose(BaseModel):
    session_token: str = Field(..., min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


# Accepted clock skew for a client-supplied capture time. Anything outside is
# rejected, so a forged timestamp can neither poison the operator views
# ('infinity', year 10000) nor dodge or game the retention purge.
CAPTURED_PAST_MAX = timedelta(hours=24)
CAPTURED_FUTURE_MAX = timedelta(minutes=5)


class PointCreate(BaseModel):
    session_id: str = Field(..., pattern=_ID_PATTERN)
    session_token: str = Field(..., min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    latitude: float = Field(..., ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(..., ge=-180, le=180, allow_inf_nan=False)
    accuracy_m: Optional[float] = Field(None, ge=0, le=100_000, allow_inf_nan=False)
    captured_at: Optional[datetime] = None


class GeofenceCreate(BaseModel):
    name: str
    kind: str = Field(..., pattern="^(circle|polygon)$")
    center_lat: Optional[float] = Field(None, ge=-90, le=90)
    center_lng: Optional[float] = Field(None, ge=-180, le=180)
    radius_m: Optional[float] = Field(None, gt=0)
    polygon: Optional[list[list[float]]] = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.post("/v1/auth/login")
def login(req: LoginRequest) -> dict[str, Any]:
    user = auth.authenticate(req.username, req.password)
    if not user:
        db.audit("LOGIN_FAILED", "user", req.username, actor=req.username)
        raise HTTPException(401, "Invalid credentials.")
    token = auth.create_token(user["id"], user["username"], user["role"])
    db.audit("LOGIN", "user", user["id"], user["role"], actor=user["username"])
    return {"token": token, "user": user, "ttl_minutes": auth.TOKEN_TTL_MINUTES}


@app.get("/v1/auth/me")
def me(user: dict = Depends(auth.current_user)) -> dict[str, Any]:
    return user


@app.get("/v1/public-url")
def public_url(user: dict = Depends(auth.current_user)) -> dict[str, Any]:
    """Public HTTPS base URL of the Cloudflare quick tunnel, if it is running."""
    if not TUNNEL_METRICS_URL:
        return {"base_url": None, "reason": "tunnel not configured"}
    try:
        with urllib.request.urlopen(f"{TUNNEL_METRICS_URL}/quicktunnel", timeout=3) as r:
            hostname = json.loads(r.read()).get("hostname")
    except Exception as exc:  # tunnel down / still starting
        return {"base_url": None, "reason": f"tunnel unavailable: {exc.__class__.__name__}"}
    if not hostname:
        return {"base_url": None, "reason": "tunnel starting"}
    return {"base_url": f"https://{hostname}"}


@app.get("/v1/users")
def list_users(user: dict = Depends(auth.require_roles("admin"))) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, username, role, status, created_at FROM users ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    return {"users": db.rows_to_dicts(rows), "roles": auth.ROLES}


@app.post("/v1/users")
def create_user(req: UserCreate,
                user: dict = Depends(auth.require_roles("admin"))) -> dict[str, Any]:
    if req.role not in auth.ROLES:
        raise HTTPException(422, f"Unknown role. Allowed: {auth.ROLES}")
    conn = db.get_conn()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=%s", (req.username,)).fetchone():
            raise HTTPException(409, "Username already exists.")
        uid = db.new_id("usr")
        conn.execute(
            "INSERT INTO users (id, username, role, password_hash, status, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (uid, req.username, req.role, auth.hash_password(req.password),
             "active", db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    db.audit("USER_CREATE", "user", uid, req.role, actor=user["username"])
    return {"id": uid, "username": req.username, "role": req.role}


# ---------------------------------------------------------------------------
# OSINT enrichment
# ---------------------------------------------------------------------------
@app.post("/v1/lookup")
def lookup(req: LookupRequest,
           user: dict = Depends(auth.require_roles("investigator", "case_manager"))
           ) -> dict[str, Any]:
    id_type = enr.classify_identifier(req.value)
    if id_type == "unknown":
        raise HTTPException(422, "Could not classify identifier.")

    normalized = enr.normalize(id_type, req.value)
    display = normalized or req.value

    if id_type == "phone":
        finding_data = enr.enrich_phone(req.value, req.default_region)
        normalized = finding_data.get("e164") or normalized
        display = normalized or req.value
        confidence = 0.99 if finding_data.get("valid") else 0.3
    elif id_type == "ip":
        finding_data = osint_online.ip_geolocation(normalized or req.value)
        confidence = 0.8 if finding_data.get("status") == "success" else 0.3
    elif id_type == "username":
        finding_data = osint_online.username_scan(normalized or req.value)
        confidence = 0.7 if finding_data.get("found_count") else 0.4
    else:
        finding_data = {"type": id_type, "normalized": normalized,
                        "source": "normalizer (offline)", "source_kind": "offline_local"}
        confidence = 0.9 if normalized else 0.3

    entity_id = None
    if normalized:
        entity_id = db.upsert_entity_with_identifier(
            id_type, display, id_type, normalized, confidence)

    finding_id = db.new_id("fnd")
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO findings (id, entity_id, finding_type, source, source_kind,"
            " data_json, confidence, collected_at, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (finding_id, entity_id, f"{id_type}_enrichment",
             finding_data.get("source", "normalizer"),
             finding_data.get("source_kind", "offline_local"),
             json.dumps(finding_data), confidence, db.now_iso(), db.now_iso()))
        conn.commit()
    finally:
        conn.close()

    db.audit("LOOKUP", "identifier", normalized or req.value, id_type, actor=user["username"])
    return {"identifier_type": id_type, "normalized": normalized,
            "confidence": confidence, "entity_id": entity_id,
            "finding_id": finding_id, "enrichment": finding_data}


@app.get("/v1/findings")
def list_findings(limit: int = 50,
                  user: dict = Depends(
                      auth.require_roles("investigator", "case_manager", "location_analyst"))
                  ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, entity_id, finding_type, source, source_kind, data_json,"
            " confidence, created_at FROM findings ORDER BY created_at DESC LIMIT %s",
            (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = d.pop("data_json")   # jsonb -> already a dict
        out.append(d)
    return {"findings": out}


# ---------------------------------------------------------------------------
# Consent lifecycle
# ---------------------------------------------------------------------------
@app.post("/v1/consents")
def create_consent(req: ConsentCreate,
                   user: dict = Depends(auth.require_roles("investigator", "case_manager"))
                   ) -> dict[str, Any]:
    # Optional: resolve a phone identity and link it to this subject. Uses the
    # same entity resolution as /v1/lookup, so a number already looked up maps
    # to the SAME entity (OSINT identity and tracked subject become one).
    phone_e164 = None
    entity_id = None
    if req.phone and req.phone.strip():
        enrich = enr.enrich_phone(req.phone, req.phone_region or "ID")
        phone_e164 = enrich.get("e164") or enr.normalize("phone", req.phone) or req.phone.strip()
        entity_id = db.upsert_entity_with_identifier(
            "phone", phone_e164, "phone", phone_e164,
            0.99 if enrich.get("valid") else 0.5)

    cid = db.new_id("cns")
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO consents (id, subject_label, purpose, policy_version,"
            " retention_days, phone_e164, entity_id, granted_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (cid, req.subject_label, req.purpose, POLICY_VERSION,
             req.retention_days, phone_e164, entity_id, db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    db.audit("CONSENT_GRANT", "consent", cid,
             f"{req.purpose} (retention {req.retention_days}d"
             + (f", phone {phone_e164}" if phone_e164 else "") + ")",
             actor=user["username"])
    return {"consent_id": cid, "policy_version": POLICY_VERSION, "status": "active",
            "retention_days": req.retention_days, "phone_e164": phone_e164,
            "entity_id": entity_id, "agent_link": f"/consent?consent_id={cid}"}


@app.post("/v1/consents/{consent_id}/revoke")
def revoke_consent(consent_id: str,
                   user: dict = Depends(auth.require_roles("investigator", "case_manager"))
                   ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT id, revoked_at FROM consents WHERE id=%s",
                           (consent_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Consent not found.")
        if row["revoked_at"]:
            return {"consent_id": consent_id, "status": "already_revoked"}
        ts = db.now_iso()
        conn.execute("UPDATE consents SET revoked_at=%s WHERE id=%s", (ts, consent_id))
        conn.execute(
            "UPDATE location_sessions SET closed_at=%s WHERE consent_id=%s AND closed_at IS NULL",
            (ts, consent_id))
        conn.commit()
    finally:
        conn.close()
    db.audit("CONSENT_REVOKE", "consent", consent_id, actor=user["username"])
    return {"consent_id": consent_id, "status": "revoked", "revoked_at": ts}


@app.post("/v1/consents/{consent_id}/link-phone")
def link_phone(consent_id: str, req: PhoneLink,
               user: dict = Depends(auth.require_roles("investigator", "case_manager"))
               ) -> dict[str, Any]:
    """Attach (or replace) a phone identity on an existing consent."""
    if not req.phone or not req.phone.strip():
        raise HTTPException(422, "Phone is required.")
    enrich = enr.enrich_phone(req.phone, req.phone_region or "ID")
    phone_e164 = enrich.get("e164") or enr.normalize("phone", req.phone) or req.phone.strip()
    entity_id = db.upsert_entity_with_identifier(
        "phone", phone_e164, "phone", phone_e164,
        0.99 if enrich.get("valid") else 0.5)

    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM consents WHERE id=%s", (consent_id,)).fetchone():
            raise HTTPException(404, "Consent not found.")
        conn.execute("UPDATE consents SET phone_e164=%s, entity_id=%s WHERE id=%s",
                     (phone_e164, entity_id, consent_id))
        conn.commit()
    finally:
        conn.close()
    db.audit("CONSENT_LINK_PHONE", "consent", consent_id, phone_e164, actor=user["username"])
    return {"consent_id": consent_id, "phone_e164": phone_e164, "entity_id": entity_id}


@app.get("/v1/consents")
def list_consents(user: dict = Depends(
        auth.require_roles("investigator", "case_manager", "location_analyst"))
        ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT * FROM consents ORDER BY granted_at DESC").fetchall()
    finally:
        conn.close()
    items = []
    for r in db.rows_to_dicts(rows):
        r["status"] = "revoked" if r["revoked_at"] else "active"
        r["agent_link"] = f"/consent?consent_id={r['id']}"
        items.append(r)
    return {"consents": items}


@app.get("/v1/public/consent/{consent_id}")
def public_consent(consent_id: str) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT id, subject_label, purpose, policy_version, revoked_at"
            " FROM consents WHERE id=%s", (consent_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Consent not found.")
    if row["revoked_at"]:
        # Revoked: disclose nothing but the status.
        return {"id": row["id"], "status": "revoked"}
    d = dict(row)
    d.pop("revoked_at")
    d["status"] = "active"
    return d


# ---------------------------------------------------------------------------
# Location sessions + points (public, consent-gated) + geofence evaluation
# ---------------------------------------------------------------------------
def _active_consent_or_404(conn, consent_id: str):
    row = conn.execute("SELECT id, revoked_at FROM consents WHERE id=%s",
                       (consent_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Consent not found.")
    if row["revoked_at"]:
        raise HTTPException(403, "Consent has been revoked; collection is not allowed.")
    return row


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _session_with_token_or_403(conn, session_id: str, token: str):
    """Load a session and check its write token. The token is returned only to
    the device that opened the session, so holding the consent link alone is
    not enough to write into (or close) someone else's session."""
    sess = conn.execute(
        "SELECT id, consent_id, closed_at, write_token_hash FROM location_sessions"
        " WHERE id=%s", (session_id,)).fetchone()
    if not sess:
        raise HTTPException(404, "Session not found.")
    stored = sess["write_token_hash"]
    if not stored or not hmac.compare_digest(stored, _token_hash(token)):
        raise HTTPException(403, "Invalid session token.")
    return sess


@app.post("/v1/location/sessions")
def start_session(req: SessionCreate) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    conn = db.get_conn()
    try:
        _active_consent_or_404(conn, req.consent_id)
        sid = db.new_id("locsess")
        conn.execute(
            "INSERT INTO location_sessions (id, consent_id, device_hint,"
            " write_token_hash, created_at) VALUES (%s,%s,%s,%s,%s)",
            (sid, req.consent_id, req.device_hint, _token_hash(token), db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    db.audit("LOCATION_SESSION_START", "location_session", sid, req.consent_id,
             actor="subject-agent")
    return {"session_id": sid, "session_token": token}


@app.post("/v1/location/sessions/{session_id}/close")
def close_session(session_id: str, req: SessionClose) -> dict[str, Any]:
    """Subject pressed Stop: close the session server-side."""
    conn = db.get_conn()
    try:
        sess = _session_with_token_or_403(conn, session_id, req.session_token)
        if sess["closed_at"]:
            return {"session_id": session_id, "status": "already_closed"}
        conn.execute("UPDATE location_sessions SET closed_at=%s WHERE id=%s",
                     (db.now_iso(), session_id))
        conn.commit()
    finally:
        conn.close()
    db.audit("LOCATION_SESSION_STOP", "location_session", session_id, actor="subject-agent")
    return {"session_id": session_id, "status": "closed"}


@app.post("/v1/location/points")
def upload_point(req: PointCreate) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    captured = now
    if req.captured_at is not None:
        captured = req.captured_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        if not (now - CAPTURED_PAST_MAX <= captured <= now + CAPTURED_FUTURE_MAX):
            raise HTTPException(422, "captured_at outside the accepted window "
                                     "(last 24 hours, up to 5 minutes ahead).")
    conn = db.get_conn()
    try:
        sess = _session_with_token_or_403(conn, req.session_id, req.session_token)
        if sess["closed_at"]:
            raise HTTPException(403, "Session is closed.")
        _active_consent_or_404(conn, sess["consent_id"])

        pid = db.new_id("pt")
        conn.execute(
            "INSERT INTO location_points (id, session_id, consent_id, latitude,"
            " longitude, accuracy_m, geom, captured_at, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s,%s)",
            (pid, req.session_id, sess["consent_id"], req.latitude, req.longitude,
             req.accuracy_m, req.longitude, req.latitude, captured.isoformat(), db.now_iso()))
        events = geo.evaluate_point(conn, req.session_id, sess["consent_id"],
                                    pid, req.latitude, req.longitude)
        conn.commit()
    finally:
        conn.close()
    for ev in events:
        db.audit(f"GEOFENCE_{ev['event_type'].upper()}", "geofence",
                 ev["geofence_id"], ev["geofence_name"], actor="subject-agent")
    db.audit("LOCATION_POINT_INGEST", "location_point", pid, req.session_id,
             actor="subject-agent")
    # Public response: no geofence ids/names (operator-side data). Operators
    # see enter/exit events in the dashboard.
    return {"point_id": pid, "status": "stored"}


@app.get("/v1/location/points")
def list_points(consent_id: Optional[str] = None, limit: int = 500,
                precision: str = "exact", order: str = "desc",
                user: dict = Depends(
                    auth.require_roles("location_analyst", "case_manager"))
                ) -> dict[str, Any]:
    cols = ("id, session_id, consent_id, latitude, longitude, accuracy_m,"
            " captured_at, created_at")
    direction = "ASC" if order.lower() == "asc" else "DESC"   # asc = chronological (timeline)
    conn = db.get_conn()
    try:
        if consent_id:
            rows = conn.execute(
                f"SELECT {cols} FROM location_points WHERE consent_id=%s"
                f" ORDER BY captured_at {direction} LIMIT %s", (consent_id, limit)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM location_points ORDER BY captured_at {direction} LIMIT %s",
                (limit,)).fetchall()
    finally:
        conn.close()
    tier = resolve_precision(precision, user["role"])
    points = apply_precision(db.rows_to_dicts(rows), tier)
    db.audit("LOCATION_VIEW", "location_point", consent_id or "all",
             f"precision={tier}", actor=user["username"])
    return {"points": points, "precision": tier}


# ---------------------------------------------------------------------------
# Geofences (PostGIS)
# ---------------------------------------------------------------------------
@app.post("/v1/geofences")
def create_geofence(req: GeofenceCreate,
                    user: dict = Depends(auth.require_roles("location_analyst"))
                    ) -> dict[str, Any]:
    gid = db.new_id("gf")
    conn = db.get_conn()
    try:
        if req.kind == "circle":
            if req.center_lat is None or req.center_lng is None or req.radius_m is None:
                raise HTTPException(422, "circle requires center_lat, center_lng, radius_m.")
            conn.execute(
                "INSERT INTO geofences (id, name, kind, center_lat, center_lng, radius_m,"
                " polygon_json, polygon_geom, created_by, created_at)"
                " VALUES (%s,%s,'circle',%s,%s,%s, NULL, NULL, %s,%s)",
                (gid, req.name, req.center_lat, req.center_lng, req.radius_m,
                 user["id"], db.now_iso()))
        else:
            if not req.polygon or len(req.polygon) < 3:
                raise HTTPException(422, "polygon requires >=3 [lat,lng] vertices.")
            wkt = geo.polygon_wkt(req.polygon)
            conn.execute(
                "INSERT INTO geofences (id, name, kind, center_lat, center_lng, radius_m,"
                " polygon_json, polygon_geom, created_by, created_at)"
                " VALUES (%s,%s,'polygon', NULL, NULL, NULL, %s,"
                " ST_GeomFromText(%s,4326), %s,%s)",
                (gid, req.name, json.dumps(req.polygon), wkt, user["id"], db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    db.audit("GEOFENCE_CREATE", "geofence", gid, req.name, actor=user["username"])
    return {"geofence_id": gid, "name": req.name, "kind": req.kind}


@app.get("/v1/geofences")
def list_geofences(user: dict = Depends(
        auth.require_roles("investigator", "case_manager", "location_analyst"))
        ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name, kind, center_lat, center_lng, radius_m, polygon_json,"
            " created_by, created_at FROM geofences ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    items = []
    for r in db.rows_to_dicts(rows):
        r["polygon"] = r.pop("polygon_json", None)   # jsonb -> list already
        items.append(r)
    return {"geofences": items}


@app.delete("/v1/geofences/{geofence_id}")
def delete_geofence(geofence_id: str,
                    user: dict = Depends(auth.require_roles("location_analyst"))
                    ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM geofences WHERE id=%s", (geofence_id,))
        conn.execute("DELETE FROM geofence_state WHERE geofence_id=%s", (geofence_id,))
        conn.commit()
    finally:
        conn.close()
    db.audit("GEOFENCE_DELETE", "geofence", geofence_id, actor=user["username"])
    return {"status": "deleted", "geofence_id": geofence_id}


@app.get("/v1/geofence/events")
def geofence_events(limit: int = 200,
                    user: dict = Depends(
                        auth.require_roles("location_analyst", "case_manager"))
                    ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT e.*, g.name AS geofence_name FROM geofence_events e"
            " LEFT JOIN geofences g ON g.id = e.geofence_id"
            " ORDER BY e.created_at DESC LIMIT %s", (limit,)).fetchall()
    finally:
        conn.close()
    return {"events": db.rows_to_dicts(rows)}


# ---------------------------------------------------------------------------
# Phase 3: retention policy enforcement (SDD §11)
# ---------------------------------------------------------------------------
_EXPIRED_FILTER = ("p.captured_at < now() -"
                   " (COALESCE(c.retention_days,30) || ' days')::interval")


@app.get("/v1/retention/preview")
def retention_preview(user: dict = Depends(auth.require_roles("admin"))) -> dict[str, Any]:
    """Dry-run: how many raw GPS points are past their retention window."""
    conn = db.get_conn()
    try:
        rows = conn.execute(f"""
            SELECT c.id, c.subject_label,
                   COALESCE(c.retention_days,30) AS retention_days,
                   COUNT(p.id) AS total,
                   COUNT(p.id) FILTER (WHERE {_EXPIRED_FILTER}) AS expired
            FROM consents c
            LEFT JOIN location_points p ON p.consent_id = c.id
            GROUP BY c.id, c.subject_label, c.retention_days
            ORDER BY expired DESC
        """).fetchall()
    finally:
        conn.close()
    items = db.rows_to_dicts(rows)
    return {"total_expired": sum(r["expired"] for r in items), "by_consent": items}


@app.post("/v1/retention/run")
def retention_run(user: dict = Depends(auth.require_roles("admin"))) -> dict[str, Any]:
    """Delete raw GPS points older than each consent's retention window."""
    conn = db.get_conn()
    try:
        cur = conn.execute(f"""
            DELETE FROM location_points p
            USING consents c
            WHERE p.consent_id = c.id AND {_EXPIRED_FILTER}
        """)
        deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    db.audit("RETENTION_PURGE", "location_point", None,
             f"deleted={deleted}", actor=user["username"])
    return {"deleted": deleted, "status": "purged"}


# ---------------------------------------------------------------------------
# Spatial analytics (PostGIS)
# ---------------------------------------------------------------------------
@app.get("/v1/analytics/movement")
def analytics_movement(consent_id: str, from_ts: Optional[str] = None,
                       to_ts: Optional[str] = None,
                       user: dict = Depends(
                           auth.require_roles("location_analyst", "case_manager"))
                       ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        result = analytics.movement_summary(conn, consent_id, from_ts, to_ts)
    finally:
        conn.close()
    db.audit("ANALYTICS_MOVEMENT", "consent", consent_id, actor=user["username"])
    return result


@app.get("/v1/analytics/activity-area")
def analytics_activity_area(consent_id: str,
                            user: dict = Depends(
                                auth.require_roles("location_analyst", "case_manager"))
                            ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        result = analytics.activity_area(conn, consent_id)
    finally:
        conn.close()
    db.audit("ANALYTICS_AREA", "consent", consent_id, actor=user["username"])
    return result


@app.get("/v1/analytics/heatmap")
def analytics_heatmap(consent_id: Optional[str] = None, cell_m: int = 250,
                      geofence_id: Optional[str] = None,
                      user: dict = Depends(
                          auth.require_roles("location_analyst", "case_manager"))
                      ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        result = analytics.heatmap_grid(conn, consent_id, cell_m, geofence_id)
    finally:
        conn.close()
    db.audit("ANALYTICS_HEATMAP", "consent", consent_id or "all", actor=user["username"])
    return result


@app.get("/v1/analytics/dwell")
def analytics_dwell(consent_id: Optional[str] = None,
                    user: dict = Depends(
                        auth.require_roles("location_analyst", "case_manager"))
                    ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        result = analytics.dwell_by_geofence(conn, consent_id)
    finally:
        conn.close()
    return result


@app.get("/v1/analytics/in-geofence")
def analytics_in_geofence(geofence_id: str, from_ts: Optional[str] = None,
                          to_ts: Optional[str] = None, limit: int = 1000,
                          user: dict = Depends(auth.require_roles("location_analyst"))
                          ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        result = analytics.points_in_geofence(conn, geofence_id, from_ts, to_ts, limit)
    finally:
        conn.close()
    db.audit("ANALYTICS_IN_GEOFENCE", "geofence", geofence_id, actor=user["username"])
    return result


# ---------------------------------------------------------------------------
# Case reports: CSV data exports + formatted PDF
# ---------------------------------------------------------------------------
def _attach(content, media_type: str, filename: str) -> Response:
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/v1/reports/consent/{consent_id}/points.csv")
def report_points_csv(consent_id: str,
                      user: dict = Depends(auth.require_roles("location_analyst"))
                      ) -> Response:
    conn = db.get_conn()
    try:
        data = reports.points_csv(conn, consent_id)
    finally:
        conn.close()
    db.audit("EXPORT_POINTS_CSV", "consent", consent_id, actor=user["username"])
    return _attach(data, "text/csv", f"points_{consent_id}.csv")


@app.get("/v1/reports/consent/{consent_id}/events.csv")
def report_events_csv(consent_id: str,
                      user: dict = Depends(
                          auth.require_roles("location_analyst", "case_manager"))
                      ) -> Response:
    conn = db.get_conn()
    try:
        data = reports.events_csv(conn, consent_id)
    finally:
        conn.close()
    db.audit("EXPORT_EVENTS_CSV", "consent", consent_id, actor=user["username"])
    return _attach(data, "text/csv", f"geofence_events_{consent_id}.csv")


@app.get("/v1/reports/findings.csv")
def report_findings_csv(user: dict = Depends(
        auth.require_roles("investigator", "case_manager"))) -> Response:
    conn = db.get_conn()
    try:
        data = reports.findings_csv(conn)
    finally:
        conn.close()
    db.audit("EXPORT_FINDINGS_CSV", "findings", "all", actor=user["username"])
    return _attach(data, "text/csv", "osint_findings.csv")


@app.get("/v1/reports/consent/{consent_id}/report.pdf")
def report_pdf(consent_id: str,
               user: dict = Depends(
                   auth.require_roles("location_analyst", "case_manager"))
               ) -> Response:
    conn = db.get_conn()
    try:
        pdf = reports.build_pdf(conn, consent_id, user["username"], db.now_iso())
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    finally:
        conn.close()
    db.audit("EXPORT_CASE_PDF", "consent", consent_id, actor=user["username"])
    return _attach(pdf, "application/pdf", f"case_report_{consent_id}.pdf")


# ---------------------------------------------------------------------------
# Entity relationship graph
# ---------------------------------------------------------------------------
@app.get("/v1/graph")
def graph(user: dict = Depends(
        auth.require_roles("investigator", "case_manager", "location_analyst"))
        ) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        entities = db.rows_to_dicts(conn.execute("SELECT * FROM entities").fetchall())
        identifiers = db.rows_to_dicts(conn.execute("SELECT * FROM identifiers").fetchall())
        consents = db.rows_to_dicts(conn.execute("SELECT * FROM consents").fetchall())
        sessions = db.rows_to_dicts(conn.execute(
            "SELECT id, consent_id FROM location_sessions").fetchall())
        geofences = db.rows_to_dicts(conn.execute(
            "SELECT id, name, kind FROM geofences").fetchall())
        gf_edges = db.rows_to_dicts(conn.execute(
            "SELECT DISTINCT consent_id, geofence_id FROM geofence_events").fetchall())
        point_counts = {r["consent_id"]: r["n"] for r in conn.execute(
            "SELECT consent_id, COUNT(*) AS n FROM location_points GROUP BY consent_id"
        ).fetchall()}
    finally:
        conn.close()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for e in entities:
        nodes.append({"id": e["id"], "label": e["display_name"] or e["type"],
                      "group": f"entity:{e['type']}", "title": f"Entity ({e['type']})"})
    for i in identifiers:
        nodes.append({"id": i["id"], "label": i["normalized_value"],
                      "group": f"id:{i['type']}", "title": f"{i['type']} identifier"})
        edges.append({"from": i["entity_id"], "to": i["id"],
                      "label": f"HAS_{i['type'].upper()}"})
    for c in consents:
        status = "revoked" if c["revoked_at"] else "active"
        pts = point_counts.get(c["id"], 0)
        nodes.append({"id": c["id"], "label": f"{c['subject_label']}\n({status}, {pts} pts)",
                      "group": "consent", "title": f"Consent · {c['purpose']}"})
        if c.get("entity_id"):   # linked phone identity -> unify with OSINT entity
            edges.append({"from": c["id"], "to": c["entity_id"], "label": "HAS_PHONE"})
    for s in sessions:
        nodes.append({"id": s["id"], "label": "session", "group": "session",
                      "title": s["id"]})
        edges.append({"from": s["consent_id"], "to": s["id"], "label": "SESSION"})
    for g in geofences:
        nodes.append({"id": g["id"], "label": g["name"], "group": "geofence",
                      "title": f"Geofence ({g['kind']})"})
    seen = set()
    for ge in gf_edges:
        key = (ge["consent_id"], ge["geofence_id"])
        if key in seen:
            continue
        seen.add(key)
        edges.append({"from": ge["consent_id"], "to": ge["geofence_id"],
                      "label": "OBSERVED_AT", "dashes": True})

    return {"nodes": nodes, "edges": edges,
            "counts": {"nodes": len(nodes), "edges": len(edges)}}


# ---------------------------------------------------------------------------
# Audit + summary
# ---------------------------------------------------------------------------
@app.get("/v1/audit")
def list_audit(limit: int = 100,
               user: dict = Depends(auth.require_roles("auditor"))) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    finally:
        conn.close()
    return {"events": db.rows_to_dicts(rows)}


@app.get("/v1/summary")
def summary(user: dict = Depends(auth.current_user)) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        def count(table: str, where: str = "") -> int:
            return conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} {where}").fetchone()["n"]
        return {
            "entities": count("entities"),
            "findings": count("findings"),
            "consents_active": count("consents", "WHERE revoked_at IS NULL"),
            "consents_total": count("consents"),
            "location_points": count("location_points"),
            "geofences": count("geofences"),
            "geofence_events": count("geofence_events"),
            "audit_events": count("audit_events"),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/consent")
def consent_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "consent.html")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# Registered on Starlette's base class so framework-raised errors (404/405,
# FastAPI's body-parse 400, the 413 body cap) use the same {"error": ...}
# shape as our own HTTPExceptions, which the frontend reads.
@app.exception_handler(StarletteHTTPException)
def http_exc_handler(_, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(psycopg.DataError)
def db_data_error_handler(_, exc: psycopg.DataError):
    # Backstop: input Postgres cannot store (e.g. NUL bytes) is a client error,
    # not a crash. Details stay in the server log, not the response.
    return JSONResponse(status_code=422, content={"error": "Invalid input value."})
