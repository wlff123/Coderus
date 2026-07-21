from __future__ import annotations

import argparse
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Coderus release operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("source", type=Path)
    backup.add_argument("destination", type=Path)
    idle = subparsers.add_parser("check-idle")
    idle.add_argument("database", type=Path)
    args = parser.parse_args()
    if args.command == "backup":
        backup_sqlite(args.source, args.destination)
        return
    counts = active_work_counts(args.database)
    print(json.dumps(counts, sort_keys=True))
    if any(counts.values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
