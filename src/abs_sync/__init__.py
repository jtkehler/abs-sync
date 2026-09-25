"""abs-sync: Audiobookshelf listening sessions as Toggl Track time entries."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from filelock import Timeout

from .sync import ConfigError, SyncError, load_config, run_sync

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def _run(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            dir_okay=False,
            show_default=False,
            help="TOML file (default: user config directory/config.toml).",
        ),
    ] = None,
    since: Annotated[
        datetime | None,
        typer.Option(
            formats=["%Y-%m-%d"],
            metavar="YYYY-MM-DD",
            show_default=False,
            help="Import from this UTC date; required on the first run.",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview without Toggl writes or files.")
    ] = False,
) -> None:
    """Create or update one Toggl entry per Audiobookshelf listening session."""
    try:
        run_sync(
            load_config(config),
            None if since is None else since.date(),
            dry_run=dry_run,
            log=typer.echo,
        )
    except (ConfigError, Timeout, SyncError, sqlite3.Error, OSError) as exc:
        code = (
            2 if isinstance(exc, ConfigError) else 75 if isinstance(exc, Timeout) else 1
        )
        message = (
            f"another abs-sync process holds {exc.lock_file}"
            if isinstance(exc, Timeout)
            else str(exc)
        )
        typer.echo(f"error: {message}", err=True)
        raise typer.Exit(code) from None


def main() -> None:
    app()
