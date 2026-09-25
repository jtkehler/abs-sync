# abs-sync

Syncs Audiobookshelf listening sessions to Toggl Track: one completed time entry
per audiobook or podcast listening session, in one existing Toggl project.
Audiobookshelf owns each entry's description (the book name for audiobooks,
the series name for podcasts), start, stop and duration. When a session grows
or is corrected, the next run updates the same entry; unchanged sessions cost
no Toggl requests.

An entry starts when the session started and lasts its listening time, so pauses
are compressed out. Podcast episode titles are not used. Entries are never deleted.

## Install

```sh
uv tool install .
command -v abs-sync   # absolute path for cron
```

## Configure

Create `config.toml` in the per-user config directory selected by
[platformdirs](https://platformdirs.readthedocs.io/en/latest/platforms.html#user-config-dir),
or specify a file with `--config PATH`:

| Platform | Config directory | State directory |
| --- | --- | --- |
| Linux | `~/.config/abs-sync` | `~/.local/state/abs-sync` |
| macOS | `~/Library/Application Support/abs-sync` | Same as config |
| Windows | `%LOCALAPPDATA%\abs-sync` | Same as config |

On Linux, `XDG_CONFIG_HOME` and `XDG_STATE_HOME` override the respective base
directories. Directory discovery does not create files. Keep the config private
to your user; on Linux/macOS, use `chmod 600`.

Process locking uses [filelock](https://py-filelock.readthedocs.io/en/latest/).
Runtime checks currently cover Linux; native Windows execution is unverified.

```toml
[audiobookshelf]
url = "https://abs.example.com"   # reverse-proxy path prefixes are kept
api_key = "USER_BOUND_ABS_API_KEY" # API key bound to the listening user

[toggl]
api_token = "TOGGL_API_TOKEN"
workspace_id = 123
project_id = 456

[sync]
requests_per_hour = 25            # 1-30; shared by every run
min_entry_duration_seconds = 0    # minimum for new entries; 0 disables the cutoff
# state_path = "/absolute/path/to/state.sqlite3"
```

Configuration uses Python's TOML parser and the layout above. Supply valid URLs,
credentials and integer workspace/project IDs; there is no configuration schema
validator. The request cap and minimum duration are checked locally.

`sync.min_entry_duration_seconds` is a non-negative integer, defaulting to `0`.
New entries shorter than this minimum are skipped; entries exactly at the
minimum are included. The comparison uses listening time rounded down to whole
seconds, not elapsed session time. New zero-length entries are always skipped.
Skipped sessions are reconsidered each run, so they can be imported after growing
or lowering the minimum. Already-mapped entries still update below the minimum;
nothing is deleted. Dry runs apply the same cutoff and report skipped entries.

State is stored as `state.sqlite3` in the state directory above. An explicit
`sync.state_path` overrides it; relative paths are resolved against the config
file's directory. The database maps sessions to Toggl entries and records every
Toggl request. Back it up with the config: importing history again without it
creates duplicate entries. Run abs-sync from one host only.

When upgrading an existing installation, retain its database. If its path
differs from the platform default, set `sync.state_path` to the old location.

## Run

The first run needs an import boundary, a UTC date selecting sessions by start:

```sh
abs-sync --since 2026-01-01 --dry-run   # preview; no Toggl requests or files
abs-sync --since 2026-01-01
```

Real runs save the boundary, so later runs need no flag. An earlier `--since`
widens it permanently; a later one is rejected. Sessions that are already mapped
keep being updated regardless of the boundary.

Every run scans the full Audiobookshelf history. At most `requests_per_hour`
Toggl requests are sent per rolling hour across all runs; remaining work waits
for later runs, updates before creates. A 402 or 429 from Toggl stops the run
and sets a cooldown.

Dry runs use an in-memory copy of the existing database, so they share the real
comparison and request-budget logic without changing saved state. Output lists
creates/updates and a short count of unchanged, skipped and deferred sessions.

Schedule it hourly with an absolute path:

```cron
15 * * * * /home/you/.local/bin/abs-sync
```

## Recovery

An update to a known entry whose response was lost is resent automatically by
the next run. An unknown create outcome or an explicit non-quota rejection
pauses all Toggl writes instead: retrying an uncertain create could duplicate
the entry, while repeatedly retrying a rejection wastes quota.

There is no recovery command or recovery flag. To repair a paused session:

1. Disable scheduling and wait for all abs-sync processes to exit.
2. Back up the state database, inspect the reported session in Toggl, and fix the
   cause of any rejection. **Do not delete the database or reset quota metadata.**
3. Open the database in a SQLite client and apply **one** of the following
   repairs for the affected session. Replace `SESSION_ID` and `123456` with the
   actual source session and verified Toggl entry ID.

If the intended entry exists, link it and force its next update. For a rejected
update, keep its existing ID after fixing the cause; a deleted entry needs an
explicitly created replacement, not an automatic retry as a new entry.

```sql
UPDATE sessions
SET toggl_entry_id = 123456, status = 'updating', last_payload_json = NULL,
    intent_json = NULL, error = NULL
WHERE session_id = 'SESSION_ID' AND status IN ('creating', 'blocked');
```

Only if you have verified that a create produced **no entry**, remove that
unmapped intent so the next run can create it. A failed list search alone does
not prove non-creation. The guard below preserves existing mappings.

```sql
DELETE FROM sessions
WHERE session_id = 'SESSION_ID'
  AND status IN ('creating', 'blocked') AND toggl_entry_id IS NULL;
```

Confirm exactly one row was changed, commit the repair, then run `abs-sync` and
resume scheduling. Request history and cooldowns remain in force.

Edits made only in Toggl to a synced entry's description, start, stop or
duration are overwritten the next time its Audiobookshelf session changes.

## Exit status

| Status | Meaning |
| --- | --- |
| 0 | Success, nothing to do, or work deferred by the request budget |
| 1 | Audiobookshelf, network, data or state error; unresolved or rejected writes |
| 2 | Invalid command line or configuration |
| 75 | Another abs-sync run holds the state lock |

## Development

```sh
uv run python -m unittest discover -s tests -v
uv run basedpyright src tests
uv run ruff check src tests
```
