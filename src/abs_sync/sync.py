"""Fetch Audiobookshelf sessions, compare SQLite mappings, and write changes to Toggl."""

import email.utils
import json
import math
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast, override
from urllib.parse import urlsplit

import requests
from platformdirs import user_config_path, user_state_path
from requests.auth import AuthBase

from .state import SPACING_SECONDS, Ledger, open_ledger

TOGGL_API = "https://api.track.toggl.com/api/v9"
TIMEOUT = (5, 30)
PAGE_SIZE = 100
_QUOTA_STATUSES = {402, 429}
_REJECTED_STATUSES = {400, 401, 403, 404, 405, 410, 422}
_PAUSED = "Toggl writes are paused; check Toggl and repair the state row (see README)"


class ConfigError(Exception):
    """Invalid configuration or command-line input."""


class SyncError(Exception):
    """A source, transport, data or state failure."""


class _NegativeListeningTime(SyncError):
    """An identifiable source session to defer without changing its Toggl entry."""

    def __init__(self, session_id: str, user_id: str, value: float) -> None:
        super().__init__(
            f"Audiobookshelf session {session_id} has negative timeListening ({value})"
        )
        self.session_id: str = session_id
        self.user_id: str = user_id


@dataclass(frozen=True)
class Config:
    abs_url: str
    abs_api_key: str = field(repr=False)
    toggl_api_token: str = field(repr=False)
    workspace_id: int
    project_id: int
    requests_per_hour: int
    min_entry_duration_seconds: int
    state_path: Path


