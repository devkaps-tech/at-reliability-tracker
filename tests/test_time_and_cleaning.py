"""Tests for the time maths (past midnight, daylight saving) and the cleaning rules."""

import gzip
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import duckdb

from tracker.clean_realtime import parse_file
from tracker.gtfs_load import gtfs_secs
from tracker.score import hours_in_day, service_day_start

NZ = ZoneInfo("Pacific/Auckland")


def local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, NZ)


def test_gtfs_times_past_midnight_become_seconds():
    secs = duckdb.sql(f"SELECT {gtfs_secs(chr(39) + '25:10:00' + chr(39))}").fetchone()[0]
    assert secs == 25 * 3600 + 10 * 60


def test_trip_at_25_10_on_13_oct_runs_at_01_10_on_14_oct():
    when = local(service_day_start(date(2026, 10, 13)) + 25 * 3600 + 10 * 60)
    assert (when.date(), when.hour, when.minute) == (date(2026, 10, 14), 1, 10)


def test_normal_day_starts_at_midnight():
    assert local(service_day_start(date(2026, 10, 13))).hour == 0


def test_8am_means_8am_on_the_day_daylight_saving_starts():
    # 27 Sep 2026: clocks jump 02:00 -> 03:00
    assert local(service_day_start(date(2026, 9, 27)) + 8 * 3600).hour == 8
    assert hours_in_day(date(2026, 9, 27)) == 23


def test_8am_means_8am_on_the_day_daylight_saving_ends():
    # 4 Apr 2027: clocks go back 03:00 -> 02:00
    assert local(service_day_start(date(2027, 4, 4)) + 8 * 3600).hour == 8
    assert hours_in_day(date(2027, 4, 4)) == 25


def write_feed(tmp_path, polled_at, trip_updates):
    path = tmp_path / "feed.json.gz"
    entities = [{"id": str(i), "trip_update": tu} for i, tu in enumerate(trip_updates)]
    path.write_bytes(gzip.compress(json.dumps(
        {"status": "OK", "response": {"header": {"timestamp": polled_at}, "entity": entities}}).encode()))
    return path


def trip_update(start_date="20261013", rel=0, updated_at=1000, event_time=990, stop=True):
    tu = {"trip": {"trip_id": "T1", "route_id": "R1", "start_date": start_date, "schedule_relationship": rel},
          "timestamp": updated_at}
    if stop:
        tu["stop_time_update"] = {"stop_sequence": 5, "stop_id": "S5", "arrival": {"time": event_time, "delay": 30}}
    return tu


def parse(tmp_path, polled_at, *tus):
    stats = {"unreadable_files": 0, "too_old": 0, "predictions": 0}
    items = list(parse_file(write_feed(tmp_path, polled_at, tus), "20261013", 3600, stats))
    return [r for k, r in items if k == "obs"], [r for k, r in items if k == "trip"], stats


def test_keeps_a_normal_stop_report(tmp_path):
    obs, trips, _ = parse(tmp_path, 1000, trip_update())
    assert len(obs) == 1 and obs[0]["event"] == "arrival" and obs[0]["event_time"] == 990


def test_skips_trips_from_another_service_day(tmp_path):
    obs, trips, _ = parse(tmp_path, 1000, trip_update(start_date="20261012"))
    assert obs == [] and trips == []


def test_drops_stop_reports_older_than_the_limit(tmp_path):
    obs, trips, stats = parse(tmp_path, 1000 + 7200, trip_update())
    assert obs == [] and stats["too_old"] == 1


def test_keeps_old_cancellations(tmp_path):
    obs, trips, _ = parse(tmp_path, 1000 + 4 * 3600, trip_update(rel=3, stop=False))
    assert trips[0]["schedule_relationship"] == 3


def test_drops_predictions_for_stops_not_reached_yet(tmp_path):
    obs, trips, stats = parse(tmp_path, 1000, trip_update(event_time=1000 + 600))
    assert obs == [] and stats["predictions"] == 1 and len(trips) == 1


def test_cancelled_trip_is_recorded_without_stops(tmp_path):
    for rel in (3, "CANCELED"):
        obs, trips, _ = parse(tmp_path, 1000, trip_update(rel=rel, stop=False))
        assert obs == [] and trips[0]["schedule_relationship"] == 3
