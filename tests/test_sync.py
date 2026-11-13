# unittest builds per-test state in setUp, not __init__.
# pyright: reportUninitializedInstanceVariable=false

"""Regression tests against local Audiobookshelf and Toggl stand-ins.

Every test drives the real CLI, Requests and SQLite paths. Only the Toggl base
URL and the clock inside ``abs_sync.sync`` are substituted.
"""

import base64
import fcntl
import json
import math
import os
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, cast, override
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import requests
from typer.testing import CliRunner, Result

import abs_sync.sync
from abs_sync import app
from abs_sync.state import Ledger

ABS_KEY = "abs-test-key"
TOGGL_TOKEN = "toggl-test-token"
WORKSPACE = 111
PROJECT = 222
EPOCH = datetime(2026, 9, 1, tzinfo=UTC)

type Json = dict[str, object]
type Call = tuple[str, int | None, Json]
type Scripted = tuple[Literal["apply", "drop", "reject"], int, Mapping[str, str]]


def at(hours: float) -> str:
    """UTC timestamp ``hours`` after 2026-09-01T00:00:00Z."""
    return (EPOCH + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_ms(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp).timestamp() * 1000)


def book(
    session_id: str,
    start: str = "2026-09-01T12:00:00Z",
    listened: float = 1200,
    *,
    title: str = "The Book",
    updated: str | None = None,
    media_type: str = "book",
) -> Json:
    """An Audiobookshelf history record; media progress deliberately differs."""
    started = epoch_ms(start)
    return {
        "id": session_id,
        "userId": "user-1",
        "libraryItemId": f"item-{session_id}",
        "mediaType": media_type,
        "displayTitle": title,
        "mediaMetadata": {"title": title},
        "startedAt": started,
        "updatedAt": started + 2_400_000 if updated is None else epoch_ms(updated),
        "timeListening": listened,
        "startTime": 0,
        "currentTime": 2400,
    }


def managed(start: str, seconds: int, title: str) -> Json:
    stop = datetime.fromisoformat(start) + timedelta(seconds=seconds)
    return {
        "description": title,
        "start": start,
        "stop": stop.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration": seconds,
    }


def created(start: str, seconds: int = 1200, title: str = "The Book") -> Call:
    body = managed(start, seconds, title) | {
        "workspace_id": WORKSPACE,
        "project_id": PROJECT,
        "created_with": "abs-sync",
        "billable": False,
    }
    return ("POST", None, body)


def updated(entry_id: int, start: str, seconds: int, title: str = "The Book") -> Call:
    return (
        "PUT",
        entry_id,
        managed(start, seconds, title) | {"workspace_id": WORKSPACE},
    )


