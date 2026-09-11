"""Tests for what the REST client does with answers that are not what it asked for.

On start, while it migrates its database, the engine serves a stand-in page on
its API address for a few seconds. The client used to hand that text back as if
it were the JSON asked for, and three worker threads called .get() on a string
at the same moment. Served here from a local socket; no network needed.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from oxeiosync.syncthing.api import SyncthingApi, SyncthingUnavailableError


class _Handler(BaseHTTPRequestHandler):
    #: Set per test: (content type, body) to answer every request with.
    answer: tuple[str, bytes] = ("text/plain", b"")

    def do_GET(self):  # noqa: N802
        content_type, body = self.answer
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def engine():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def answering(content_type: str, body: bytes) -> SyncthingApi:
        _Handler.answer = (content_type, body)
        return SyncthingApi(f"http://127.0.0.1:{server.server_address[1]}", "key", timeout=5)

    yield answering
    server.shutdown()
    server.server_close()


MIGRATING = ("text/html", b"<html><body>Database migration in progress</body></html>")


def test_a_stand_in_page_is_the_engine_not_being_ready(engine):
    api = engine(*MIGRATING)

    with pytest.raises(SyncthingUnavailableError, match="not ready"):
        api.system_status()


def test_polling_events_from_a_stand_in_page_is_not_ready_either(engine):
    api = engine(*MIGRATING)

    with pytest.raises(SyncthingUnavailableError):
        api.disk_events(since=0, limit=10, timeout=0)


def test_ping_says_no_rather_than_raising(engine):
    assert engine(*MIGRATING).ping() is False


def test_an_event_feed_that_is_not_a_list_is_not_trusted(engine):
    api = engine("application/json", json.dumps({"surprise": True}).encode())

    with pytest.raises(SyncthingUnavailableError):
        api.events(since=0, limit=10, timeout=0)


def test_entries_in_a_feed_that_are_not_events_are_dropped(engine):
    event = {"id": 1, "type": "LocalChangeDetected", "data": {}}
    api = engine("application/json", json.dumps(["noise", event, 7]).encode())

    assert api.disk_events(since=0, limit=10, timeout=0) == [event]


def test_real_json_still_comes_back_as_itself(engine):
    api = engine("application/json", json.dumps({"myID": "ABC", "startTime": "t"}).encode())

    assert api.system_status() == {"myID": "ABC", "startTime": "t"}
