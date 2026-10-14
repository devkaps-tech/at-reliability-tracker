"""Live view: read the newest saved poll and list the vehicles on the road right now, with how late each is running.

This reads the raw files the collector writes, so it only works on the machine that runs the collector.
"""

import gzip
import json
from pathlib import Path

# GTFS-realtime occupancy, in plain words. The feed may send the number or the name.
OCCUPANCY = {
    0: "Empty", 1: "Plenty of seats", 2: "Few seats left", 3: "Standing room only", 4: "Very crowded", 5: "Full",
    6: "Not taking passengers",
}
OCCUPANCY_NAMES = {
    "EMPTY": 0, "MANY_SEATS_AVAILABLE": 1, "FEW_SEATS_AVAILABLE": 2, "STANDING_ROOM_ONLY": 3,
    "CRUSHED_STANDING_ROOM_ONLY": 4, "FULL": 5, "NOT_ACCEPTING_PASSENGERS": 6,
}


def latest_raw_file(raw_dir: Path) -> Path | None:
    for folder in sorted(raw_dir.glob("date=*"), reverse=True):
        files = sorted(folder.glob("*.json.gz"), key=lambda p: p.stat().st_mtime)
        if files:
            return files[-1]
    return None


def occupancy_text(value) -> str:
    if isinstance(value, str):
        value = OCCUPANCY_NAMES.get(value)
    return OCCUPANCY.get(value, "")


def vehicles_now(feed: dict, max_update_age_s: float = 900) -> tuple[float, list[dict]]:
    """Returns (poll time, vehicles). Each vehicle on a trip gets its position and, when AT has sent a recent
    trip update for that trip, its delay and the last stop it reported."""
    header = feed["response"]["header"]
    polled_at = float(header["timestamp"])
    entities = feed["response"]["entity"] or []

    updates = {}
    for e in entities:
        tu = e.get("trip_update")
        if not tu or polled_at - (tu.get("timestamp") or 0) > max_update_age_s:
            continue
        stu = tu.get("stop_time_update") if isinstance(tu.get("stop_time_update"), dict) else {}
        event = stu.get("arrival") or stu.get("departure") or {}
        delay = event.get("delay", tu.get("delay"))
        updates[(tu.get("trip") or {}).get("trip_id")] = {"delay_s": delay, "last_stop_id": stu.get("stop_id")}

    vehicles = []
    for e in entities:
        v = e.get("vehicle")
        if not v or not v.get("trip") or not v.get("position"):
            continue
        trip_id = v["trip"].get("trip_id")
        update = updates.get(trip_id, {})
        vehicles.append({
            "trip_id": trip_id, "route_id": v["trip"].get("route_id"), "direction_id": v["trip"].get("direction_id"),
            "lat": v["position"]["latitude"], "lon": v["position"]["longitude"],
            "vehicle": (v.get("vehicle") or {}).get("label") or (v.get("vehicle") or {}).get("id"),
            "seen_at": v.get("timestamp") or polled_at, "occupancy": occupancy_text(v.get("occupancy_status")),
            "delay_s": update.get("delay_s"), "last_stop_id": update.get("last_stop_id"),
        })
    return polled_at, vehicles


def read_feed(path: Path) -> dict:
    return json.loads(gzip.open(path).read())
