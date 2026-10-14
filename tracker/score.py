"""Score step: match one day's clean observations to the timetable, work out delays, check quality, save scores.

Delay = when the vehicle actually arrived  -  when the timetable said it would.
Negative = early. Then each stop is labelled early / on time / late using the thresholds in config.toml.

Outputs (all tagged with a run_id; re-running a day adds a new run rather than overwriting):
  data/clean/stop_arrivals/service_date=.../data.parquet   every scored stop, for analysts
  daily_route_metrics    on-time counts and delays per route per hour
  daily_route_trips      planned vs seen vs cancelled vs missing trips per route
  quality_checks         the day's checks, pass/fail
  pipeline_runs          one row per run; published = all "block" checks passed
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tracker.config import load_config, project_path

NZ = ZoneInfo("Pacific/Auckland")


def service_day_start(service_date: date) -> int:
    """The moment GTFS times are counted from, as a Unix timestamp.

    GTFS defines it as "noon minus 12 hours" on the service day. That's midnight on normal days, but on the
    two daylight-saving days it's 23:00 or 01:00 — which keeps times like 08:00:00 meaning 8 am on the clock.
    """
    noon = datetime(service_date.year, service_date.month, service_date.day, 12, tzinfo=NZ)
    return int(noon.timestamp()) - 12 * 3600


def hours_in_day(service_date: date) -> float:
    """24, or 23 / 25 on the days the clocks change."""
    start = datetime(service_date.year, service_date.month, service_date.day, tzinfo=NZ)
    end = start + timedelta(days=1)
    return (end.timestamp() - start.timestamp()) / 3600


def pick_gtfs_version(con, service_date: date) -> str:
    """The timetable that was live on that day: the newest version we had by the end of the day.
    (When back-filling a day from before our first download, fall back to the oldest version covering it.)"""
    rows = con.execute("""SELECT gtfs_version, loaded_at FROM gtfs_versions
                          WHERE $d BETWEEN feed_start_date AND feed_end_date ORDER BY loaded_at""",
                       {"d": service_date}).fetchall()
    if not rows:
        raise RuntimeError(f"No timetable covers {service_date}. Run: python -m tracker.gtfs_load --download")
    day_end = datetime(service_date.year, service_date.month, service_date.day, tzinfo=NZ) + timedelta(days=1)
    live = [version for version, loaded_at in rows if loaded_at <= day_end]
    return live[-1] if live else rows[0][0]


def time_band_sql(bands: dict) -> str:
    whens = " ".join(f"WHEN hour IN ({', '.join(map(str, hours))}) THEN '{name}'" for name, hours in bands.items())
    return f"CASE {whens} ELSE 'night' END"


def score_day(con, service_date: date, clean_stats: dict) -> dict:
    cfg = load_config()
    clean_dir = project_path(cfg["storage"]["clean_dir"])
    punct, quality = cfg["punctuality"], cfg["quality"]
    version = pick_gtfs_version(con, service_date)
    run_id = f"{service_date}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    obs_path = clean_dir / "stop_observations" / f"service_date={service_date}" / "data.parquet"
    trips_path = clean_dir / "trip_status" / f"service_date={service_date}" / "data.parquet"

    # 1. Match each observed stop to its timetable stop and work out the delay.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE scored AS
        WITH joined AS (
            SELECT o.*, t.route_id AS tt_route_id, t.direction_id,
                   CASE WHEN o.event = 'arrival' THEN st.arrival_secs ELSE st.departure_secs END AS sched_secs
            FROM read_parquet('{obs_path}') o
            JOIN gtfs_trips t ON t.gtfs_version = $v AND t.trip_id = o.trip_id
            JOIN gtfs_stop_times st ON st.gtfs_version = $v AND st.trip_id = o.trip_id
                                   AND st.stop_sequence = o.stop_sequence
        ), with_delay AS (
            SELECT j.service_date, $v AS gtfs_version, j.trip_id, j.tt_route_id AS route_id, j.direction_id,
                   r.route_short_name, r.mode,
                   coalesce(a.agency_name, r.agency_id) AS operator, j.stop_sequence, j.stop_id, j.event,
                   to_timestamp($base + j.sched_secs) AS scheduled_time, j.event_time AS actual_time,
                   epoch(j.event_time)::BIGINT - ($base + j.sched_secs) AS delay_s, j.feed_delay_s,
                   (j.sched_secs // 3600) % 24 AS hour
            FROM joined j
            JOIN gtfs_routes r ON r.gtfs_version = $v AND r.route_id = j.tt_route_id
            LEFT JOIN gtfs_agency a ON a.gtfs_version = $v AND a.agency_id = r.agency_id
        )
        SELECT *, {time_band_sql(cfg['time_bands'])} AS time_band,
               CASE WHEN abs(delay_s) > {quality['max_abs_delay_minutes'] * 60} THEN 'bad_data'
                    WHEN delay_s < -{punct['early_if_more_than_seconds_early']} THEN 'early'
                    WHEN delay_s > {punct['late_if_more_than_seconds_late']} THEN 'late'
                    ELSE 'on_time' END AS status
        FROM with_delay""", {"v": version, "base": service_day_start(service_date)})
    arrivals_path = clean_dir / "stop_arrivals" / f"service_date={service_date}" / "data.parquet"
    arrivals_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY (SELECT * FROM scored ORDER BY trip_id, stop_sequence) TO '{arrivals_path}' (FORMAT parquet)")

    # 2. Which trips were MEANT to run that day (from the calendar + holiday exceptions).
    con.execute("""CREATE OR REPLACE TEMP TABLE planned AS
        WITH running AS (
            SELECT service_id FROM gtfs_calendar
            WHERE gtfs_version = $v AND $d BETWEEN start_date AND end_date
              AND [sunday, monday, tuesday, wednesday, thursday, friday, saturday][dayofweek($d) + 1] = 1
            UNION SELECT service_id FROM gtfs_calendar_dates WHERE gtfs_version = $v AND date = $d AND exception_type = 1
        )
        SELECT trip_id, route_id FROM gtfs_trips
        WHERE gtfs_version = $v AND service_id IN (SELECT service_id FROM running)
          AND service_id NOT IN (SELECT service_id FROM gtfs_calendar_dates
                                 WHERE gtfs_version = $v AND date = $d AND exception_type = 2)""",
                {"v": version, "d": service_date})

    # 3. Quality checks.
    polls_expected = round(hours_in_day(service_date) * 3600 / cfg["collector"]["poll_seconds"])
    rates = con.execute(f"""SELECT
        (SELECT avg((trip_id IN (SELECT trip_id FROM gtfs_trips WHERE gtfs_version = $v))::INT)
         FROM read_parquet('{trips_path}')),
        (SELECT avg((status = 'bad_data')::INT) FROM scored),
        (SELECT count(DISTINCT route_id) FROM scored)
            / nullif((SELECT count(DISTINCT route_id) FROM planned), 0),
        (SELECT avg((abs(delay_s - feed_delay_s) <= 60)::INT) FROM scored WHERE feed_delay_s IS NOT NULL)
        """, {"v": version}).fetchone()
    checks = [  # (name, value, threshold, higher_is_better, severity)
        ("trip_match_rate", rates[0], quality["min_trip_match_rate"], True, "block"),
        ("bad_delay_rate", rates[1], quality["max_bad_delay_rate"], False, "block"),
        ("poll_coverage", clean_stats["polls_received"] / polls_expected, quality["min_poll_coverage"], True, "warn"),
        ("route_coverage", rates[2], quality["min_route_coverage"], True, "warn"),
        ("delay_agreement_with_AT", rates[3], quality["min_delay_agreement"], True, "warn"),
    ]
    results = []
    for name, value, threshold, higher_is_better, severity in checks:
        passed = value is not None and (value >= threshold if higher_is_better else value <= threshold)
        results.append((run_id, service_date, name, value, threshold, severity, passed))
    published = all(passed for *_, severity, passed in results if severity == "block")

    # 4. Save everything for this run in one go (all or nothing).
    con.execute("BEGIN")
    con.executemany("INSERT INTO quality_checks VALUES (?, ?, ?, ?, ?, ?, ?)", results)
    con.execute("""INSERT INTO daily_route_metrics
        SELECT $run, service_date, route_id, route_short_name, mode, operator, hour, time_band,
               count(*), count(*) FILTER (status = 'early'), count(*) FILTER (status = 'on_time'),
               count(*) FILTER (status = 'late'), sum(delay_s), max(delay_s)
        FROM scored WHERE status != 'bad_data' GROUP BY ALL""", {"run": run_id})
    con.execute(f"""INSERT INTO daily_route_trips
        SELECT $run, $d, p.route_id, r.route_short_name, r.mode, coalesce(a.agency_name, r.agency_id),
               count(*), count(*) FILTER (s.trip_id IS NOT NULL AND NOT s.cancelled),
               count(*) FILTER (s.cancelled), count(*) FILTER (s.trip_id IS NULL)
        FROM planned p
        LEFT JOIN read_parquet('{trips_path}') s USING (trip_id)
        JOIN gtfs_routes r ON r.gtfs_version = $v AND r.route_id = p.route_id
        LEFT JOIN gtfs_agency a ON a.gtfs_version = $v AND a.agency_id = r.agency_id
        GROUP BY ALL""", {"run": run_id, "d": service_date, "v": version})
    con.execute("INSERT INTO pipeline_runs VALUES ($run, $d, $v, $now, $got, $exp, $obs, $pub)",
                {"run": run_id, "d": service_date, "v": version, "now": datetime.now(timezone.utc),
                 "got": clean_stats["polls_received"], "exp": polls_expected,
                 "obs": clean_stats["stop_observations"], "pub": published})
    con.execute("COMMIT")

    summary = con.execute("""SELECT count(*), avg((status = 'on_time')::INT), median(delay_s)
                             FROM scored WHERE status != 'bad_data'""").fetchone()
    return {"run_id": run_id, "gtfs_version": version, "published": published, "checks": results,
            "stops_scored": summary[0], "on_time_rate": summary[1], "median_delay_s": summary[2]}
