from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coderus.release_ops import active_work_counts, backup_sqlite


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
