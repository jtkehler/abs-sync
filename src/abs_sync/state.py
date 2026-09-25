"""SQLite session mappings, pending writes, and the shared request budget."""

import sqlite3
from collections.abc import Generator, Mapping
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from typing import NamedTuple, cast

from filelock import FileLock

WINDOW_SECONDS = 3600.0
SPACING_SECONDS = 1.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    toggl_entry_id INTEGER UNIQUE,
    status TEXT NOT NULL,
    last_payload_json TEXT,
    intent_json TEXT,
    error TEXT,
    CHECK (
        (status = 'creating' AND toggl_entry_id IS NULL)
        OR (status IN ('synced', 'updating') AND toggl_entry_id IS NOT NULL)
        OR status = 'blocked'
    )
);
CREATE TABLE IF NOT EXISTS request_attempts (at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS request_attempts_at ON request_attempts (at);
"""


class Row(NamedTuple):
    session_id: str
    toggl_entry_id: int | None
    status: str
    last_payload_json: str | None
    intent_json: str | None
    error: str | None


class Budget(NamedTuple):
    available: int
    next_available: float
    last_attempt: float | None


@contextmanager
def open_ledger(path: Path, *, dry_run: bool = False) -> Generator["Ledger"]:
    """Lock real runs; give previews a private, consistent copy of the database."""
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(f"{path}.lock", timeout=0, mode=0o600)
    with (
        nullcontext() if dry_run else lock,
        closing(sqlite3.connect(":memory:" if dry_run else path)) as conn,
    ):
        if dry_run and path.exists():
            uri = f"{path.absolute().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as source:
                source.backup(conn)
        _ = conn.executescript(_SCHEMA)
        yield Ledger(conn)


class Ledger:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn: sqlite3.Connection = conn

    def meta(self) -> dict[str, str]:
        return dict(
            cast(
                list[tuple[str, str]],
                self._conn.execute("SELECT key, value FROM meta").fetchall(),
            )
        )

    def rows(self) -> dict[str, Row]:
        records = cast(
            list[tuple[str, int | None, str, str | None, str | None, str | None]],
            self._conn.execute(
                "SELECT session_id, toggl_entry_id, status, last_payload_json, "
                + "intent_json, error FROM sessions"
            ).fetchall(),
        )
        return {record[0]: Row(*record) for record in records}

    def set_meta(self, values: Mapping[str, str]) -> None:
        with self._conn:
            _ = self._conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                + "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                values.items(),
            )

    def budget(self, now: float, cap: int) -> Budget:
        records = cast(
            list[tuple[float]],
            self._conn.execute(
                "SELECT at FROM request_attempts ORDER BY at"
            ).fetchall(),
        )
        recent = [at for (at,) in records if at > now - WINDOW_SECONDS]
        freed_at = recent[-cap] + WINDOW_SECONDS if len(recent) >= cap else now
        next_available = max(now, freed_at, float(self.meta().get("blocked_until", 0)))
        available = 0 if next_available > now else cap - len(recent)
        return Budget(available, next_available, records[-1][0] if records else None)

    def extend_cooldown(self, until: float) -> None:
        if until > float(self.meta().get("blocked_until", 0)):
            self.set_meta({"blocked_until": str(until)})

    def reserve(
        self, session_id: str, entry_id: int | None, intent_json: str, at: float
    ) -> None:
        """Commit the request attempt and intent together before sending anything."""
        with self._conn:
            _ = self._conn.execute("INSERT INTO request_attempts VALUES (?)", (at,))
            _ = self._conn.execute(
                "DELETE FROM request_attempts WHERE at <= ?", (at - WINDOW_SECONDS,)
            )
            _ = self._conn.execute(
                """
                INSERT INTO sessions (session_id, toggl_entry_id, status, intent_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (session_id) DO UPDATE SET
                    status = excluded.status, intent_json = excluded.intent_json,
                    error = NULL
                """,
                (
                    session_id,
                    entry_id,
                    "creating" if entry_id is None else "updating",
                    intent_json,
                ),
            )

    def mark_synced(self, session_id: str, entry_id: int, payload_json: str) -> None:
        with self._conn:
            _ = self._conn.execute(
                """
                UPDATE sessions SET toggl_entry_id = ?, status = 'synced',
                    last_payload_json = ?, intent_json = NULL, error = NULL
                WHERE session_id = ?
                """,
                (entry_id, payload_json, session_id),
            )

    def mark_failed(self, session_id: str, error: str, *, rejected: bool) -> None:
        with self._conn:
            _ = self._conn.execute(
                "UPDATE sessions SET error = ?, "
                + "status = CASE WHEN ? THEN 'blocked' ELSE status END WHERE session_id = ?",
                (error, rejected, session_id),
            )

    def discard_create(self, session_id: str) -> None:
        """A quota rejection is safe to retry; keep its spent request in the budget."""
        with self._conn:
            _ = self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ? "
                + "AND status = 'creating' AND toggl_entry_id IS NULL",
                (session_id,),
            )