class FakeClock:
    """Replaces the ``time`` module inside ``abs_sync.sync``."""

    def __init__(self, now: float) -> None:
        self.now: float = now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Fixture:
    """Audiobookshelf history and Toggl time entries served over local HTTP."""

    def __init__(self, page_size: int = 2) -> None:
        self.sessions: list[Json] = []
        self.page_size: int = page_size
        self.failing_page: int | None = None
        self.abs_requests: int = 0
        self.entries: dict[int, Json] = {}
        self.calls: list[Call] = []
        self.responses: list[Scripted] = []
        """Behaviour for the next Toggl calls; default is apply and reply 200."""
        self.rejection_body: str = "rejected"
        self._next_id: int = 5000
        self._server: _Server = _Server(self)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.abs_url: str = f"{base}/abs"
        self.toggl_api: str = f"{base}/toggl/api/v9"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def entry_at(self, start: str) -> int:
        [entry_id] = [
            key for key, entry in self.entries.items() if entry["start"] == start
        ]
        return entry_id

    def handle(self, request: BaseHTTPRequestHandler, method: str) -> None:
        path = urlsplit(request.path).path
        if method == "GET" and path == "/abs/api/me/listening-sessions":
            self._history(request)
        elif path.startswith("/toggl/api/v9/"):
            self._toggl(request, method, path.removeprefix("/toggl/api/v9/"))
        else:
            _reply(request, 404, "unknown path")

    def _history(self, request: BaseHTTPRequestHandler) -> None:
        if request.headers.get("Authorization") != f"Bearer {ABS_KEY}":
            return _reply(request, 401, "unauthorized")
        self.abs_requests += 1
        page = int(parse_qs(urlsplit(request.path).query)["page"][0])
        if page == self.failing_page:
            return _reply(request, 500, "failed")
        ordered = sorted(
            self.sessions,
            key=lambda session: cast(int, session["updatedAt"]),
            reverse=True,
        )
        start = page * self.page_size
        _reply(
            request,
            200,
            {
                "total": len(ordered),
                "numPages": math.ceil(len(ordered) / self.page_size),
                "page": page,
                "itemsPerPage": self.page_size,
                "sessions": ordered[start : start + self.page_size],
            },
        )

    def _toggl(self, request: BaseHTTPRequestHandler, method: str, path: str) -> None:
        token = base64.b64encode(f"{TOGGL_TOKEN}:api_token".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {token}":
            return _reply(request, 403, "forbidden")
        segments = path.split("/")
        if segments[:3] != ["workspaces", str(WORKSPACE), "time_entries"]:
            return _reply(request, 404, "unknown workspace")
        entry_id = int(segments[3]) if method == "PUT" else None
        length = int(request.headers["Content-Length"])
        body = cast(Json, json.loads(request.rfile.read(length)))
        self.calls.append((method, entry_id, body))
        scripted: Scripted = (
            self.responses.pop(0) if self.responses else ("apply", 200, {})
        )
        kind, status, headers = scripted
        if kind == "reject":
            return _reply(request, status, self.rejection_body, headers)
        if entry_id is None:
            entry_id = self._next_id
            self._next_id += 1
            self.entries[entry_id] = {}
        elif entry_id not in self.entries:
            return _reply(request, 404, "not found")
        self.entries[entry_id].update(body)
        if kind == "drop":
            return  # applied, but the connection closes without a response
        _reply(request, status, {"id": entry_id, **self.entries[entry_id]}, headers)


class _Server(ThreadingHTTPServer):
    def __init__(self, fixture: Fixture) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.fixture: Fixture = fixture


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        cast(_Server, self.server).fixture.handle(self, "GET")

    def do_POST(self) -> None:
        cast(_Server, self.server).fixture.handle(self, "POST")

    def do_PUT(self) -> None:
        cast(_Server, self.server).fixture.handle(self, "PUT")

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass


def _reply(
    request: BaseHTTPRequestHandler,
    status: int,
    payload: object,
    headers: Mapping[str, str] | None = None,
) -> None:
    body = json.dumps(payload).encode()
    request.send_response(status)
    request.send_header("Content-Type", "application/json")
    request.send_header("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        request.send_header(name, value)
    request.end_headers()
    _ = request.wfile.write(body)


class SyncTest(unittest.TestCase):
    fixture: Fixture
    dir: Path
    config: Path
    state: Path
    clock: FakeClock

    @override
    def setUp(self) -> None:
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.config = self.dir / "config.toml"
        self.state = self.dir / "state" / "state.sqlite3"
        _ = self.config.write_text(self.config_text())
        self.clock = FakeClock(datetime(2026, 9, 20, tzinfo=UTC).timestamp())
        for patcher in (
            mock.patch.object(abs_sync.sync, "TOGGL_API", self.fixture.toggl_api),
            mock.patch.object(abs_sync.sync, "time", self.clock),
            mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1"}),
        ):
            _ = patcher.start()
            self.addCleanup(patcher.stop)

    def config_text(self, requests_per_hour: int = 25) -> str:
        return f"""
[audiobookshelf]
url = "{self.fixture.abs_url}/"
api_key = "{ABS_KEY}"

[toggl]
api_token = "{TOGGL_TOKEN}"
workspace_id = {WORKSPACE}
project_id = {PROJECT}

[sync]
requests_per_hour = {requests_per_hour}
state_path = "state/state.sqlite3"
"""

    def sync(self, *args: str, expect: int = 0) -> Result:
        result = CliRunner().invoke(app, [*args, "--config", str(self.config)])
        self.assertEqual(result.exit_code, expect, result.output)
        return result

    def test_growing_session_updates_its_one_entry(self) -> None:
        self.fixture.sessions = [book("S", "2026-09-01T12:00:00Z", 1200)]
        _ = self.sync("--since", "2026-09-01", "--dry-run")
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(list(self.dir.iterdir()), [self.config])

        _ = self.sync("--since", "2026-09-01")
        self.assertTrue(self.state.exists())  # relative to the config file
        entry_id = self.fixture.entry_at("2026-09-01T12:00:00Z")
        # The session is still active: its listening time keeps growing.
        self.fixture.sessions[0]["timeListening"] = 1800
        _ = self.sync()
        _ = self.sync()
        self.fixture.sessions[0]["updatedAt"] = epoch_ms("2026-09-01T13:30:00Z")
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls,
            [
                created("2026-09-01T12:00:00Z", 1200),
                updated(entry_id, "2026-09-01T12:00:00Z", 1800),
            ],
        )

        self.fixture.sessions[0] |= {"displayTitle": "Renamed", "timeListening": 900}
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls[2:],
            [updated(entry_id, "2026-09-01T12:00:00Z", 900, "Renamed")],
        )
        self.assertEqual(list(self.fixture.entries), [entry_id])

    def test_podcasts_use_series_names_and_keep_separate_session_entries(self) -> None:
        series = "Nihongo con Teppei 波!"
        self.fixture.sessions = [
            book("P1", at(0), 600.9, title="Episode one", media_type="podcast"),
            book("P2", at(1), 300.8, title="Episode two", media_type="podcast"),
        ]
        for session in self.fixture.sessions:
            session["libraryItemId"] = "podcast-series"
            session["mediaMetadata"] = {"title": series}
        _ = self.sync("--since", "2026-09-01")
        self.assertEqual(
            self.fixture.calls,
            [created(at(0), 600, series), created(at(1), 300, series)],
        )
        entry_id = self.fixture.entry_at(at(0))
        self.fixture.sessions[0]["timeListening"] = 900.5
        self.fixture.sessions[0]["displayTitle"] = "Renamed episode"
        _ = self.sync()
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls[2:], [updated(entry_id, at(0), 900, series)]
        )
        self.assertEqual(self.fixture.entries[entry_id]["description"], series)
        self.assertEqual(len(self.fixture.entries), 2)

    def test_min_entry_duration_reconsiders_skipped_sessions(self) -> None:
        _ = self.config.write_text(
            self.config_text() + "min_entry_duration_seconds = 60\n"
        )
        self.fixture.sessions = [
            book("zero", at(0), 0.9),
            book("growing", at(1), 59.9),
            book("short-podcast", at(2), 30, media_type="podcast"),
            book("minimum", at(3), 60, media_type="podcast"),
            book("long", at(4), 61.9),
        ]
        _ = self.sync("--since", "2026-09-01", "--dry-run")
        self.assertEqual(self.fixture.calls, [])
        self.assertFalse(self.state.parent.exists())
        _ = self.sync("--since", "2026-09-01")
        self.assertEqual(self.fixture.calls, [created(at(3), 60), created(at(4), 61)])

        self.fixture.sessions[1]["timeListening"] = 60.7
        _ = self.sync()
        self.assertEqual(self.fixture.calls[2:], [created(at(1), 60)])

        # Disabling the cutoff imports the other short session, but not zeroes.
        _ = self.config.write_text(
            self.config_text() + "min_entry_duration_seconds = 0\n"
        )
        _ = self.sync()
        self.assertEqual(self.fixture.calls[3:], [created(at(2), 30)])

    def test_min_entry_duration_keeps_mapped_entries_current(self) -> None:
        self.fixture.sessions = [book("S", at(0), 60)]
        _ = self.sync("--since", "2026-09-01")
        entry_id = self.fixture.entry_at(at(0))

        _ = self.config.write_text(
            self.config_text() + "min_entry_duration_seconds = 120\n"
        )
        self.fixture.sessions[0]["timeListening"] = 30
        self.fixture.responses = [("drop", 0, {})]
        _ = self.sync(expect=1)
        _ = self.sync()  # Retry the unconfirmed update despite the new cutoff.
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls,
            [
                created(at(0), 60),
                updated(entry_id, at(0), 30),
                updated(entry_id, at(0), 30),
            ],
        )
        self.assertEqual(self.fixture.entries[entry_id]["duration"], 30)

    def test_full_scan_finds_late_history_and_keeps_old_entries_current(self) -> None:
        self.fixture.sessions = [
            book("A", "2026-09-10T08:00:00Z", 600),
            book("podcast", "2026-09-10T09:00:00Z", 600, media_type="podcast"),
            book("zero", "2026-09-10T10:00:00Z", 0.9),
            book("old", "2026-07-01T10:00:00Z", 600),
            book("D", "2026-09-11T10:00:00Z", 600),
            book("D", "2026-09-11T10:00:00Z", 660),  # disagreeing duplicate
        ]
        _ = self.sync("--since", "2026-08-01")
        self.assertEqual(
            self.fixture.calls,
            [
                created("2026-09-10T08:00:00Z", 600),
                created("2026-09-10T09:00:00Z", 600),
            ],
        )
        a_id = self.fixture.entry_at("2026-09-10T08:00:00Z")

        _ = self.fixture.sessions.pop()
        # An offline upload keeps its old client timestamps, so it sorts onto the
        # last page of a scan after newer sessions were already imported.
        self.fixture.sessions.append(
            book("late", "2026-08-15T00:00:00Z", 300, updated="2026-08-15T00:05:00Z")
        )
        self.fixture.sessions[0] |= {
            "startedAt": epoch_ms("2026-07-31T23:00:00Z"),
            "timeListening": 700,
        }
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls[2:],
            [
                updated(a_id, "2026-07-31T23:00:00Z", 700),
                created("2026-08-15T00:00:00Z", 300),
                created("2026-09-11T10:00:00Z", 600),
            ],
        )

        self.fixture.sessions[0]["timeListening"] = 0.4
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls[5:], [updated(a_id, "2026-07-31T23:00:00Z", 0)]
        )

        self.fixture.sessions.clear()
        _ = self.sync()
        self.assertEqual(len(self.fixture.calls), 6)
        self.assertEqual(len(self.fixture.entries), 4)

    def test_source_failures_stop_before_any_toggl_request(self) -> None:
        self.fixture.sessions = [book(f"S{index}", at(index)) for index in range(5)]
        self.fixture.failing_page = 2
        _ = self.sync("--since", "2026-09-01", expect=1)
        self.fixture.failing_page = None
        self.fixture.sessions[4]["timeListening"] = "invalid"
        _ = self.sync("--since", "2026-09-01", expect=1)
        self.assertEqual(self.fixture.calls, [])

    def test_negative_listening_time_is_deferred_and_reconsidered(self) -> None:
        # Observed in an actual Lissen session returned by Audiobookshelf.
        self.fixture.sessions = [
            book("negative", at(0), -1.014),
            book("A", at(1)),
            book("B", at(2)),
        ]
        output = self.sync("--since", "2026-09-01", "--dry-run").output
        self.assertIn("deferred", output)
        self.assertIn("negative timeListening (-1.014)", output)
        self.assertIn("would sync: created 2", output)
        self.assertEqual(self.fixture.calls, [])
        self.assertFalse(self.state.parent.exists())

        _ = self.sync("--since", "2026-09-01")
        self.assertEqual(self.fixture.calls, [created(at(1)), created(at(2))])
        with sqlite3.connect(self.state) as conn:
            self.assertEqual(set(Ledger(conn).rows()), {"A", "B"})
            self.assertEqual(Ledger(conn).budget(self.clock.now, 25).available, 23)

        self.fixture.sessions[0]["timeListening"] = 60.7
        _ = self.sync()
        self.assertEqual(self.fixture.calls[2:], [created(at(0), 60)])

    def test_negative_listening_time_preserves_existing_entry(self) -> None:
        self.fixture.sessions = [book("A", at(0))]
        _ = self.sync("--since", "2026-09-01")
        entry_id = self.fixture.entry_at(at(0))
        with sqlite3.connect(self.state) as conn:
            before = Ledger(conn).rows()["A"]

        self.fixture.sessions[0]["timeListening"] = -1.014
        self.fixture.sessions.append(book("B", at(1)))
        _ = self.sync()
        self.assertEqual(self.fixture.calls[1:], [created(at(1))])
        self.assertEqual(self.fixture.entries[entry_id]["duration"], 1200)
        with sqlite3.connect(self.state) as conn:
            self.assertEqual(Ledger(conn).rows()["A"], before)

        self.fixture.sessions[0]["timeListening"] = 900
        _ = self.sync()
        self.assertEqual(self.fixture.calls[2:], [updated(entry_id, at(0), 900)])

    def test_negative_listening_time_defers_all_copies_of_session(self) -> None:
        self.fixture.sessions = [
            book("A", at(0), -1.014),
            book("A", at(0)),
            book("B", at(1)),
            book("B", at(1), -5),
            book("C", at(2)),
        ]
        _ = self.sync("--since", "2026-09-01")
        self.assertEqual(self.fixture.calls, [created(at(2))])

        self.fixture.sessions = [book("A", at(0)), book("B", at(1)), book("C", at(2))]
        _ = self.sync()
        self.assertEqual(self.fixture.calls[1:], [created(at(0)), created(at(1))])

    def test_negative_listening_time_still_checks_user_identity(self) -> None:
        self.fixture.sessions = [book("A", at(0)), book("B", at(1), -1.014)]
        self.fixture.sessions[1]["userId"] = "another-user"
        output = self.sync("--since", "2026-09-01", expect=1).output
        self.assertIn("more than one user", output)
        self.assertEqual(self.fixture.calls, [])

    def test_negative_listening_time_does_not_hide_other_invalid_fields(self) -> None:
        cases: tuple[tuple[Json, str], ...] = (
            ({"startedAt": 253_402_300_800_000}, "exceeds the calendar range"),
            (
                {"displayTitle": None, "mediaMetadata": None, "libraryItemId": None},
                "no usable title or libraryItemId",
            ),
        )
        for fields, error in cases:
            for dry_run in (True, False):
                with self.subTest(fields=fields, dry_run=dry_run):
                    self.fixture.sessions = [
                        book("A", at(0)),
                        book("B", at(1), -1.014) | fields,
                    ]
                    args = ("--dry-run",) if dry_run else ()
                    output = self.sync("--since", "2026-09-01", *args, expect=1).output
                    self.assertIn(error, output)
                    self.assertEqual(self.fixture.calls, [])

    def test_malformed_source_times_still_stop_all_writes(self) -> None:
        self.fixture.sessions = [book("A", at(0)), book("B", at(1))]
        for key, value in (
            ("timeListening", None),
            ("timeListening", "-1.014"),
            ("timeListening", True),
            ("timeListening", math.nan),
            ("timeListening", math.inf),
            ("timeListening", -math.inf),
            ("startedAt", -1),
        ):
            with self.subTest(key=key, value=value):
                self.fixture.sessions[1] = book("B", at(1)) | {key: value}
                output = self.sync("--since", "2026-09-01", expect=1).output
                self.assertIn(f"invalid {key}", output)
                self.assertEqual(self.fixture.calls, [])

    def test_budget_caps_requests_per_rolling_hour_across_runs(self) -> None:
        self.fixture.sessions = [book(f"S{index:02}", at(index)) for index in range(27)]
        _ = self.sync("--since", "2026-09-01")
        self.assertEqual(
            self.fixture.calls, [created(at(index)) for index in range(25)]
        )

        self.clock.now += 1800
        _ = self.sync()  # a restart inside the hour sends nothing
        self.assertEqual(len(self.fixture.calls), 25)

        self.fixture.sessions[0]["timeListening"] = 1500
        self.clock.now += 1800
        _ = self.sync()
        first = self.fixture.entry_at(at(0))
        self.assertEqual(
            self.fixture.calls[25:],
            [updated(first, at(0), 1500), created(at(25)), created(at(26))],
        )

    def test_failed_attempts_count_against_the_budget(self) -> None:
        _ = self.config.write_text(self.config_text(requests_per_hour=2))
        self.fixture.sessions = [book("A")]
        _ = self.sync("--since", "2026-09-01")
        entry_id = self.fixture.entry_at("2026-09-01T12:00:00Z")
        self.fixture.sessions[0]["timeListening"] = 1500
        self.fixture.responses = [("reject", 503, {})]
        _ = self.sync(expect=1)
        _ = self.sync()  # both attempts in this hour are spent
        self.assertEqual(len(self.fixture.calls), 2)

        self.clock.now += 3600
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls[2:], [updated(entry_id, "2026-09-01T12:00:00Z", 1500)]
        )

    def test_quota_responses_stop_the_run_and_persist_a_cooldown(self) -> None:
        self.fixture.sessions = [book("A", at(0)), book("B", at(1))]
        self.fixture.responses = [("reject", 402, {"X-Toggl-Quota-Resets-In": "600"})]
        _ = self.sync("--since", "2026-09-01")
        self.clock.now += 590
        _ = self.sync()
        self.assertEqual(self.fixture.calls, [created(at(0))])
        self.clock.now += 20
        _ = self.sync()  # the 402 proved nothing was created
        self.assertEqual(self.fixture.calls[1:], [created(at(0)), created(at(1))])

        self.fixture.sessions += [book("C", at(2)), book("D", at(3))]
        self.fixture.responses = [
            (
                "apply",
                200,
                {"X-Toggl-Quota-Remaining": "0", "X-Toggl-Quota-Resets-In": "300"},
            )
        ]
        _ = self.sync()
        self.assertEqual(self.fixture.calls[3:], [created(at(2))])
        self.clock.now += 301
        _ = self.sync()
        self.assertEqual(self.fixture.calls[4:], [created(at(3))])

        a_id = self.fixture.entry_at(at(0))
        self.fixture.sessions[0]["timeListening"] = 1500
        self.fixture.responses = [("reject", 429, {"Retry-After": "7200"})]
        _ = self.sync()
        self.clock.now += 3700  # past the one-hour default, inside Retry-After
        _ = self.sync()
        self.assertEqual(self.fixture.calls[5:], [updated(a_id, at(0), 1500)])
        self.clock.now += 3600
        _ = self.sync()
        self.assertEqual(self.fixture.calls[6:], [updated(a_id, at(0), 1500)])
        self.assertEqual(len(self.fixture.entries), 4)

    def test_long_server_cooldowns_are_not_shortened(self) -> None:
        self.fixture.sessions = [book("A", at(0))]
        self.fixture.responses = [
            ("reject", 402, {"X-Toggl-Quota-Resets-In": str(8 * 86400)})
        ]
        _ = self.sync("--since", "2026-09-01")
        self.clock.now += 3601
        _ = self.sync()
        self.assertEqual(self.fixture.calls, [created(at(0))])
        self.clock.now += 8 * 86400
        _ = self.sync()
        entry_id = self.fixture.entry_at(at(0))

        self.fixture.sessions[0]["timeListening"] = 1500
        self.fixture.responses = [("reject", 429, {"Retry-After": str(9 * 86400)})]
        _ = self.sync()
        self.clock.now += 8 * 86400
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls,
            [created(at(0)), created(at(0)), updated(entry_id, at(0), 1500)],
        )
        self.clock.now += 86401
        _ = self.sync()
        self.assertEqual(self.fixture.calls[3:], [updated(entry_id, at(0), 1500)])

    def test_cooldown_survives_failed_acknowledgement(self) -> None:
        self.fixture.sessions = [book("A", at(0))]
        _ = self.sync("--since", "2026-09-01")
        entry_id = self.fixture.entry_at(at(0))
        self.fixture.sessions[0]["timeListening"] = 1500
        self.fixture.responses = [
            (
                "apply",
                200,
                {"X-Toggl-Quota-Remaining": "0", "X-Toggl-Quota-Resets-In": "600"},
            )
        ]
        with mock.patch.object(
            Ledger,
            "mark_synced",
            side_effect=sqlite3.OperationalError("simulated state failure"),
        ):
            _ = self.sync(expect=1)
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls, [created(at(0)), updated(entry_id, at(0), 1500)]
        )
        self.clock.now += 601
        _ = self.sync()
        self.assertEqual(self.fixture.calls[2:], [updated(entry_id, at(0), 1500)])

    def test_errors_never_echo_response_or_transport_secrets(self) -> None:
        self.fixture.sessions = [book("A")]
        self.fixture.rejection_body = f"Authorization: {TOGGL_TOKEN}"
        self.fixture.responses = [("reject", 401, {})]
        output = self.sync("--since", "2026-09-01", expect=1).output
        self.assertNotIn(TOGGL_TOKEN, output)
        output = self.sync(expect=1).output
        self.assertNotIn(TOGGL_TOKEN, output)
        self.assertNotIn(TOGGL_TOKEN.encode(), self.state.read_bytes())

        with mock.patch.object(
            requests.Session, "get", side_effect=requests.ConnectionError(ABS_KEY)
        ):
            output = self.sync(expect=1).output
        self.assertNotIn(ABS_KEY, output)

    def test_concurrent_writer_exits_75_without_requests(self) -> None:
        self.fixture.sessions = [book("A")]
        self.state.parent.mkdir()
        with self.state.with_name("state.sqlite3.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _ = self.sync("--since", "2026-09-01", expect=75)
        self.assertEqual(self.fixture.abs_requests, 0)
        self.assertEqual(self.fixture.calls, [])

    def test_lost_create_response_pauses_writes_until_resolved(self) -> None:
        self.fixture.sessions = [book("A", at(0)), book("B", at(1))]
        self.fixture.responses = [("drop", 0, {})]
        _ = self.sync("--since", "2026-09-01", expect=1)
        _ = self.sync(expect=1)
        self.assertEqual(self.fixture.calls, [created(at(0))])

        entry_id = self.fixture.entry_at(at(0))
        self.fixture.sessions[0]["timeListening"] = 1500
        # Operator repair, after inspecting Toggl; no recovery command is exposed.
        with sqlite3.connect(self.state) as conn:
            _ = conn.execute(
                """
                UPDATE sessions
                SET toggl_entry_id = ?, status = 'updating', last_payload_json = NULL,
                    intent_json = NULL, error = NULL
                WHERE session_id = ? AND status IN ('creating', 'blocked')
                """,
                (entry_id, "A"),
            )
            _ = conn.execute(
                """
                DELETE FROM sessions WHERE session_id = ?
                AND status IN ('creating', 'blocked') AND toggl_entry_id IS NULL
                """,
                ("A",),
            )  # cannot erase a known mapping
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls[1:], [updated(entry_id, at(0), 1500), created(at(1))]
        )
        self.assertEqual(len(self.fixture.entries), 2)

    def test_not_created_resolution_allows_one_fresh_create(self) -> None:
        self.fixture.sessions = [book("A")]
        self.fixture.responses = [("reject", 503, {})]
        _ = self.sync("--since", "2026-09-01", expect=1)
        with sqlite3.connect(self.state) as conn:
            _ = conn.execute(
                """
                DELETE FROM sessions WHERE session_id = ?
                AND status IN ('creating', 'blocked') AND toggl_entry_id IS NULL
                """,
                ("A",),
            )
        _ = self.sync()
        with sqlite3.connect(self.state) as conn:
            _ = conn.execute(
                """
                DELETE FROM sessions WHERE session_id = ?
                AND status IN ('creating', 'blocked') AND toggl_entry_id IS NULL
                """,
                ("A",),
            )  # healthy mappings stay intact
        _ = self.sync()
        self.assertEqual(self.fixture.calls, [created("2026-09-01T12:00:00Z")] * 2)
        self.assertEqual(len(self.fixture.entries), 1)

    def test_unconfirmed_update_is_resent_after_the_source_reverts(self) -> None:
        self.fixture.sessions = [book("A", listened=1200)]
        _ = self.sync("--since", "2026-09-01")
        entry_id = self.fixture.entry_at("2026-09-01T12:00:00Z")
        self.fixture.sessions[0]["timeListening"] = 1800
        self.fixture.responses = [("drop", 0, {})]
        _ = self.sync(expect=1)
        self.assertEqual(self.fixture.entries[entry_id]["duration"], 1800)

        # Back to the last acknowledged values, yet the lost PUT may have applied.
        self.fixture.sessions[0]["timeListening"] = 1200
        _ = self.sync()
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls[1:],
            [
                updated(entry_id, "2026-09-01T12:00:00Z", 1800),
                updated(entry_id, "2026-09-01T12:00:00Z", 1200),
            ],
        )
        self.assertEqual(self.fixture.entries[entry_id]["duration"], 1200)

    def test_rejected_update_blocks_without_retries_or_replacements(self) -> None:
        self.fixture.sessions = [book("A", at(0)), book("B", at(1))]
        _ = self.sync("--since", "2026-09-01")
        a_id, b_id = self.fixture.entry_at(at(0)), self.fixture.entry_at(at(1))
        for session in self.fixture.sessions:
            session["timeListening"] = 1500
        self.fixture.responses = [("reject", 404, {})]
        _ = self.sync(expect=1)
        _ = self.sync(expect=1)
        self.assertEqual(self.fixture.calls[2:], [updated(a_id, at(0), 1500)])

        with sqlite3.connect(self.state) as conn:
            _ = conn.execute(
                """
                UPDATE sessions
                SET toggl_entry_id = ?, status = 'updating', last_payload_json = NULL,
                    intent_json = NULL, error = NULL
                WHERE session_id = ? AND status IN ('creating', 'blocked')
                """,
                (a_id, "A"),
            )
        _ = self.sync()
        self.assertEqual(
            self.fixture.calls[3:],
            [updated(a_id, at(0), 1500), updated(b_id, at(1), 1500)],
        )

    def test_import_boundary_is_explicit_and_only_widens(self) -> None:
        self.fixture.sessions = [book("A", "2026-09-05T08:00:00Z"), book("B", at(-240))]
        _ = self.sync(expect=2)
        _ = self.sync("--dry-run", expect=2)
        _ = self.sync("--since", "2026-09-01", "--dry-run")
        self.assertEqual(list(self.dir.iterdir()), [self.config])

        _ = self.sync("--since", "2026-09-01")
        _ = self.sync("--since", "2026-09-02", expect=2)
        _ = self.sync("--since", "2026-08-01", "--dry-run")
        _ = self.sync()  # the dry run did not widen the saved boundary
        self.assertEqual(self.fixture.calls, [created("2026-09-05T08:00:00Z")])
        _ = self.sync("--since", "2026-08-01")
        self.assertEqual(self.fixture.calls[1:], [created(at(-240))])

    def test_missing_or_malformed_config_stops_before_any_request(self) -> None:
        valid = self.config_text()
        cases = (
            valid.replace("[toggl]", "[wrong_table]"),
            valid.replace('api_key = "', 'missing_key = "'),
            valid + f'broken = "{TOGGL_TOKEN}\n',
        )
        for text in cases:
            with self.subTest(text=text):
                _ = self.config.write_text(text)
                output = self.sync("--since", "2026-09-01", expect=2).output
                self.assertIn("cannot load config", output)
                for secret in (ABS_KEY, TOGGL_TOKEN):
                    self.assertNotIn(secret, output)
        self.config.unlink()
        _ = self.sync("--since", "2026-09-01", expect=2)
        self.assertEqual(self.fixture.abs_requests, 0)
        self.assertFalse(self.state.parent.exists())

    def test_config_defaults_and_relative_paths(self) -> None:
        _ = self.config.write_text(self.config_text().split("[sync]")[0])
        with (
            mock.patch.object(abs_sync.sync, "user_config_path", return_value=self.dir),
            mock.patch.object(
                abs_sync.sync, "user_state_path", return_value=self.dir / "data"
            ),
        ):
            config = abs_sync.sync.load_config(None)
        self.assertEqual(config.abs_url, self.fixture.abs_url)
        self.assertEqual(config.requests_per_hour, 25)
        self.assertEqual(config.min_entry_duration_seconds, 0)
        self.assertEqual(config.state_path, self.dir / "data" / "state.sqlite3")
        self.assertEqual(list(self.dir.iterdir()), [self.config])
        _ = self.config.write_text(self.config_text())
        self.assertEqual(abs_sync.sync.load_config(self.config).state_path, self.state)
        _ = self.config.write_text(
            self.config_text().replace(
                self.fixture.abs_url, "HTTP://ABS.EXAMPLE/Prefix"
            )
        )
        self.assertEqual(
            abs_sync.sync.load_config(self.config).abs_url, "http://abs.example/Prefix"
        )

    def test_dry_run_reuses_state_and_budget_without_modifying_files(self) -> None:
        _ = self.config.write_text(self.config_text(requests_per_hour=3))
        self.fixture.sessions = [book("A", at(0))]
        _ = self.sync("--since", "2026-09-01")
        entry_id = self.fixture.entry_at(at(0))
        self.fixture.sessions[0]["timeListening"] = 1500
        self.fixture.sessions += [book("B", at(1)), book("C", at(2))]
        before = {p: p.read_bytes() for p in self.dir.rglob("*") if p.is_file()}

        # A preview must also work while a writer holds the process lock.
        with self.state.with_name("state.sqlite3.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            output = self.sync("--dry-run").output
        self.assertIn("would update: session A", output)
        self.assertIn("would create: session B", output)
        self.assertIn("deferred 1", output)
        self.assertEqual(self.fixture.calls, [created(at(0))])
        self.assertEqual(
            before, {p: p.read_bytes() for p in self.dir.rglob("*") if p.is_file()}
        )

        _ = self.sync()
        self.assertEqual(
            self.fixture.calls[1:], [updated(entry_id, at(0), 1500), created(at(1))]
        )

    def test_existing_mappings_reject_changed_source_or_destination(self) -> None:
        self.fixture.sessions = [book("A", at(0))]
        _ = self.sync("--since", "2026-09-01")
        before = self.state.read_bytes()
        for text in (
            self.config_text().replace(
                self.fixture.abs_url, self.fixture.abs_url + "/other"
            ),
            self.config_text().replace(
                f"workspace_id = {WORKSPACE}", "workspace_id = 999"
            ),
            self.config_text().replace(f"project_id = {PROJECT}", "project_id = 999"),
        ):
            _ = self.config.write_text(text)
            _ = self.sync(expect=2)
        self.assertEqual(self.fixture.abs_requests, 1)
        _ = self.config.write_text(self.config_text())
        self.fixture.sessions[0]["userId"] = "another-user"
        _ = self.sync(expect=2)
        self.assertEqual(self.fixture.calls, [created(at(0))])
        self.assertEqual(self.state.read_bytes(), before)

    def test_invalid_min_entry_duration_stops_before_any_request(self) -> None:
        for value in ("-1", "1.5", "true", '"60"', "[]"):
            with self.subTest(value=value):
                _ = self.config.write_text(
                    self.config_text() + f"min_entry_duration_seconds = {value}\n"
                )
                output = self.sync("--since", "2026-09-01", expect=2).output
                self.assertIn("sync.min_entry_duration_seconds", output)
        self.assertEqual(self.fixture.abs_requests, 0)
        self.assertEqual(self.fixture.calls, [])
        self.assertFalse(self.state.parent.exists())


if __name__ == "__main__":
    _ = unittest.main()
