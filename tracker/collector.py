"""Collector: call AT's combined realtime feed every 30 seconds and save each response untouched.

Each response is saved as
    raw/realtime/date=YYYY-MM-DD/HHMMSS+1300.json.gz
where the date and time are Auckland local time and +1300 / +1200 is the offset from UTC.
The offset keeps file names unique in the hour that happens twice when daylight saving ends.

Run with:  python -m tracker.collector     (stop with Ctrl+C)
"""

import gzip
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from tracker.config import get_api_key, load_config, project_path

NZ = ZoneInfo("Pacific/Auckland")
log = logging.getLogger("collector")


def setup_logging(log_dir: Path) -> None:
    """Log to the screen and to logs/collector.log (a new file each night, kept for 30 days)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    to_file = TimedRotatingFileHandler(log_dir / "collector.log", when="midnight", backupCount=30)
    to_screen = logging.StreamHandler()
    for handler in (to_file, to_screen):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def raw_path(raw_dir: Path, fetched_at_utc: datetime) -> Path:
    local = fetched_at_utc.astimezone(NZ)
    return raw_dir / f"date={local:%Y-%m-%d}" / f"{local:%H%M%S%z}.json.gz"


def write_atomically(path: Path, data: bytes, compress: bool = False) -> None:
    """Write to a temporary file, then rename it into place.
    If the program is killed mid-write, we never leave a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if compress:
        with gzip.open(tmp, "wb") as f:
            f.write(data)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def fetch_with_retries(session, url: str, timeout: float, retry_waits: list[float], deadline: float):
    """Call the feed, retrying on temporary problems. Returns (body, parsed JSON) or None if every try failed.

    Temporary problems (worth retrying): timeouts, network errors, AT server errors (5xx), garbled JSON.
    Not worth retrying: a wrong/expired key (401/403) or being over quota (429) — retrying only wastes calls.
    `deadline` stops us retrying into the next poll's 30-second slot.
    """
    attempts = len(retry_waits) + 1
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code in (401, 403):
                log.error("AT rejected the API key (HTTP %s) — check AT_API_KEY in .env", resp.status_code)
                return None
            if resp.status_code == 429:
                log.warning("Over the API quota (HTTP 429) — skipping this poll")
                return None
            resp.raise_for_status()
            return resp.content, resp.json()
        except (requests.RequestException, ValueError) as err:
            # ValueError covers a response that isn't valid JSON
            log.warning("Attempt %d/%d failed: %s", attempt, attempts, err)
            if attempt == attempts:
                break
            wait = retry_waits[attempt - 1]
            if time.monotonic() + wait + timeout > deadline:
                log.warning("No time left before the next poll — skipping retries")
                break
            time.sleep(wait)
    log.error("Poll failed after %d attempt(s)", attempt)
    return None


def describe(feed: dict) -> str:
    """A one-line summary of a response for the log, e.g. '2268 entities (983 trips, 1104 vehicles, 181 alerts)'."""
    entities = (feed.get("response") or {}).get("entity") or []
    trips = sum("trip_update" in e for e in entities)
    vehicles = sum("vehicle" in e for e in entities)
    alerts = sum("alert" in e for e in entities)
    return f"{len(entities)} entities ({trips} trips, {vehicles} vehicles, {alerts} alerts)"


def poll_once(session, cfg: dict, raw_dir: Path, heartbeat: Path, deadline: float) -> None:
    started = time.monotonic()
    fetched_at = datetime.now(timezone.utc)
    result = fetch_with_retries(session, cfg["feed_url"], cfg["timeout_seconds"], cfg["retry_wait_seconds"], deadline)
    if result is None:
        return
    body, feed = result
    if feed.get("status") != "OK":
        # Still saved (raw means untouched); the parser decides later what to do with it.
        log.warning("Feed status is %r, error: %r", feed.get("status"), feed.get("error"))

    path = raw_path(raw_dir, fetched_at)
    write_atomically(path, body, compress=True)
    log.info("Saved %s/%s  %s  %d KB  %d ms", path.parent.name, path.name, describe(feed),
             path.stat().st_size // 1024, (time.monotonic() - started) * 1000)

    # The heartbeat file is how the health check knows data is still arriving.
    write_atomically(heartbeat, json.dumps({"last_success_utc": fetched_at.isoformat(), "file": str(path)}).encode())


def run() -> None:
    cfg = load_config()["collector"]
    raw_dir = project_path(cfg["raw_dir"])
    log_dir = project_path(cfg["log_dir"])
    setup_logging(log_dir)
    heartbeat = log_dir / "heartbeat.json"

    session = requests.Session()
    session.headers.update({"Ocp-Apim-Subscription-Key": get_api_key(), "Accept": "application/json"})

    stop = False

    def request_stop(signum, frame):
        nonlocal stop
        stop = True
        log.info("Stop requested — finishing up")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    interval = cfg["poll_seconds"]
    log.info("Collector started: polling %s every %d s", cfg["feed_url"], interval)
    next_poll = time.monotonic()
    while not stop:
        try:
            poll_once(session, cfg, raw_dir, heartbeat, deadline=next_poll + interval)
        except Exception:
            # Anything unexpected (disk full, a bug...) is logged and the loop carries on.
            log.exception("Unexpected error during poll")

        # Polls are pinned to a fixed 30 s rhythm, so slow calls don't make the schedule drift.
        next_poll += interval
        if next_poll < time.monotonic():  # fell behind, e.g. the computer was asleep
            next_poll = time.monotonic()
        while not stop and time.monotonic() < next_poll:
            time.sleep(min(1.0, next_poll - time.monotonic()))
    log.info("Collector stopped")


if __name__ == "__main__":
    run()
