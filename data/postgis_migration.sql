-- =============================================================================
-- PostGIS migration path for the location + geofence subsystem.
--
-- The MVP runs geofencing in pure Python on SQLite (backend/geo.py). When you
-- move to PostgreSQL + PostGIS, use the schema below and replace the Python
-- containment checks with the SQL shown at the bottom. Table/column names match
-- the SQLite schema so the application layer changes very little.
--
-- Setup (once):
--   CREATE EXTENSION IF NOT EXISTS postgis;
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- Consent-based GPS points as a real geography column (SDD §8) ----------------
CREATE TABLE location_points (
    id          text PRIMARY KEY,
    session_id  text NOT NULL,
    consent_id  text NOT NULL,
    geom        geography(Point, 4326) NOT NULL,   -- lon/lat
    accuracy_m  numeric,
    captured_at timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_location_points_geom
    ON location_points USING GIST (geom);
CREATE INDEX idx_location_points_session_time
    ON location_points (session_id, captured_at DESC);

-- Geofences: circle (center + radius) or polygon -----------------------------
CREATE TABLE geofences (
    id         text PRIMARY KEY,
    name       text NOT NULL,
    kind       text NOT NULL CHECK (kind IN ('circle','polygon')),
    center     geography(Point, 4326),             -- for circle
    radius_m   numeric,                            -- for circle
    area       geography(Polygon, 4326),           -- for polygon
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_geofences_area ON geofences USING GIST (area);

CREATE TABLE geofence_events (
    id          text PRIMARY KEY,
    geofence_id text NOT NULL REFERENCES geofences(id) ON DELETE CASCADE,
    session_id  text NOT NULL,
    consent_id  text NOT NULL,
    point_id    text,
    event_type  text NOT NULL CHECK (event_type IN ('enter','exit')),
    geom        geography(Point, 4326) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Inserting a point (lon, lat) -----------------------------------------------
--   INSERT INTO location_points (id, session_id, consent_id, geom, accuracy_m, captured_at)
--   VALUES ($1, $2, $3, ST_MakePoint($lon, $lat)::geography, $acc, $ts);

-- Which geofences does a point fall inside? (replaces geo.point_in_geofence) --
--   SELECT id, name FROM geofences
--   WHERE (kind = 'circle'  AND ST_DWithin(center, ST_MakePoint($lon,$lat)::geography, radius_m))
--      OR (kind = 'polygon' AND ST_Contains(area::geometry, ST_MakePoint($lon,$lat)::geometry));

-- Distance between two points, in metres --------------------------------------
--   SELECT ST_Distance(a.geom, b.geom) FROM ... ;

-- Points within a polygon geofence over a time window (analytics) -------------
--   SELECT p.* FROM location_points p, geofences g
--   WHERE g.id = $1 AND ST_Contains(g.area::geometry, p.geom::geometry)
--     AND p.captured_at BETWEEN $from AND $to;
