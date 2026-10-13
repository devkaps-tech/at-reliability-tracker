"""Scheduler: the "clock" of the pipeline (a tiny stand-in for cron / Airflow).

  every 5 minutes   -> health check (alert if no data for 10 minutes)
  every day 04:30   -> nightly job: refresh timetable, clean + score yesterday

Each job runs as its own process, so a failing job can't stop the scheduler.
The last nightly run date is remembered in logs/, so a restart doesn't run it twice.

Run with:  python -m tracker.scheduler     (run_local.sh starts it for you)
"""

import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from tracker.config import load_config, project_path

NZ = ZoneInfo("Pacific/Auckland")


def run_job(module: str, *args: str) -> None:
    log_file = project_path(load_config()["collector"]["log_dir"]) / f"{module.split('.')[-1]}.log"
    with open(log_file, "a") as log:
        log.write(f"\n=== {datetime.now(NZ):%Y-%m-%d %H:%M:%S} {module} {' '.join(args)}\n")
        log.flush()
        subprocess.run([sys.executable, "-m", module, *args], stdout=log, stderr=subprocess.STDOUT)


def main() -> None:
    cfg = load_config()
    check_every = cfg["schedule"]["health_check_minutes"] * 60
    nightly_at = cfg["schedule"]["nightly_run_time"]
    last_nightly_file = project_path(cfg["collector"]["log_dir"]) / "last_nightly_run.txt"
    last_check = 0.0
    print(f"Scheduler started: health check every {check_every // 60} min, nightly job at {nightly_at}")
    while True:
        now = datetime.now(NZ)
        if time.monotonic() - last_check >= check_every:
            run_job("tracker.health_check")
            last_check = time.monotonic()
        today = f"{now:%Y-%m-%d}"
        last_nightly = last_nightly_file.read_text().strip() if last_nightly_file.exists() else ""
        if f"{now:%H:%M}" >= nightly_at and last_nightly != today:
            print(f"{now:%H:%M} running nightly job (log: logs/pipeline.log)")
            run_job("tracker.pipeline", "nightly")
            last_nightly_file.write_text(today)
        time.sleep(30)


if __name__ == "__main__":
    main()