def load_config(path: Path | None) -> Config:
    """Read the documented TOML layout; only enforce the two sync limits."""
    path = path or user_config_path("abs-sync", appauthor=False) / "config.toml"
    path = path.expanduser().absolute()
    try:
        with path.open("rb") as file:
            data = cast(dict[str, dict[str, object]], tomllib.load(file))
        source, toggl, options = (
            data["audiobookshelf"],
            data["toggl"],
            data.get("sync", {}),
        )
        state = options.get("state_path")
        url = urlsplit(cast(str, source["url"]).strip())
        base_url = url._replace(
            scheme=url.scheme.lower(),
            netloc=url.netloc.lower(),
            path=url.path.rstrip("/"),
        ).geturl()
        config = Config(
            abs_url=base_url,
            abs_api_key=cast(str, source["api_key"]),
            toggl_api_token=cast(str, toggl["api_token"]),
            workspace_id=cast(int, toggl["workspace_id"]),
            project_id=cast(int, toggl["project_id"]),
            requests_per_hour=cast(int, options.get("requests_per_hour", 25)),
            min_entry_duration_seconds=cast(
                int, options.get("min_entry_duration_seconds", 0)
            ),
            state_path=(
                user_state_path("abs-sync", appauthor=False) / "state.sqlite3"
                if state is None
                else path.parent / Path(cast(str, state)).expanduser()
            ),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Do not echo malformed config values, which may contain credentials.
        raise ConfigError(
            f"cannot load config {path}: {type(exc).__name__}; see README"
        ) from None
    if (
        type(config.requests_per_hour) is not int
        or not 1 <= config.requests_per_hour <= 30
    ):
        raise ConfigError("sync.requests_per_hour must be an integer from 1 to 30")
    if (
        type(config.min_entry_duration_seconds) is not int
        or config.min_entry_duration_seconds < 0
    ):
        raise ConfigError(
            "sync.min_entry_duration_seconds must be a non-negative integer"
        )
    return config


@dataclass(frozen=True)
class ListeningSession:
    session_id: str
    user_id: str
    description: str
    start_s: int
    duration: int

    def payload(self) -> dict[str, str | int]:
        return {
            "description": self.description,
            "start": _rfc3339(self.start_s),
            "stop": _rfc3339(self.start_s + self.duration),
            "duration": self.duration,
        }


def run_sync(
    config: Config,
    since: date | None,
    *,
    dry_run: bool,
    log: Callable[[str], None] = print,
) -> None:
    """Fetch and validate the full source, then compare and write each changed session."""
    if since is None and not config.state_path.exists():
        raise ConfigError("no import boundary is saved yet; pass --since YYYY-MM-DD")
    with open_ledger(config.state_path, dry_run=dry_run) as ledger:
        meta, rows = ledger.meta(), ledger.rows()
        boundary = _boundary(meta.get("import_since"), since)
        bindings = {
            "abs_url": config.abs_url,
            "toggl_workspace_id": str(config.workspace_id),
            "toggl_project_id": str(config.project_id),
        }
        for key, value in bindings.items():
            if rows and key in meta and meta[key] != value:
                raise ConfigError(
                    f"{key} differs from the value this database belongs to"
                )

        sessions, users = _fetch_sessions(config, log)
        if users:
            user_id = next(iter(users))
            if rows and meta.get("abs_user_id", user_id) != user_id:
                raise ConfigError("audiobookshelf.api_key belongs to a different user")
            bindings["abs_user_id"] = user_id
        ledger.set_meta(bindings | {"import_since": boundary.isoformat()})

        unresolved = [
            row for row in rows.values() if row.status in {"creating", "blocked"}
        ]
        for row in unresolved:
            log(
                f"unresolved session {row.session_id} ({row.status}, Toggl ID "
                + f"{row.toggl_entry_id}): {row.error or 'interrupted before acknowledgement'}"
            )
        if unresolved:
            raise SyncError(_PAUSED)
        missing = [
            row.session_id
            for row in rows.values()
            if row.status == "updating" and row.session_id not in sessions
        ]
        if missing:
            log(
                f"waiting for source sessions with unconfirmed updates: {', '.join(missing)}"
            )

        if dry_run:
            log(
                f"dry run (import boundary {boundary}): no Toggl requests or state changes"
            )
        counts: Counter[str] = Counter()
        boundary_s = int(
            datetime.combine(boundary, datetime.min.time(), UTC).timestamp()
        )
        # Existing entries take priority over the import backlog.
        ordered = sorted(
            sessions.values(),
            key=lambda s: (s.session_id not in rows, s.start_s, s.session_id),
        )
        with requests.Session() as toggl:
            toggl.auth = (config.toggl_api_token, "api_token")
            for session in ordered:
                row = rows.get(session.session_id)
                if row is None and (
                    session.start_s < boundary_s
                    or session.duration == 0
                    or session.duration < config.min_entry_duration_seconds
                ):
                    counts["skipped"] += 1
                    continue
                payload_json = _canonical(session.payload())
                if (
                    row is not None
                    and row.status == "synced"
                    and row.last_payload_json == payload_json
                ):
                    counts["unchanged"] += 1
                    continue
                if counts["deferred"]:
                    counts["deferred"] += 1
                    continue

                now = time.time()
                budget = ledger.budget(now, config.requests_per_hour)
                if budget.available == 0:
                    log(
                        "quota: remaining writes deferred for "
                        + f"{math.ceil(budget.next_available - now)} seconds"
                    )
                    counts["deferred"] += 1
                    continue
                if not dry_run and budget.last_attempt is not None:
                    pause = min(
                        SPACING_SECONDS,
                        max(0, budget.last_attempt + SPACING_SECONDS - now),
                    )
                    if pause:
                        time.sleep(pause)
                entry_id = None if row is None else row.toggl_entry_id
                saved_id = _write(
                    ledger, config, toggl, session, entry_id, dry_run=dry_run
                )
                if saved_id is None and not dry_run:
                    log("quota: Toggl requested a cooldown; remaining writes deferred")
                    counts["deferred"] += 1
                    continue
                action = "created" if entry_id is None else "updated"
                counts[action] += 1
                label = f"would {action.removesuffix('d')}" if dry_run else action
                log(
                    f"{label}: session {session.session_id}, {session.description}, "
                    + f"{session.duration}s, Toggl entry {saved_id or 'new'}"
                )

        prefix = "would sync" if dry_run else "synced"
        log(
            f"{prefix}: created {counts['created']}, updated {counts['updated']}, "
            + f"unchanged {counts['unchanged']}, skipped {counts['skipped']} "
            + "(before boundary, zero-length or below minimum), "
            + f"deferred {counts['deferred']} (request budget)"
        )


def _boundary(saved: str | None, since: date | None) -> date:
    if saved is None:
        if since is None:
            raise ConfigError(
                "no import boundary is saved yet; pass --since YYYY-MM-DD"
            )
        return since
    previous = date.fromisoformat(saved)
    if since is not None and since > previous:
        raise ConfigError(
            f"--since {since} is later than the saved import boundary {previous}"
        )
    return previous if since is None else since


class _BearerAuth(AuthBase):
    """Explicit auth prevents Requests' netrc support from replacing the API key."""

    def __init__(self, token: str) -> None:
        self._token: str = token

    @override
    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Bearer {self._token}"
        return request


def _fetch_history(config: Config) -> Iterator[dict[str, object]]:
    with requests.Session() as source:
        source.auth = _BearerAuth(config.abs_api_key)
        page, pages = 0, 1
        while page < pages:
            try:
                response = source.get(
                    f"{config.abs_url}/api/me/listening-sessions",
                    params={"page": page, "itemsPerPage": PAGE_SIZE},
                    timeout=TIMEOUT,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise SyncError(
                    f"Audiobookshelf page {page} failed: {type(exc).__name__}"
                ) from None
            if response.status_code != 200:
                raise SyncError(
                    f"Audiobookshelf page {page} returned HTTP {response.status_code}"
                )
            try:
                envelope = _object(cast(object, response.json()))
            except ValueError:
                envelope = None
            if envelope is None:
                raise SyncError(f"Audiobookshelf page {page} is not a JSON object")
            records, page_count = (
                envelope.get("sessions"),
                _count(envelope.get("numPages")),
            )
            if (
                not isinstance(records, list)
                or page_count is None
                or _count(envelope.get("page")) != page
                or _count(envelope.get("total")) is None
                or _count(envelope.get("itemsPerPage")) is None
            ):
                raise SyncError(f"Audiobookshelf page {page} has an unexpected shape")
            for record in cast(list[object], records):
                fields = _object(record)
                if fields is None:
                    raise SyncError(
                        "Audiobookshelf returned a session that is not an object"
                    )
                yield fields
            if page == 0:
                pages = page_count
            page += 1


def _fetch_sessions(
    config: Config,
    log: Callable[[str], None],
) -> tuple[dict[str, ListeningSession], set[str]]:
    sessions: dict[str, ListeningSession] = {}
    conflicts: set[str] = set()
    users: set[str] = set()
    unsupported: set[str] = set()
    negative: set[str] = set()
    anonymous = 0
    for fields in _fetch_history(config):
        if fields.get("mediaType") not in ("book", "podcast"):
            session_id = fields.get("id")
            if isinstance(session_id, str) and session_id:
                unsupported.add(session_id)
            else:
                anonymous += 1
            continue
        try:
            session = _listening_session(fields)
        except _NegativeListeningTime as exc:
            users.add(exc.user_id)
            negative.add(exc.session_id)
            log(f"deferred: {exc}; will recheck on the next run")
            continue
        users.add(session.user_id)
        if sessions.setdefault(session.session_id, session) != session:
            conflicts.add(session.session_id)
    if len(users) > 1:
        raise SyncError(
            "Audiobookshelf history contains sessions of more than one user"
        )
    # A changing paginated history can repeat an ID with disagreeing values.
    conflicts.update(sessions.keys() & (unsupported | negative))
    for session_id in conflicts:
        del sessions[session_id]
    if conflicts:
        log(f"deferred conflicting source sessions: {', '.join(sorted(conflicts))}")
    skipped = len(unsupported - conflicts) + anonymous
    if skipped:
        log(f"skipped {skipped} unsupported media sessions")
    return sessions, users


def _listening_session(fields: Mapping[str, object]) -> ListeningSession:
    session_id, user_id = fields.get("id"), fields.get("userId")
    if not isinstance(session_id, str) or not session_id.strip():
        raise SyncError("Audiobookshelf returned a session without a valid id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise SyncError(f"Audiobookshelf session {session_id} has an invalid userId")
    values: list[int] = []
    negative_time: float | None = None
    for key in ("startedAt", "timeListening"):
        value = fields.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or isinstance(value, float)
            and not math.isfinite(value)
        ):
            raise SyncError(f"Audiobookshelf session {session_id} has an invalid {key}")
        if value < 0:
            if key == "timeListening":
                negative_time = value
            else:
                raise SyncError(f"Audiobookshelf session {session_id} has an invalid {key}")
        values.append(math.floor(value))
    start_s, duration = values[0] // 1000, values[1]
    if start_s + max(duration, 0) > 253_402_300_799:
        raise SyncError(
            f"Audiobookshelf session {session_id} exceeds the calendar range"
        )

    metadata = _object(fields.get("mediaMetadata")) or {}
    podcast = fields.get("mediaType") == "podcast"
    titles = (
        (metadata.get("title"),)
        if podcast
        else (fields.get("displayTitle"), metadata.get("title"))
    )
    title = next((t.strip() for t in titles if isinstance(t, str) and t.strip()), None)
    item_id = fields.get("libraryItemId")
    if title is None and isinstance(item_id, str) and item_id.strip():
        title = f"{'Podcast' if podcast else 'Audiobook'} {item_id.strip()}"
    if title is None:
        raise SyncError(
            f"Audiobookshelf session {session_id} has no usable title or libraryItemId"
        )
    # Only defer after checking the rest of the session for malformed values.
    if negative_time is not None:
        raise _NegativeListeningTime(session_id, user_id, negative_time)
    return ListeningSession(session_id, user_id, title, start_s, duration)


def _write(
    ledger: Ledger,
    config: Config,
    toggl: requests.Session,
    session: ListeningSession,
    entry_id: int | None,
    *,
    dry_run: bool,
) -> int | None:
    """Record intent, send once, and acknowledge; uncertain creates require repair."""
    body = session.payload() | {"workspace_id": config.workspace_id}
    url = f"{TOGGL_API}/workspaces/{config.workspace_id}/time_entries"
    if entry_id is None:
        body |= {
            "project_id": config.project_id,
            "created_with": "abs-sync",
            "billable": False,
        }
    else:
        url = f"{url}/{entry_id}"
    ledger.reserve(session.session_id, entry_id, _canonical(body), time.time())
    if dry_run:
        return entry_id  # The intent and budget only changed the in-memory database.

    rejected = False
    try:
        response = toggl.request(
            "POST" if entry_id is None else "PUT",
            url,
            json=body,
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        error = f"no response: {type(exc).__name__}"
    else:
        # Save server limits before acknowledging a write, even if that save later fails.
        cooldown = _cooldown_until(response, time.time())
        if cooldown is not None:
            ledger.extend_cooldown(cooldown)
        status = response.status_code
        if status in _QUOTA_STATUSES:
            if entry_id is None:
                ledger.discard_create(session.session_id)
            else:
                ledger.mark_failed(session.session_id, f"HTTP {status}", rejected=False)
            return None
        if 200 <= status < 300:
            received_id = _entry_id(response)
            if received_id is not None and entry_id in {None, received_id}:
                ledger.mark_synced(
                    session.session_id, received_id, _canonical(session.payload())
                )
                return received_id
            error = f"HTTP {status} without the expected entry id"
        else:
            error = f"HTTP {status}"
            rejected = status in _REJECTED_STATUSES
    ledger.mark_failed(session.session_id, error, rejected=rejected)
    consequence = (
        _PAUSED
        if entry_id is None or rejected
        else "the next sync resends the latest values"
    )
    raise SyncError(f"write for session {session.session_id}: {error}; {consequence}")


def _entry_id(response: requests.Response) -> int | None:
    try:
        payload = _object(cast(object, response.json())) or {}
    except ValueError:
        return None
    entry_id = payload.get("id")
    return entry_id if type(entry_id) is int and 0 < entry_id < 2**63 else None


def _cooldown_until(response: requests.Response, now: float) -> float | None:
    headers = response.headers
    try:
        remaining = int(headers.get("X-Toggl-Quota-Remaining", "1"))
    except ValueError:
        remaining = 1
    if response.status_code not in _QUOTA_STATUSES and remaining > 0:
        return None
    wait = _seconds(headers.get("X-Toggl-Quota-Resets-In")) or 3600
    retry = headers.get("Retry-After")
    seconds = _seconds(retry)
    if seconds is None and retry is not None:
        try:
            when = email.utils.parsedate_to_datetime(retry)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = _seconds(str(when.timestamp() - now))
        except (ValueError, TypeError, OverflowError):
            pass
    return now + max(wait, seconds or 0)


def _seconds(value: str | None) -> float | None:
    try:
        seconds = float(value) if value is not None else math.nan
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _object(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _rfc3339(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
