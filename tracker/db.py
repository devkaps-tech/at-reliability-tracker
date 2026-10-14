"""The warehouse: one DuckDB file holding the timetable versions, the run log, quality checks and daily scores.

Every table is defined here, so this file doubles as the list of what's stored.
"""

import duckdb

from tracker.config import load_config, project_path

SCHEMA = """
-- Timetable (GTFS static). Every version AT publishes is kept and every row is tagged with it.
CREATE TABLE IF NOT EXISTS gtfs_versions (
    gtfs_version VARCHAR PRIMARY KEY,   -- e.g. 20261005-3f9a1c
    feed_version VARCHAR,               -- AT's own (long) version string
    feed_start_date DATE, feed_end_date DATE,
    loaded_at TIMESTAMPTZ,              -- when we first saw it = when it became "live" for scoring
    source VARCHAR
);
CREATE TABLE IF NOT EXISTS gtfs_agency (gtfs_version VARCHAR, agency_id VARCHAR, agency_name VARCHAR);
CREATE TABLE IF NOT EXISTS gtfs_routes (
    gtfs_version VARCHAR, route_id VARCHAR, agency_id VARCHAR, route_short_name VARCHAR,
    route_long_name VARCHAR, route_type INTEGER, mode VARCHAR);
CREATE TABLE IF NOT EXISTS gtfs_trips (
    gtfs_version VARCHAR, trip_id VARCHAR, route_id VARCHAR, service_id VARCHAR,
    direction_id INTEGER, trip_headsign VARCHAR, shape_id VARCHAR);
ALTER TABLE gtfs_trips ADD COLUMN IF NOT EXISTS shape_id VARCHAR;  -- added after the first version was loaded
CREATE TABLE IF NOT EXISTS gtfs_stop_times (
    gtfs_version VARCHAR, trip_id VARCHAR, stop_sequence INTEGER, stop_id VARCHAR,
    arrival_secs INTEGER, departure_secs INTEGER);  -- seconds after "service-day midnight"; can pass 24 h
CREATE TABLE IF NOT EXISTS gtfs_stops (
    gtfs_version VARCHAR, stop_id VARCHAR, stop_code VARCHAR, stop_name VARCHAR, stop_lat DOUBLE, stop_lon DOUBLE);
CREATE TABLE IF NOT EXISTS gtfs_shapes (   -- the line each route draws on a map
    gtfs_version VARCHAR, shape_id VARCHAR, seq INTEGER, lat DOUBLE, lon DOUBLE);
CREATE TABLE IF NOT EXISTS gtfs_calendar (
    gtfs_version VARCHAR, service_id VARCHAR, monday INTEGER, tuesday INTEGER, wednesday INTEGER,
    thursday INTEGER, friday INTEGER, saturday INTEGER, sunday INTEGER, start_date DATE, end_date DATE);
CREATE TABLE IF NOT EXISTS gtfs_calendar_dates (
    gtfs_version VARCHAR, service_id VARCHAR, date DATE, exception_type INTEGER);  -- 1 = added, 2 = removed

-- The main pattern of each route direction: its usual destination sign and map line.
CREATE OR REPLACE VIEW route_patterns AS
    SELECT gtfs_version, route_id, direction_id, mode(trip_headsign) AS headsign, mode(shape_id) AS shape_id,
           count(*) AS trips
    FROM gtfs_trips GROUP BY ALL;

-- One row each time a day is scored. Re-running a day adds a new run; nothing is overwritten.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id VARCHAR PRIMARY KEY, service_date DATE, gtfs_version VARCHAR, computed_at TIMESTAMPTZ,
    polls_received INTEGER, polls_expected INTEGER, stop_observations INTEGER,
    published BOOLEAN                    -- false if a "block" quality check failed
);
CREATE TABLE IF NOT EXISTS quality_checks (
    run_id VARCHAR, service_date DATE, check_name VARCHAR, value DOUBLE, threshold DOUBLE,
    severity VARCHAR, passed BOOLEAN);

-- Daily scores (append-only). One row per route per scheduled hour per day.
CREATE TABLE IF NOT EXISTS daily_route_metrics (
    run_id VARCHAR, service_date DATE, route_id VARCHAR, route_short_name VARCHAR, mode VARCHAR,
    operator VARCHAR, hour INTEGER, time_band VARCHAR,
    stops_scored INTEGER, early INTEGER, on_time INTEGER, late INTEGER,
    total_delay_s BIGINT, max_delay_s INTEGER);   -- average delay = total_delay_s / stops_scored
-- Trips per route per day: planned vs seen vs cancelled vs never seen.
CREATE TABLE IF NOT EXISTS daily_route_trips (
    run_id VARCHAR, service_date DATE, route_id VARCHAR, route_short_name VARCHAR, mode VARCHAR, operator VARCHAR,
    planned_trips INTEGER, seen_trips INTEGER, cancelled_trips INTEGER, missing_trips INTEGER);

-- What the dashboard reads: for each day, only the newest run that passed its quality checks.
CREATE OR REPLACE VIEW latest_runs AS
    SELECT * FROM pipeline_runs WHERE published
    QUALIFY row_number() OVER (PARTITION BY service_date ORDER BY computed_at DESC) = 1;
CREATE OR REPLACE VIEW route_metrics AS
    SELECT m.* FROM daily_route_metrics m JOIN latest_runs USING (run_id);
CREATE OR REPLACE VIEW route_trips AS
    SELECT t.* FROM daily_route_trips t JOIN latest_runs USING (run_id);
"""


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = project_path(load_config()["storage"]["warehouse"])
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    con.execute("SET TimeZone = 'Pacific/Auckland'")
    if not read_only:
        con.execute(SCHEMA)
    return con
