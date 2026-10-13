"""Clean step: turn one service day's raw feed files into two tidy tables.

  stop_observations — one row per trip per stop: when the vehicle actually arrived/departed
  trip_status       — one row per trip seen in the feed: route, vehicle, cancelled or not

Things the raw feed does that we clean up (found by inspecting real responses):
  * Each trip update only reports the LATEST stop, so we build stop history across many polls.
  * Old updates are repeated in later polls -> keep one row per (trip, stop), the most recent report.
  * Some stop reports are hours old -> ignore those older than max_update_age_minutes when received
    (but keep cancellations, which AT repeats for hours with their original timestamp).
  * A few updates are predictions for the next stop, not things that happened -> drop those.
  * Trips after midnight belong to the previous "service day" (start_date), so a day's trips
    are read from that day's folder AND the next day's folder.

Output: data/clean/<table>/service_date=YYYY-MM-DD/data.parquet (re-running a day replaces these files).
"""

import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa

from tracker.config import load_config, project_path

# The feed sends schedule_relationship as a number or a word; we store the number.
RELATIONSHIPS = {"SCHEDULED": 0, "ADDED": 1, "UNSCHEDULED": 2, "CANCELED": 3, "REPLACEMENT": 5, "DUPLICATED": 6,
                 "DELETED": 7}
CANCELED = 3
PREDICTION_SLACK_S = 60   # an event time this far after the update's own timestamp is a prediction


def relationship(value) -> int | None:
    return RELATIONSHIPS.get(value, None) if isinstance(value, str) else value


def raw_files_for(service_date: date, raw_dir: Path) -> list[Path]:
    """This day's folder plus the next day's (for trips running past midnight)."""
    files = []
    for d in (service_date, service_date + timedelta(days=1)):
        files += sorted((raw_dir / f"date={d:%Y-%m-%d}").glob("*.json.gz"))
    return files


def parse_file(path: Path, service_day: str, max_age_s: float, stats: dict):
    """Yield ('obs', row) and ('trip', row) items for trips belonging to service_day (YYYYMMDD)."""
    try:
        feed = json.loads(gzip.open(path).read())
        header = feed["response"]["header"]
        entities = feed["response"]["entity"] or []
    except (OSError, ValueError, KeyError, TypeError):
        stats["unreadable_files"] += 1
        return
    polled_at = float(header["timestamp"])

    for entity in entities:
        tu = entity.get("trip_update")
        if not tu:
            continue
        trip = tu.get("trip") or {}
        if trip.get("start_date") != service_day:
            continue
        updated_at = tu.get("timestamp") or polled_at
        rel = relationship(trip.get("schedule_relationship"))
        vehicle = (tu.get("vehicle") or {}).get("id")
        # Trip status is kept whatever its age: a cancellation is announced once and then repeated for hours.
        yield "trip", {"trip_id": trip.get("trip_id"), "route_id": trip.get("route_id"),
                       "direction_id": trip.get("direction_id"), "start_time": trip.get("start_time"),
                       "schedule_relationship": rel, "vehicle_id": vehicle, "updated_at": updated_at}

        stu = tu.get("stop_time_update")
        if rel == CANCELED or not isinstance(stu, dict):
            continue
        if polled_at - updated_at > max_age_s:
            stats["too_old"] += 1
            continue
        arrival, departure = stu.get("arrival") or {}, stu.get("departure") or {}
        # Score on arrival time; the first stop of a trip often only has a departure time.
        event = "arrival" if arrival.get("time") else "departure"
        event_time = (arrival if event == "arrival" else departure).get("time")
        if not event_time or stu.get("stop_sequence") is None:
            continue
        if event_time > updated_at + PREDICTION_SLACK_S:
            stats["predictions"] += 1
            continue
        yield "obs", {"trip_id": trip.get("trip_id"), "route_id": trip.get("route_id"),
                      "stop_sequence": stu["stop_sequence"], "stop_id": stu.get("stop_id"),
                      "event": event, "event_time": event_time,
                      "feed_delay_s": (arrival if event == "arrival" else departure).get("delay"),
                      "vehicle_id": vehicle, "updated_at": updated_at, "polled_at": polled_at}


