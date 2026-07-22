from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from coderus.release_ops import (
    active_work_counts,
    backup_sqlite,
    check_schema_compatibility,
    migrate_database,
    prune_release_artifacts,
    write_release_history,
)


def test_backup_sqlite_copies_committed_wal_data(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('committed')")
        connection.commit()

        backup_sqlite(source, destination)

    with sqlite3.connect(destination) as backup:
        assert backup.execute("SELECT value FROM sample").fetchone() == ("committed",)


def test_failed_backup_keeps_existing_destination_intact(tmp_path: Path) -> None:
    source = tmp_path / "corrupt.db"
    destination = tmp_path / "production.db"
    source.write_bytes(b"not a sqlite database")
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('original')")
        connection.commit()

    with pytest.raises(sqlite3.DatabaseError):
        backup_sqlite(source, destination)

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == ("original",)
    assert list(tmp_path.glob(".production.db.*.tmp")) == []


def test_active_work_counts_covers_agents_reviews_and_repository_sync(
    tmp_path: Path,
) -> None:
    database = tmp_path / "coderus.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (id INTEGER, status TEXT);
            CREATE TABLE pr_review_tasks (id INTEGER, status TEXT);
            CREATE TABLE repositories (id INTEGER, sync_status TEXT);
            CREATE TABLE agent_runs (id INTEGER, status TEXT);
            CREATE TABLE feishu_events (id INTEGER, status TEXT);
            INSERT INTO tasks VALUES (1, 'developer_working'), (2, 'queued');
            INSERT INTO pr_review_tasks VALUES (1, 'reviewing'), (2, 'completed');
            INSERT INTO repositories VALUES (1, 'running'), (2, 'idle');
            INSERT INTO agent_runs VALUES (1, 'running'), (2, 'completed');
            INSERT INTO feishu_events VALUES (1, 'processing'), (2, 'processed');
            """
        )

    assert active_work_counts(database) == {
        "issue_tasks": 1,
        "pr_reviews": 1,
        "repository_syncs": 1,
        "agent_runs": 1,
        "feishu_commands": 1,
    }


def test_schema_compatibility_rejects_rollback_to_older_contract(tmp_path: Path) -> None:
    database = tmp_path / "coderus.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE coderus_schema (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO coderus_schema VALUES (1, 2)")
    release = tmp_path / "release.json"
    release.write_text(
        json.dumps({"min_schema_version": 1, "max_schema_version": 1}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema version 2"):
        check_schema_compatibility(database, release)


def test_schema_compatibility_accepts_explicit_bootstrap_release(tmp_path: Path) -> None:
    database = tmp_path / "coderus.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER)")
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"bootstrap": True}), encoding="utf-8")

    assert check_schema_compatibility(database, release) == 1


def test_migrate_database_creates_the_current_schema(tmp_path: Path) -> None:
    database = tmp_path / "coderus.db"

    migrate_database(database)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = connection.execute(
            "SELECT version FROM coderus_schema WHERE singleton=1"
        ).fetchone()
    assert {"tasks", "pr_review_tasks", "feishu_events"}.issubset(tables)
    assert version == (1,)


def test_release_history_is_atomic_and_prunes_old_entries(tmp_path: Path) -> None:
    history = tmp_path / "history"
    for index in range(4):
        write_release_history(
            history,
            {
                "release_id": f"20260721-00000{index}-deadbeef",
                "previous_id": "20260720-000000-cafebabe",
            },
            retain=2,
        )

    entries = sorted(history.glob("*.json"))
    assert [path.stem for path in entries] == [
        "20260721-000002-deadbeef",
        "20260721-000003-deadbeef",
    ]
    assert list(history.glob("*.tmp")) == []


def test_release_artifact_retention_preserves_protected_versions(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    backups = tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    release_ids = [f"2026072{day}-000000-deadbee{day}" for day in range(1, 6)]
    for release_id in release_ids:
        (releases / release_id).mkdir()
    for index in range(5):
        (backups / f"2026072{index}-before-release.db").write_bytes(b"db")

    prune_release_artifacts(
        tmp_path,
        protected_release_ids={release_ids[0]},
        retain_releases=2,
        retain_backups=2,
    )

    assert {path.name for path in releases.iterdir()} == {
        release_ids[0],
        release_ids[-2],
        release_ids[-1],
    }
    assert [path.name for path in sorted(backups.iterdir())] == [
        "20260723-before-release.db",
        "20260724-before-release.db",
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions are required")
def test_release_artifact_retention_removes_readonly_versions(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    stale = releases / "20260721-000000-deadbeef"
    nested = stale / "nested"
    current = releases / "20260722-000000-cafebabe"
    nested.mkdir(parents=True)
    current.mkdir()
    payload = nested / "payload.txt"
    payload.write_text("release", encoding="utf-8")
    payload.chmod(0o400)
    nested.chmod(0o500)
    stale.chmod(0o500)

    try:
        prune_release_artifacts(
            tmp_path,
            retain_releases=1,
            retain_backups=1,
        )
    finally:
        for path in (stale, nested, payload):
            if path.exists():
                path.chmod(0o700)

    assert not stale.exists()
    assert current.exists()
