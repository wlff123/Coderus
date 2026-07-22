from __future__ import annotations

import sqlite3

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from coderus.config import DatabaseSettings
from coderus.models import CoderusSchema, Task, TaskTransition
from coderus.tasks.statuses import TERMINAL_TASK_STATES

ACTIVE_TASK_INDEX = "uq_active_task_per_issue"
CURRENT_SCHEMA_VERSION = 1


def create_engine_from_settings(settings: DatabaseSettings) -> Engine:
    path = settings.path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: sqlite3.Connection, _record: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def ensure_schema_compatibility(engine: Engine) -> None:
    active_task_index = next(
        index for index in Task.__table__.indexes if index.name == ACTIVE_TASK_INDEX
    )
    with engine.begin() as connection:
        CoderusSchema.__table__.create(connection, checkfirst=True)
        schema_version = connection.exec_driver_sql(
            "SELECT version FROM coderus_schema WHERE singleton=1"
        ).scalar_one_or_none()
        if schema_version is not None and schema_version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                "database schema is newer than this Coderus release"
            )

        tasks_exist = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).scalar_one_or_none()
        if tasks_exist is not None:
            task_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(tasks)")
            }
            additions = {
                "claim_token": "VARCHAR(100)",
                "claim_expires_at": "DATETIME",
                "publication_key": "VARCHAR(100)",
                "publication_started_at": "DATETIME",
                "contract_version": "INTEGER NOT NULL DEFAULT 1",
                "pr_status_error": "TEXT",
                "pr_status_checked_at": "DATETIME",
            }
            for name, sql_type in additions.items():
                if name not in task_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}"
                    )
            TaskTransition.__table__.create(connection, checkfirst=True)

        reviews_exist = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reviews'"
        ).scalar_one_or_none()
        if reviews_exist is not None:
            review_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(reviews)")
            }
            if "contract_version" not in review_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE reviews ADD COLUMN contract_version "
                    "INTEGER NOT NULL DEFAULT 1"
                )

        feishu_events_exist = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'feishu_events'"
        ).scalar_one_or_none()
        if feishu_events_exist is not None:
            event_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(feishu_events)")
            }
            event_additions = {
                "reply_text": "TEXT",
                "reply_status": "VARCHAR(30)",
                "reply_attempts": "INTEGER NOT NULL DEFAULT 0",
                "reply_error": "TEXT",
                "reply_next_attempt_at": "DATETIME",
                "reply_sent_at": "DATETIME",
            }
            for name, sql_type in event_additions.items():
                if name not in event_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE feishu_events ADD COLUMN {name} {sql_type}"
                    )

        table_exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pr_review_tasks'"
        ).scalar_one_or_none()
        if table_exists is not None:
            columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(pr_review_tasks)"
                )
            }
            if "claim_token" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE pr_review_tasks ADD COLUMN claim_token VARCHAR(100)"
                )
            if "claim_expires_at" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE pr_review_tasks ADD COLUMN claim_expires_at DATETIME"
                )
            if "review_key" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE pr_review_tasks ADD COLUMN review_key VARCHAR(100)"
                )

        existing_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (ACTIVE_TASK_INDEX,),
        ).scalar_one_or_none()
        if existing_sql is None or not all(
            f"'{status}'" in existing_sql for status in TERMINAL_TASK_STATES
        ):
            active_task_index.drop(connection, checkfirst=True)
            active_task_index.create(connection)
        connection.exec_driver_sql(
            "INSERT INTO coderus_schema (singleton, version) VALUES (1, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET version = excluded.version",
            (CURRENT_SCHEMA_VERSION,),
        )