def clean_day(con, service_date: date) -> dict:
    """Build the clean tables for one service day. Returns counts for the run log and quality checks."""
    cfg = load_config()
    raw_dir = project_path(cfg["collector"]["raw_dir"])
    out_dir = project_path(cfg["storage"]["clean_dir"])
    max_age_s = cfg["cleaning"]["max_update_age_minutes"] * 60
    stats = {"unreadable_files": 0, "too_old": 0, "predictions": 0}

    obs, trips = [], []
    for path in raw_files_for(service_date, raw_dir):
        for kind, row in parse_file(path, f"{service_date:%Y%m%d}", max_age_s, stats):
            (obs if kind == "obs" else trips).append(row)

    obs_table = pa.Table.from_pylist(obs, schema=pa.schema([
        ("trip_id", pa.string()), ("route_id", pa.string()), ("stop_sequence", pa.int32()), ("stop_id", pa.string()),
        ("event", pa.string()), ("event_time", pa.int64()), ("feed_delay_s", pa.int32()),
        ("vehicle_id", pa.string()), ("updated_at", pa.float64()), ("polled_at", pa.float64())]))
    trip_table = pa.Table.from_pylist(trips, schema=pa.schema([
        ("trip_id", pa.string()), ("route_id", pa.string()), ("direction_id", pa.int32()),
        ("start_time", pa.string()), ("schedule_relationship", pa.int32()), ("vehicle_id", pa.string()),
        ("updated_at", pa.float64())]))
    con.register("obs_raw", obs_table)
    con.register("trips_raw", trip_table)

    obs_path = out_dir / "stop_observations" / f"service_date={service_date}" / "data.parquet"
    trips_path = out_dir / "trip_status" / f"service_date={service_date}" / "data.parquet"
    for p in (obs_path, trips_path):
        p.parent.mkdir(parents=True, exist_ok=True)

    # One row per (trip, stop): the most recent report wins. Times stored as real timestamps.
    con.execute(f"""COPY (
        SELECT DATE '{service_date}' AS service_date, trip_id, route_id, stop_sequence, stop_id, event,
               to_timestamp(event_time) AS event_time, feed_delay_s, vehicle_id,
               to_timestamp(updated_at) AS updated_at, to_timestamp(polled_at) AS received_at
        FROM obs_raw
        QUALIFY row_number() OVER (PARTITION BY trip_id, stop_sequence ORDER BY updated_at DESC, polled_at) = 1
        ORDER BY trip_id, stop_sequence
    ) TO '{obs_path}' (FORMAT parquet)""")
    # One row per trip: cancelled if it was ever reported cancelled.
    con.execute(f"""COPY (
        SELECT DATE '{service_date}' AS service_date, trip_id,
               arg_max(route_id, updated_at) AS route_id, arg_max(direction_id, updated_at) AS direction_id,
               arg_max(start_time, updated_at) AS start_time, arg_max(vehicle_id, updated_at) AS vehicle_id,
               bool_or(schedule_relationship = {CANCELED}) AS cancelled,
               arg_max(schedule_relationship, updated_at) AS last_schedule_relationship,
               to_timestamp(min(updated_at)) AS first_seen, to_timestamp(max(updated_at)) AS last_seen
        FROM trips_raw GROUP BY trip_id ORDER BY trip_id
    ) TO '{trips_path}' (FORMAT parquet)""")
    con.unregister("obs_raw")
    con.unregister("trips_raw")

    day_folder = raw_dir / f"date={service_date:%Y-%m-%d}"
    stats.update(
        polls_received=len(list(day_folder.glob("*.json.gz"))),
        raw_stop_reports=len(obs),
        stop_observations=con.execute(f"SELECT count(*) FROM '{obs_path}'").fetchone()[0],
        trips_seen=con.execute(f"SELECT count(*) FROM '{trips_path}'").fetchone()[0],
    )
    return stats
