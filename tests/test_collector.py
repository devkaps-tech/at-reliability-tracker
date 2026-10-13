"""Tests for the collector's file naming, safe writing and retry rules."""

import gzip
from datetime import datetime, timezone

import requests

from tracker.collector import fetch_with_retries, raw_path, write_atomically


def test_file_goes_in_auckland_date_folder(tmp_path):
    # 12:30 UTC on 13 Oct is 01:30 on 14 Oct in Auckland (NZDT, UTC+13)
    path = raw_path(tmp_path, datetime(2026, 10, 13, 12, 30, 0, tzinfo=timezone.utc))
    assert path == tmp_path / "date=2026-10-14" / "013000+1300.json.gz"


def test_repeated_hour_when_daylight_saving_ends_gives_two_files(tmp_path):
    # On 4 Apr 2027 clocks go back 03:00 -> 02:00, so 02:30 happens twice in Auckland
    first = raw_path(tmp_path, datetime(2027, 4, 3, 13, 30, tzinfo=timezone.utc))   # 02:30 NZDT
    second = raw_path(tmp_path, datetime(2027, 4, 3, 14, 30, tzinfo=timezone.utc))  # 02:30 NZST
    assert first.name == "023000+1300.json.gz"
    assert second.name == "023000+1200.json.gz"


def test_write_atomically_compresses_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "date=2026-10-13" / "220044+1300.json.gz"
    write_atomically(path, b'{"status":"OK"}', compress=True)
    assert gzip.decompress(path.read_bytes()) == b'{"status":"OK"}'
    assert list(path.parent.iterdir()) == [path]


class FakeResponse:
    def __init__(self, status_code, body=b'{"status":"OK"}'):
        self.status_code, self.content = status_code, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        import json
        return json.loads(self.content)


class FakeSession:
    """Hands out pre-set responses (or errors) in order and counts the calls."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, timeout):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fetch(session):
    return fetch_with_retries(session, "http://x", timeout=1, retry_waits=[0, 0], deadline=float("inf"))


def test_retries_after_server_error_and_timeout_then_succeeds():
    session = FakeSession(FakeResponse(503), requests.Timeout("slow"), FakeResponse(200))
    body, feed = fetch(session)
    assert feed == {"status": "OK"} and session.calls == 3


def test_gives_up_after_three_failures():
    session = FakeSession(FakeResponse(500), FakeResponse(500), FakeResponse(500))
    assert fetch(session) is None and session.calls == 3


def test_retries_garbled_json():
    session = FakeSession(FakeResponse(200, b"<html>oops"), FakeResponse(200))
    assert fetch(session) is not None and session.calls == 2


def test_does_not_retry_bad_key_or_quota():
    for code in (401, 403, 429):
        session = FakeSession(FakeResponse(code))
        assert fetch(session) is None and session.calls == 1
