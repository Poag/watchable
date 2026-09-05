"""Command-line entry point: ``watchable``."""

from __future__ import annotations

import sys
import time

import click
from rich.console import Console
from rich.table import Table

from watchable import __version__
from watchable.app import build_clients, configure_logging, open_database
from watchable.config import AppConfig, ConfigError, load_config
from watchable.providers.base import ProviderError
from watchable.sync import SyncEngine

console = Console()


@click.group()
@click.version_option(__version__, prog_name="watchable")
def main() -> None:
    """Sync watch state between Plex, Jellyfin, and Emby via a local database."""


@main.command("validate")
@click.argument("config_path", default="config.yaml")
def validate_cmd(config_path: str) -> None:
    """Validate a config file without connecting to any server."""
    config = _load_or_exit(config_path)
    console.print(
        f"[green]OK[/green] {len(config.servers)} server(s), {len(config.users)} user(s), "
        f"{len(config.sync.pairs)} sync pair(s)."
    )
    for pair in config.sync.pairs:
        console.print(f"  - {pair.name}: {pair.source} -> {pair.target} ({pair.direction.value})")


@main.command("test-connections")
@click.argument("config_path", default="config.yaml")
def test_connections_cmd(config_path: str) -> None:
    """Check that every configured server is reachable and authenticates."""
    config = _load_or_exit(config_path)
    clients = build_clients(config)
    table = Table("Server", "Type", "Status")
    ok = True
    for key, client in clients.items():
        try:
            client.test_connection()
        except ProviderError as exc:
            table.add_row(key, client.server_type, f"[red]FAILED[/red] {exc}")
            ok = False
        else:
            table.add_row(key, client.server_type, "[green]OK[/green]")
    console.print(table)
    if not ok:
        sys.exit(1)


@main.command("sync")
@click.argument("config_path", default="config.yaml")
@click.option("--dry-run", is_flag=True, help="Compute and log what would change without pushing anything")
def sync_cmd(config_path: str, dry_run: bool) -> None:
    """Run a single sync pass."""
    config = _load_or_exit(config_path)
    configure_logging(config.log_level)
    _run_once(config, dry_run=dry_run)


@main.command("run")
@click.argument("config_path", default="config.yaml")
@click.option("--dry-run", is_flag=True, help="Compute and log what would change without pushing anything")
def run_cmd(config_path: str, dry_run: bool) -> None:
    """Run sync passes forever on sync.schedule.interval_minutes.

    For most home-lab setups a system timer or cron job calling `watchable
    sync` on a schedule is simpler to operate than this; `run` exists for the
    container deployment in docker/, where a long-lived process is the norm.
    """
    config = _load_or_exit(config_path)
    configure_logging(config.log_level)
    interval = config.sync.schedule.interval_minutes
    if not interval:
        console.print("[red]sync.schedule.interval_minutes is not set in config[/red]")
        sys.exit(1)
    console.print(f"Running every {interval} minute(s). Press Ctrl+C to stop.")
    while True:
        _run_once(config, dry_run=dry_run)
        time.sleep(interval * 60)


@main.command("log")
@click.argument("config_path", default="config.yaml")
@click.option("--limit", default=25, show_default=True)
def log_cmd(config_path: str, limit: int) -> None:
    """Show the most recent sync actions recorded in the local database."""
    config = _load_or_exit(config_path)
    with open_database(config) as db:
        table = Table("Time", "Pair", "Title", "User", "From", "To", "Action", "Detail")
        for row in db.recent_log(limit):
            table.add_row(
                row["ts"],
                row["pair_name"],
                row["title"] or "",
                row["user_name"],
                row["source_server"],
                row["target_server"],
                row["action"],
                row["detail"] or "",
            )
    console.print(table)


def _load_or_exit(config_path: str) -> AppConfig:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        sys.exit(1)


def _run_once(config: AppConfig, *, dry_run: bool) -> None:
    clients = build_clients(config)
    with open_database(config) as db:
        engine = SyncEngine(config, db, clients)
        stats = engine.run(dry_run=dry_run)
    console.print(
        f"pulled={stats.pulled} pushed={stats.pushed} dry_run_pushes={stats.dry_run_pushes} "
        f"unchanged={stats.unchanged} skipped_no_match={stats.skipped_no_match} errors={stats.errors}"
    )
    if stats.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
