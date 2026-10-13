"""Load AT's timetable (GTFS static) into the warehouse, tagged with a version.

AT republishes the timetable every few weeks. Each new one gets a version tag and old ones are kept,
so a past day is always scored against the timetable that was live on that day.

Run with:
    python -m tracker.gtfs_load --download      # fetch AT's latest zip; load it only if it's new
    python -m tracker.gtfs_load gtfs            # load a folder (or .zip) you already have
"""

import argparse
import csv
import hashlib
import io
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from tracker.config import load_config, project_path
from tracker.db import connect

MODES = {2: "train", 3: "bus", 4: "ferry"}


def read_feed_info(folder: Path) -> dict:
    with open(folder / "feed_info.txt", newline="", encoding="utf-8-sig") as f:
        return next(csv.DictReader(f))


def version_tag(info: dict) -> str:
    """Short, readable tag: the timetable's start date plus a fingerprint of AT's version string."""
    fingerprint = hashlib.sha1(info["feed_version"].encode()).hexdigest()[:6]
    return f"{info['feed_start_date']}-{fingerprint}"


def gtfs_secs(column: str) -> str:
    """SQL turning a GTFS time like '25:10:00' into seconds (90600). Times past 24:00 are next-day trips."""
    return (f"try_cast(split_part({column}, ':', 1) AS INT) * 3600 + "
            f"try_cast(split_part({column}, ':', 2) AS INT) * 60 + "
            f"try_cast(split_part({column}, ':', 3) AS INT)")


def load_folder(folder: Path, source: str) -> str | None:
    """Load one unzipped timetable. Returns its version tag, or None if that version is already loaded."""
    info = read_feed_info(folder)
    version = version_tag(info)
    con = connect()
    if con.execute("SELECT 1 FROM gtfs_versions WHERE gtfs_version = ?", [version]).fetchone():
        print(f"Timetable {version} is already loaded — nothing to do")
        return None

    def csv_of(name):
        return f"read_csv('{folder / name}', all_varchar = true, header = true)"

    con.execute("BEGIN")
    con.execute(f"""INSERT INTO gtfs_agency SELECT $v, agency_id, agency_name FROM {csv_of('agency.txt')}""", {"v": version})
    con.execute(f"""INSERT INTO gtfs_routes
        SELECT $v, route_id, agency_id, route_short_name, route_long_name, route_type::INT,
               CASE route_type::INT {' '.join(f"WHEN {k} THEN '{m}'" for k, m in MODES.items())} ELSE 'other' END
        FROM {csv_of('routes.txt')}""", {"v": version})
    con.execute(f"""INSERT INTO gtfs_trips
        SELECT $v, trip_id, route_id, service_id, direction_id::INT, trip_headsign FROM {csv_of('trips.txt')}""",
                {"v": version})
    con.execute(f"""INSERT INTO gtfs_stop_times
        SELECT $v, trip_id, stop_sequence::INT, stop_id, {gtfs_secs('arrival_time')}, {gtfs_secs('departure_time')}
        FROM {csv_of('stop_times.txt')}""", {"v": version})
    con.execute(f"""INSERT INTO gtfs_stops
        SELECT $v, stop_id, stop_code, stop_name, stop_lat::DOUBLE, stop_lon::DOUBLE FROM {csv_of('stops.txt')}""",
                {"v": version})
    con.execute(f"""INSERT INTO gtfs_calendar
        SELECT $v, service_id, monday::INT, tuesday::INT, wednesday::INT, thursday::INT, friday::INT,
               saturday::INT, sunday::INT, strptime(start_date, '%Y%m%d')::DATE, strptime(end_date, '%Y%m%d')::DATE
        FROM {csv_of('calendar.txt')}""", {"v": version})
    con.execute(f"""INSERT INTO gtfs_calendar_dates
        SELECT $v, service_id, strptime(date, '%Y%m%d')::DATE, exception_type::INT
        FROM {csv_of('calendar_dates.txt')}""", {"v": version})
    con.execute("""INSERT INTO gtfs_versions VALUES ($v, $fv, strptime($s, '%Y%m%d')::DATE,
                   strptime($e, '%Y%m%d')::DATE, $now, $src)""",
                {"v": version, "fv": info["feed_version"], "s": info["feed_start_date"], "e": info["feed_end_date"],
                 "now": datetime.now(timezone.utc), "src": source})
    con.execute("COMMIT")

    counts = con.execute("""SELECT (SELECT count(*) FROM gtfs_routes WHERE gtfs_version = $v),
                                   (SELECT count(*) FROM gtfs_trips WHERE gtfs_version = $v),
                                   (SELECT count(*) FROM gtfs_stop_times WHERE gtfs_version = $v)""",
                         {"v": version}).fetchone()
    print(f"Loaded timetable {version}: {counts[0]} routes, {counts[1]:,} trips, {counts[2]:,} stop times")
    return version


def load_zip(zip_path: Path, source: str) -> str | None:
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        return load_folder(Path(tmp), source)


def download_and_load() -> str | None:
    """Fetch AT's current timetable zip, keep a copy in data/gtfs/, and load it if it's a new version."""
    cfg = load_config()["gtfs"]
    archive = project_path(cfg["archive_dir"])
    archive.mkdir(parents=True, exist_ok=True)
    resp = requests.get(cfg["url"], timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        info = next(csv.DictReader(io.TextIOWrapper(z.open("feed_info.txt"), encoding="utf-8-sig")))
    version = version_tag(info)
    zip_path = archive / f"{version}.zip"
    if not zip_path.exists():
        zip_path.write_bytes(resp.content)
    return load_zip(zip_path, source=cfg["url"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", help="a GTFS folder or .zip to load")
    parser.add_argument("--download", action="store_true", help="download AT's latest timetable")
    args = parser.parse_args()
    if args.download:
        download_and_load()
    elif args.path:
        path = Path(args.path).resolve()
        if path.is_dir():
            # Keep a zipped copy in the archive too, so every version can be re-loaded later.
            archive = project_path(load_config()["gtfs"]["archive_dir"])
            archive.mkdir(parents=True, exist_ok=True)
            target = archive / f"{version_tag(read_feed_info(path))}.zip"
            if not target.exists():
                shutil.make_archive(str(target.with_suffix("")), "zip", path)
            load_folder(path, source=str(path))
        else:
            load_zip(path, source=str(path))
    else:
        parser.error("give a folder/zip to load, or --download")


if __name__ == "__main__":
    main()
