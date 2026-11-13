"""
PostgreSQL + PostGIS persistence layer.

Migrated from SQLite. Uses psycopg 3 with dict rows. Geospatial columns are real
PostGIS geography/geometry, so geofencing runs as ST_DWithin / ST_Contains
queries (see geo.py) instead of pure-Python math.

Connection string comes from DATABASE_URL, e.g.
    postgresql://osint:osint@db:5432/osint   (inside docker-compose)
    postgresql://osint:osint@localhost:5432/osint   (app on host, db in docker)
"""

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("DATABASE_URL",
                     "postgresql://osint:osint@localhost:5432/osint")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def get_conn() -> psycopg.Connection:
    return psycopg.connect(DSN, row_factory=dict_row)


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS users (
    id            text PRIMARY KEY,
    username      text NOT NULL UNIQUE,
    role          text NOT NULL,
    password_hash text NOT NULL,
    status        text NOT NULL DEFAULT 'active',
    created_at    timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id           text PRIMARY KEY,
    type         text NOT NULL,
    display_name text,
    confidence   numeric DEFAULT 1.0,
    created_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS identifiers (
    id               text PRIMARY KEY,
    entity_id        text NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    type             text NOT NULL,
    normalized_value text NOT NULL,
    created_at       timestamptz NOT NULL,
    UNIQUE(type, normalized_value)
);

CREATE TABLE IF NOT EXISTS findings (
    id           text PRIMARY KEY,
    entity_id    text REFERENCES entities(id) ON DELETE SET NULL,
    finding_type text NOT NULL,
    source       text NOT NULL,
    source_kind  text NOT NULL,
    data_json    jsonb NOT NULL,
    confidence   numeric DEFAULT 1.0,
    collected_at timestamptz NOT NULL,
    created_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
    id             text PRIMARY KEY,
    subject_label  text NOT NULL,
    purpose        text NOT NULL,
    policy_version text NOT NULL,
    granted_at     timestamptz NOT NULL,
    revoked_at     timestamptz
);

CREATE TABLE IF NOT EXISTS location_sessions (
    id          text PRIMARY KEY,
    consent_id  text NOT NULL REFERENCES consents(id) ON DELETE CASCADE,
    device_hint text,
    created_at  timestamptz NOT NULL,
    closed_at   timestamptz
);

CREATE TABLE IF NOT EXISTS location_points (
    id          text PRIMARY KEY,
    session_id  text NOT NULL REFERENCES location_sessions(id) ON DELETE CASCADE,
    consent_id  text NOT NULL REFERENCES consents(id) ON DELETE CASCADE,
    latitude    double precision NOT NULL,
    longitude   double precision NOT NULL,
    accuracy_m  numeric,
    geom        geography(Point, 4326),
    captured_at timestamptz NOT NULL,
    created_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS geofences (
    id           text PRIMARY KEY,
    name         text NOT NULL,
    kind         text NOT NULL,
    center_lat   double precision,
    center_lng   double precision,
    radius_m     double precision,
    polygon_json jsonb,
    polygon_geom geometry(Polygon, 4326),
    created_by   text REFERENCES users(id),
    created_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS geofence_events (
    id          text PRIMARY KEY,
    geofence_id text NOT NULL REFERENCES geofences(id) ON DELETE CASCADE,
    session_id  text NOT NULL REFERENCES location_sessions(id) ON DELETE CASCADE,
    consent_id  text NOT NULL REFERENCES consents(id) ON DELETE CASCADE,
    point_id    text,
    event_type  text NOT NULL,
    latitude    double precision NOT NULL,
    longitude   double precision NOT NULL,
    created_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS geofence_state (
    session_id  text NOT NULL,
    geofence_id text NOT NULL,
    inside      boolean NOT NULL,
    updated_at  timestamptz NOT NULL,
    PRIMARY KEY (session_id, geofence_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id            text PRIMARY KEY,
    actor         text NOT NULL DEFAULT 'researcher',
    action        text NOT NULL,
    resource_type text,
    resource_id   text,
    detail        text,
    created_at    timestamptz NOT NULL
);

-- Phase 3: per-consent retention policy (days of raw GPS to keep)
ALTER TABLE consents ADD COLUMN IF NOT EXISTS retention_days integer DEFAULT 30;

-- Per-session write token (sha256 of a secret returned only to the device that
-- opened the session). Sessions without one cannot accept points.
ALTER TABLE location_sessions ADD COLUMN IF NOT EXISTS write_token_hash text;

-- Optional phone identity linked to a consent (unifies OSINT entity + subject)
ALTER TABLE consents ADD COLUMN IF NOT EXISTS phone_e164 text;
ALTER TABLE consents ADD COLUMN IF NOT EXISTS entity_id text REFERENCES entities(id);

CREATE INDEX IF NOT EXISTS idx_location_points_geom ON location_points USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_location_points_session_time ON location_points(session_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_geofences_polygon ON geofences USING GIST (polygon_geom);
CREATE INDEX IF NOT EXISTS idx_findings_created ON findings(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_geoevents_created ON geofence_events(created_at DESC);
"""


def init_db(retries: int = 30, delay: float = 2.0) -> None:
    """Wait for the database, then apply the schema idempotently."""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            conn = get_conn()
            break
        except psycopg.OperationalError as exc:
            last_err = exc
            print(f"[db] waiting for database ({attempt + 1}/{retries})…")
            time.sleep(delay)
    else:
        raise RuntimeError(f"Database not reachable: {last_err}")

    try:
        with conn.cursor() as cur:
            for raw in SCHEMA.split(";"):
                # drop full-line comments, keep the actual statement
                stmt = "\n".join(
                    ln for ln in raw.splitlines() if not ln.strip().startswith("--")
                ).strip()
                if stmt:
                    cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def audit(action: str, resource_type: Optional[str] = None,
          resource_id: Optional[str] = None, detail: Optional[str] = None,
          actor: str = "researcher") -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO audit_events (id, actor, action, resource_type, resource_id, detail, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (new_id("aud"), actor, action, resource_type, resource_id, detail, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_entity_with_identifier(entity_type: str, display_name: str,
                                  id_type: str, normalized_value: str,
                                  confidence: float) -> str:
    """Exact-match entity resolution (never merges on a weak/partial signal)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT entity_id FROM identifiers WHERE type=%s AND normalized_value=%s",
            (id_type, normalized_value),
        ).fetchone()
        if row:
            return row["entity_id"]

        entity_id = new_id("ent")
        conn.execute(
            "INSERT INTO entities (id, type, display_name, confidence, created_at) VALUES (%s,%s,%s,%s,%s)",
            (entity_id, entity_type, display_name, confidence, now_iso()),
        )
        conn.execute(
            "INSERT INTO identifiers (id, entity_id, type, normalized_value, created_at) VALUES (%s,%s,%s,%s,%s)",
            (new_id("idf"), entity_id, id_type, normalized_value, now_iso()),
        )
        conn.commit()
        return entity_id
    finally:
        conn.close()


def rows_to_dicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
