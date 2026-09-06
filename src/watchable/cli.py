"""Command-line entry point: ``watchable``."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from watchable import __version__
from watchable.app import backup_dir_path, build_clients, configure_logging, database_path, open_database
from watchable.backup import create_backup, list_backups, purge_old_backups, restore_backup
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

    If `backup.interval_hours` is set, this loop also takes a database
    backup on that cadence -- independent of the sync interval -- and purges
    backups older than `backup.keep_days` afterwards.
    """
    config = _load_or_exit(config_path)
    configure_logging(config.log_level)
    interval = config.sync.schedule.interval_minutes
    if not interval:
        console.print("[red]sync.schedule.interval_minutes is not set in config[/red]")
        sys.exit(1)
    backup_interval_seconds = config.backup.interval_hours * 3600 if config.backup.interval_hours else None

    console.print(f"Running every {interval} minute(s). Press Ctrl+C to stop.")
    now = time.monotonic()
    next_sync_at = now
    next_backup_at = now if backup_interval_seconds else None

    while True:
        now = time.monotonic()
        if now >= next_sync_at:
            _run_once(config, dry_run=dry_run)
            next_sync_at = time.monotonic() + interval * 60
        if backup_interval_seconds is not None and next_backup_at is not None and now >= next_backup_at:
            _run_backup(config)
            next_backup_at = time.monotonic() + backup_interval_seconds

        wake_at = next_sync_at if next_backup_at is None else min(next_sync_at, next_backup_at)
        time.sleep(max(0.0, wake_at - time.monotonic()))


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


@main.group("backup")
def backup_group() -> None:
    """Create, list, restore, and purge database backups."""


@backup_group.command("create")
@click.argument("config_path", default="config.yaml")
def backup_create_cmd(config_path: str) -> None:
    """Take a backup of the database now."""
    config = _load_or_exit(config_path)
    _run_backup(config)


@backup_group.command("list")
@click.argument("config_path", default="config.yaml")
def backup_list_cmd(config_path: str) -> None:
    """List available backups, newest first."""
    config = _load_or_exit(config_path)
    backups = list_backups(backup_dir_path(config))
    if not backups:
        console.print("No backups found.")
        return
    table = Table("Created (UTC)", "Age", "Size", "File")
    now = datetime.now(timezone.utc)
    for backup in backups:
        table.add_row(
            backup.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            _format_age(now - backup.created_at),
            _format_size(backup.size_bytes),
            backup.path.name,
        )
    console.print(table)


@backup_group.command("restore")
@click.argument("config_path", default="config.yaml")
@click.argument("backup_name")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
def backup_restore_cmd(config_path: str, backup_name: str, yes: bool) -> None:
    """Restore the database from a backup, overwriting the current one.

    BACKUP_NAME is a filename from `watchable backup list`, or a path to a
    backup file. Make sure no `sync`/`run` is running concurrently first.
    """
    config = _load_or_exit(config_path)
    backup_path = Path(backup_name)
    if not backup_path.is_file():
        backup_path = backup_dir_path(config) / backup_name
    if not backup_path.is_file():
        console.print(f"[red]Backup not found:[/red] {backup_name}")
        sys.exit(1)

    db_path = database_path(config)
    if not yes and not click.confirm(f"This overwrites {db_path} with {backup_path}. Continue?"):
        console.print("Aborted.")
        return

    restore_backup(backup_path, db_path)
    console.print(f"[green]Restored[/green] {db_path} from {backup_path}")


@backup_group.command("purge")
@click.argument("config_path", default="config.yaml")
@click.option("--older-than-days", type=float, default=None, help="Override backup.keep_days for this run")
def backup_purge_cmd(config_path: str, older_than_days: float | None) -> None:
    """Delete backups older than backup.keep_days (or --older-than-days)."""
    config = _load_or_exit(config_path)
    keep_days = older_than_days if older_than_days is not None else config.backup.keep_days
    if keep_days is None:
        console.print("[red]No backup.keep_days configured and --older-than-days not given[/red]")
        sys.exit(1)
    removed = purge_old_backups(backup_dir_path(config), keep_days)
    console.print(f"Purged {len(removed)} backup(s) older than {keep_days} day(s).")


def _run_backup(config: AppConfig) -> None:
    backup_path = create_backup(database_path(config), backup_dir_path(config))
    console.print(f"Backed up database to {backup_path}")
    if config.backup.keep_days is not None:
        removed = purge_old_backups(backup_dir_path(config), config.backup.keep_days)
        if removed:
            console.print(f"Purged {len(removed)} backup(s) older than {config.backup.keep_days} day(s)")


def _format_age(age: timedelta) -> str:
    total_seconds = int(age.total_seconds())
    if total_seconds < 3600:
        return f"{total_seconds // 60}m"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h"
    return f"{total_seconds // 86400}d"


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


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
