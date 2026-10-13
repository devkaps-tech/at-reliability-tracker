"""Health check: raise an alarm if the collector hasn't saved anything for 10 minutes.

Runs separately from the collector (every 5 minutes), so it still works if the collector freezes.
Sends ONE alert when data stops and ONE "recovered" message when it comes back — no spam.
Alerts go to a Mac notification and, if ALERT_WEBHOOK_URL is set in .env, to Slack or ntfy.sh.

Run with:  python -m tracker.health_check
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests

from tracker.config import load_config, project_path


def minutes_since_last_save(heartbeat_file) -> float | None:
    """Minutes since the collector last saved a response, or None if it never has."""
    try:
        last = json.loads(heartbeat_file.read_text())["last_success_utc"]
    except (FileNotFoundError, KeyError, ValueError):
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60


def send_alert(message: str) -> None:
    print(message)
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{message}" with title "AT tracker"'], check=False)
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if url:
        try:
            if "hooks.slack.com" in url:
                requests.post(url, json={"text": message}, timeout=10)
            else:  # e.g. https://ntfy.sh/<your-topic> — pushes to your phone
                requests.post(url, data=message.encode(), timeout=10)
        except requests.RequestException as err:
            print(f"Could not send alert to webhook: {err}", file=sys.stderr)


def check() -> None:
    cfg = load_config()
    log_dir = project_path(cfg["collector"]["log_dir"])
    limit = cfg["alert"]["no_data_minutes"]
    state_file = log_dir / "alert_state.json"
    was_down = state_file.exists() and json.loads(state_file.read_text()).get("down", False)

    age = minutes_since_last_save(log_dir / "heartbeat.json")
    is_down = age is None or age > limit

    if is_down and not was_down:
        detail = "no data has ever been saved" if age is None else f"last save was {age:.0f} min ago"
        send_alert(f"Collector problem: {detail}. Check logs/collector.log")
    elif was_down and not is_down:
        send_alert("Collector recovered: data is arriving again")
    else:
        print("OK" if not is_down else "Still down (already alerted)", f"- last save {age:.1f} min ago" if age is not None else "")

    log_dir.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"down": is_down}))


if __name__ == "__main__":
    check()
