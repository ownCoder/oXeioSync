"""Tests for how quickly an event poller stops while a long poll is in flight.

A poll blocked in a read cannot be broken off from another thread on Windows,
so a poller only stops once the engine answers. When that took up to 55 s,
quitting gave up waiting and the interpreter destroyed QThreads that were still
running, which Qt answers by aborting the process (exit code 0xC0000409). The
engine is stood in for by a local server that holds each request for as long as
it is asked to, as the real one does. No network needed.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from oxeiosync.syncthing.api import EVENT_POLL_TIMEOUT
from oxeiosync.syncthing.events import EventPoller

#: Allowance on top of the hold for the request itself and thread wake-up.
SLACK = 1.5


class _HoldingEngine(BaseHTTPRequestHandler):
    in_flight = threading.Event()

    def do_GET(self):  # noqa: N802
        query = parse_qs(urlsplit(self.path).query)
        hold = float(query.get("timeout", ["0"])[0])
        if urlsplit(self.path).path == "/rest/system/status":
            body = b'{"startTime": "run-1"}'
        else:
            if hold:
                type(self).in_flight.set()
            time.sleep(hold)  # nothing happens: the engine answers empty
            body = b"[]"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def engine_url():
    _HoldingEngine.in_flight = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HoldingEngine)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_the_hold_is_short_enough_for_quit_to_wait_out():
    """Quit's deadline is only useful if a poll that has just begun fits in it."""
    from oxeiosync import app

    assert EVENT_POLL_TIMEOUT + app.POLLER_STOP_MARGIN <= 10
    assert app.POLLER_STOP_MARGIN >= SLACK


@pytest.mark.parametrize("disk", [False, True], ids=["general feed", "file-change feed"])
def test_a_poller_stops_within_one_hold_of_being_told_to(engine_url, disk):
    poller = EventPoller(engine_url, "key", disk=disk, backlog=100 if disk else 0)
    poller.start()
    try:
        assert _HoldingEngine.in_flight.wait(10), "the poller never started a long poll"
        time.sleep(0.2)  # well inside the hold

        told = time.monotonic()
        poller.stop()
        stopped = poller.wait(int((EVENT_POLL_TIMEOUT + 5) * 1000))
        took = time.monotonic() - told
    finally:
        poller.stop()
        poller.wait(20_000)

    assert stopped
    assert took <= EVENT_POLL_TIMEOUT + SLACK, f"took {took:.2f}s to stop"
