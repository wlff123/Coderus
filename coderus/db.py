from __future__ import annotations

import sqlite3

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from coderus.config import DatabaseSettings
from coderus.models import Task
from coderus.tasks.statuses import TERMINAL_TASK_STATES

ACTIVE_TASK_INDEX = "uq_active_task_per_issue"


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
        if existing_sql is not None and all(
            f"'{status}'" in existing_sql for status in TERMINAL_TASK_STATES
        ):
            return
        active_task_index.drop(connection, checkfirst=True)
        active_task_index.create(connection)
