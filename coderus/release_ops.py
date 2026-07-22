from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from contextlib import closing
from pathlib import Path

from coderus.config import DatabaseSettings
from coderus.db import create_engine_from_settings, ensure_schema_compatibility
from coderus.models import Base

ISSUE_RUNNING_STATUSES = (
    "preparing",
    "developer_working",
    "reviewing",
    "developer_revising",
    "sealing",
    "publishing",
    "cancelling",
)
PR_REVIEW_RUNNING_STATUSES = ("preparing", "reviewing", "commenting")
DEFAULT_SCHEMA_VERSION = 1
RELEASE_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


def backup_sqlite(source: Path, destination: Path) -> None:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"database does not exist: {source}")
    if source == destination:
        raise ValueError("source and destination database must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    source_uri = f"{source.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=10)) as source_db:
            with closing(sqlite3.connect(temporary)) as destination_db:
                source_db.backup(destination_db)
                result = destination_db.execute("PRAGMA integrity_check").fetchone()
                if result != ("ok",):
                    raise sqlite3.DatabaseError("backup integrity check failed")
        with temporary.open("r+b") as backup_file:
            os.fsync(backup_file.fileno())
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_database(database: Path) -> None:
    engine = create_engine_from_settings(DatabaseSettings(path=database))
    try:
        Base.metadata.create_all(engine)
        ensure_schema_compatibility(engine)
    finally:
        engine.dispose()


def _count_statuses(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    statuses: tuple[str, ...],
) -> int:
    placeholders = ",".join("?" for _ in statuses)
    row = connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})",  # noqa: S608
        statuses,
    ).fetchone()
    return int(row[0])


def active_work_counts(database: Path) -> dict[str, int]:
    database = database.expanduser().resolve()
    uri = f"{database.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        return {
            "issue_tasks": _count_statuses(
                connection, "tasks", "status", ISSUE_RUNNING_STATUSES
            ),
            "pr_reviews": _count_statuses(
                connection,
                "pr_review_tasks",
                "status",
                PR_REVIEW_RUNNING_STATUSES,
            ),
            "repository_syncs": _count_statuses(
                connection, "repositories", "sync_status", ("running",)
            ),
            "agent_runs": _count_statuses(
                connection, "agent_runs", "status", ("running",)
            ),
            "feishu_commands": _count_statuses(
                connection, "feishu_events", "status", ("processing",)
            ),
        }


def check_schema_compatibility(database: Path, release_manifest: Path) -> int:
    database = database.expanduser().resolve()
    release_manifest = release_manifest.expanduser().resolve()
    try:
        manifest = json.loads(release_manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("invalid release schema contract") from exc
    if not isinstance(manifest, dict):
        raise ValueError("invalid release schema contract")
    if manifest.get("bootstrap") is True:
        minimum = maximum = DEFAULT_SCHEMA_VERSION
    else:
        try:
            minimum = manifest["min_schema_version"]
            maximum = manifest["max_schema_version"]
        except KeyError as exc:
            raise ValueError("invalid release schema contract") from exc
    if (
        not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or minimum < 1
        or maximum < minimum
    ):
        raise ValueError("invalid release schema contract")

    uri = f"{database.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coderus_schema'"
        ).fetchone()
        if table is None:
            current = DEFAULT_SCHEMA_VERSION
        else:
            row = connection.execute(
                "SELECT version FROM coderus_schema WHERE singleton=1"
            ).fetchone()
            if row is None or not isinstance(row[0], int):
                raise ValueError("database schema version is invalid")
            current = row[0]
    if not minimum <= current <= maximum:
        raise ValueError(
            f"database schema version {current} is outside supported range "
            f"{minimum}..{maximum}"
        )
    return current


def write_release_history(
    directory: Path,
    payload: dict[str, object],
    *,
    retain: int = 20,
) -> Path:
    if retain < 1:
        raise ValueError("release history retention must be positive")
    release_id = payload.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise ValueError("release history requires a release id")
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{release_id}.json"
    temporary = directory / f".{release_id}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)

    entries = sorted(
        (path for path in directory.glob("*.json") if path.is_file()),
        key=lambda path: path.name,
    )
    for stale in entries[:-retain]:
        stale.unlink()
    return destination


def _remove_release_tree(path: Path) -> None:
    entries = [path, *path.rglob("*")]
    for entry in entries:
        if entry.is_symlink():
            continue
        mode = entry.stat().st_mode | stat.S_IWUSR
        if entry.is_dir():
            mode |= stat.S_IXUSR
        entry.chmod(mode)
    shutil.rmtree(path)


def prune_release_artifacts(
    root: Path,
    *,
    protected_release_ids: set[str] | None = None,
    retain_releases: int = 5,
    retain_backups: int = 20,
) -> None:
    if retain_releases < 1 or retain_backups < 1:
        raise ValueError("release retention values must be positive")
    root = root.expanduser().resolve()
    releases = root / "releases"
    backups = root / "backups"
    protected = set(protected_release_ids or ())
    for pointer_name in ("current", "previous"):
        pointer = root / pointer_name
        if not pointer.is_symlink():
            continue
        target = pointer.resolve(strict=False)
        if target.parent == releases and RELEASE_ID_PATTERN.fullmatch(target.name):
            protected.add(target.name)

    candidates = sorted(
        (
            path
            for path in releases.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and RELEASE_ID_PATTERN.fullmatch(path.name)
        ),
        key=lambda path: path.name,
    ) if releases.is_dir() else []
    keep = {path.name for path in candidates[-retain_releases:]} | protected
    for stale in candidates:
        if stale.name not in keep:
            _remove_release_tree(stale)

    backup_files = sorted(
        (
            path
            for path in backups.glob("*.db")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.name,
    ) if backups.is_dir() else []
    for stale in backup_files[:-retain_backups]:
        stale.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Coderus release operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("source", type=Path)
    backup.add_argument("destination", type=Path)
    migrate = subparsers.add_parser("migrate")
    migrate.add_argument("database", type=Path)
    idle = subparsers.add_parser("check-idle")
    idle.add_argument("database", type=Path)
    schema = subparsers.add_parser("check-schema")
    schema.add_argument("database", type=Path)
    schema.add_argument("release_manifest", type=Path)
    history = subparsers.add_parser("write-history")
    history.add_argument("directory", type=Path)
    history.add_argument("payload")
    history.add_argument("--retain", type=int, default=20)
    prune = subparsers.add_parser("prune-artifacts")
    prune.add_argument("root", type=Path)
    prune.add_argument("--retain-releases", type=int, default=5)
    prune.add_argument("--retain-backups", type=int, default=20)
    args = parser.parse_args()
    if args.command == "backup":
        backup_sqlite(args.source, args.destination)
        return
    if args.command == "migrate":
        migrate_database(args.database)
        return
    if args.command == "check-schema":
        print(check_schema_compatibility(args.database, args.release_manifest))
        return
    if args.command == "write-history":
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError("release history payload must be an object")
        print(write_release_history(args.directory, payload, retain=args.retain))
        return
    if args.command == "prune-artifacts":
        prune_release_artifacts(
            args.root,
            retain_releases=args.retain_releases,
            retain_backups=args.retain_backups,
        )
        return
    counts = active_work_counts(args.database)
    print(json.dumps(counts, sort_keys=True))
    if any(counts.values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
