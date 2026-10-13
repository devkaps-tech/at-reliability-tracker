"""Orchestrator: run the steps in the right order for a day.

    refresh timetable  ->  clean raw files  ->  score + quality checks  ->  publish (if checks pass)

Run with:
    python -m tracker.pipeline nightly                     # refresh timetable, then score yesterday
    python -m tracker.pipeline run --date 2026-10-13       # score one day (default: yesterday)
    python -m tracker.pipeline backfill --start 2026-10-10 --end 2026-10-13   # re-run a range of days
"""

import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tracker.clean_realtime import clean_day
from tracker.db import connect
from tracker.gtfs_load import download_and_load
from tracker.score import score_day

NZ = ZoneInfo("Pacific/Auckland")


def yesterday() -> date:
    return datetime.now(NZ).date() - timedelta(days=1)


def run_day(service_date: date) -> dict:
    con = connect()
    try:
        stats = clean_day(con, service_date)
        print(f"{service_date}  cleaned: {stats['polls_received']} polls, {stats['raw_stop_reports']:,} stop reports "
              f"-> {stats['stop_observations']:,} unique stop observations, {stats['trips_seen']:,} trips")
        result = score_day(con, service_date, stats)
    finally:
        con.close()
    on_time = f"{result['on_time_rate']:.1%}" if result["on_time_rate"] is not None else "n/a"
    print(f"{service_date}  scored {result['stops_scored']:,} stops against timetable {result['gtfs_version']}: "
          f"{on_time} on time, median delay {result['median_delay_s']} s")
    for _, _, name, value, threshold, severity, passed in result["checks"]:
        shown = "n/a" if value is None else f"{value:.3f}"
        print(f"    {'PASS' if passed else 'FAIL'}  {name:<24} {shown:>6}  (limit {threshold}, {severity})")
    print(f"{service_date}  {'PUBLISHED' if result['published'] else 'NOT PUBLISHED - a blocking check failed'}"
          f"  (run {result['run_id']})")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("nightly", help="refresh the timetable, then score yesterday")
    run = sub.add_parser("run", help="score one day")
    run.add_argument("--date", type=date.fromisoformat, default=None, help="YYYY-MM-DD (default: yesterday)")
    backfill = sub.add_parser("backfill", help="score every day in a range")
    backfill.add_argument("--start", type=date.fromisoformat, required=True)
    backfill.add_argument("--end", type=date.fromisoformat, required=True)
    args = parser.parse_args()

    if args.command == "nightly":
        try:
            download_and_load()
        except Exception as err:  # keep going with the timetable we already have
            print(f"Timetable refresh failed ({err}); scoring with the latest loaded version")
        run_day(yesterday())
    elif args.command == "run":
        run_day(args.date or yesterday())
    else:
        day = args.start
        while day <= args.end:
            run_day(day)
            day += timedelta(days=1)


if __name__ == "__main__":
    main()
