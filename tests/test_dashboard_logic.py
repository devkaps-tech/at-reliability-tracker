"""Tests for the live-vehicle reader, route names and the plain-English stop summary."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

from common import ends_label, station, stop_summary, tidy  # noqa: E402
from tracker.live import vehicles_now  # noqa: E402


def feed(entities, polled_at=10_000):
    return {"response": {"header": {"timestamp": polled_at}, "entity": entities}}


def vehicle(trip_id="T1", occupancy=None):
    v = {"trip": {"trip_id": trip_id, "route_id": "R1", "direction_id": 0},
         "position": {"latitude": -36.8, "longitude": 174.7}, "timestamp": 9_990, "vehicle": {"id": "V1"}}
    if occupancy is not None:
        v["occupancy_status"] = occupancy
    return {"vehicle": v}


def trip_update(trip_id="T1", updated_at=9_950, delay=120):
    return {"trip_update": {"trip": {"trip_id": trip_id}, "timestamp": updated_at,
                            "stop_time_update": {"stop_id": "S5", "arrival": {"delay": delay}}}}


def test_vehicle_gets_delay_and_last_stop_from_a_recent_update():
    _, vs = vehicles_now(feed([vehicle(), trip_update()]))
    assert vs[0]["delay_s"] == 120 and vs[0]["last_stop_id"] == "S5"


def test_stale_update_gives_no_delay():
    _, vs = vehicles_now(feed([vehicle(), trip_update(updated_at=10_000 - 3600)]))
    assert vs[0]["delay_s"] is None


def test_vehicles_without_a_trip_are_skipped():
    _, vs = vehicles_now(feed([{"vehicle": {"position": {"latitude": 0, "longitude": 0}}}]))
    assert vs == []


def test_occupancy_as_number_or_name():
    _, vs = vehicles_now(feed([vehicle(occupancy=2), vehicle("T2", occupancy="STANDING_ROOM_ONLY")]))
    assert [v["occupancy"] for v in vs] == ["Few seats left", "Standing room only"]


def test_route_names_read_naturally():
    assert tidy("Botany To Britomart Via Panmure") == "Botany to Britomart via Panmure"
    assert ends_label("Botany to Britomart via Panmure") == "Botany ↔ Britomart"
    assert ends_label("Inner Link Clockwise") == "Inner Link Clockwise"
    assert station("Newmarket Train Station 2") == "Newmarket"


def test_summary_for_a_punctual_bus():
    s = stop_summary([0, 10, -20, 30, 20] * 4)
    assert s["typical"] == "usually on time" and "rarely comes early" in s["catch"] and s["on_time"] == 1


def test_summary_for_a_late_bus():
    s = stop_summary([180, 240, 200, 900, 1200, 210, 190, 260, 230, 1000])
    assert s["typical"] == "usually 4 min late"
    assert "1 in 10 was more than" in s["worst"]
    assert s["late"] == 0.3


def test_summary_for_a_bus_that_comes_early():
    s = stop_summary([-200, -150, -30, 0, 20, 40, 60, -180, 10, 0])
    assert "1 in 10 reached this stop **4+ min early**" in s["catch"]


def test_no_buses_gives_no_summary():
    assert stop_summary([]) is None
