import pytest
from sqlalchemy import inspect

from coderus.db import ensure_schema_compatibility
from coderus.models import (
    Base,
    CoderusSchema,
    FeishuEvent,
    IntegrationCredential,
    Issue,
    PRReviewTask,
    Repository,
    Task,
    TaskTransition,
    User,
)


def test_sqlite_enables_required_pragmas(engine) -> None:
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000


def test_core_tables_exist(engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert {"users", "repositories", "issues", "tasks"} <= tables
    assert Base.metadata.tables["users"] is User.__table__
    assert Repository.__tablename__ == "repositories"
    assert Issue.__tablename__ == "issues"
    assert Task.__tablename__ == "tasks"


def test_integration_credentials_table_is_registered(engine) -> None:
    tables = set(inspect(engine).get_table_names())
    table = Base.metadata.tables["integration_credentials"]

    assert "integration_credentials" in tables
    assert table is IntegrationCredential.__table__
    assert table.c.provider.unique


def test_issue_triage_defaults_to_discovered(session) -> None:
    user = User(username="admin", password_hash="hash", role="admin")
    repository = Repository(
        provider="github",
        owner="octo",
        name="demo",
        canonical_url="https://github.com/octo/demo",
        default_branch="main",
        created_by_user=user,
    )
    issue = Issue(
        repository=repository,
        external_id="123",
        number=1,
        title="Broken build",
        state="open",
    )
    session.add(issue)
    session.commit()

    assert issue.triage_state == "discovered"


def test_schema_compatibility_updates_legacy_active_task_index(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_active_task_per_issue")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_active_task_per_issue ON tasks (issue_id) "
            "WHERE status NOT IN ('completed', 'failed', 'cancelled', 'manual_intervention')"
        )

    ensure_schema_compatibility(engine)

    with engine.connect() as connection:
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'uq_active_task_per_issue'"
        ).scalar_one()
    assert "'closed'" in sql


def test_schema_compatibility_adds_pr_review_lease_columns_to_legacy_sqlite(
    engine,
) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE pr_review_tasks")
        connection.exec_driver_sql(
            "CREATE TABLE pr_review_tasks (id INTEGER PRIMARY KEY, status VARCHAR(30))"
        )
        connection.exec_driver_sql(
            "INSERT INTO pr_review_tasks (id, status) VALUES (1, 'queued')"
        )

    ensure_schema_compatibility(engine)
    ensure_schema_compatibility(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("pr_review_tasks")}
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT status, claim_token, claim_expires_at, review_key "
            "FROM pr_review_tasks WHERE id = 1"
        ).one()
    assert {"claim_token", "claim_expires_at", "review_key"} <= columns
    assert row == ("queued", None, None, None)
    assert PRReviewTask.__table__.c.claim_token.nullable
    assert PRReviewTask.__table__.c.claim_expires_at.nullable
    assert PRReviewTask.__table__.c.review_key.nullable


def test_schema_compatibility_adds_feishu_reply_outbox_to_legacy_sqlite(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE feishu_events")
        connection.exec_driver_sql(
            "CREATE TABLE feishu_events ("
            "id INTEGER PRIMARY KEY, message_id VARCHAR(255) NOT NULL, "
            "event_id VARCHAR(255) NOT NULL, chat_id VARCHAR(255) NOT NULL, "
            "chat_type VARCHAR(30) NOT NULL, sender_open_id VARCHAR(255) NOT NULL, "
            "command TEXT NOT NULL, status VARCHAR(30) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO feishu_events "
            "(id, message_id, event_id, chat_id, chat_type, sender_open_id, command, status) "
            "VALUES (1, 'm1', 'e1', 'c1', 'group', 'u1', '状态', 'completed')"
        )

    ensure_schema_compatibility(engine)
    ensure_schema_compatibility(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("feishu_events")}
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT reply_text, reply_status, reply_attempts, reply_error, "
            "reply_next_attempt_at, reply_sent_at "
            "FROM feishu_events WHERE id = 1"
        ).one()
    assert {
        "reply_text",
        "reply_status",
        "reply_attempts",
        "reply_error",
        "reply_next_attempt_at",
        "reply_sent_at",
    } <= columns
    assert row == (None, None, 0, None, None, None)
    assert FeishuEvent.__table__.c.reply_attempts.default.arg == 0


def test_schema_compatibility_adds_issue_task_reliability_fields(tmp_path) -> None:
    from coderus.config import DatabaseSettings
    from coderus.db import create_engine_from_settings

    legacy_engine = create_engine_from_settings(
        DatabaseSettings(path=tmp_path / "legacy-task.db")
    )
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE coderus_schema ("
            "singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO coderus_schema (singleton, version) VALUES (1, 0)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE tasks ("
            "id INTEGER PRIMARY KEY, issue_id INTEGER, status VARCHAR(50) NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_active_task_per_issue ON tasks (issue_id) "
            "WHERE status NOT IN "
            "('completed', 'closed', 'dismissed', 'failed', 'cancelled', "
            "'manual_intervention')"
        )
        connection.exec_driver_sql(
            "INSERT INTO tasks (id, issue_id, status) VALUES (1, 1, 'queued')"
        )

    ensure_schema_compatibility(legacy_engine)
    ensure_schema_compatibility(legacy_engine)

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("tasks")}
    tables = set(inspect(legacy_engine).get_table_names())
    with legacy_engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT claim_token, claim_expires_at, publication_key, "
            "publication_started_at, contract_version, pr_status_error, "
            "pr_status_checked_at FROM tasks WHERE id = 1"
        ).one()
        schema_row = connection.exec_driver_sql(
            "SELECT singleton, version FROM coderus_schema"
        ).one()
    assert {
        "claim_token",
        "claim_expires_at",
        "publication_key",
        "publication_started_at",
        "contract_version",
        "pr_status_error",
        "pr_status_checked_at",
    } <= columns
    assert "task_transitions" in tables
    assert row == (None, None, None, None, 1, None, None)
    assert TaskTransition.__table__.c.contract_version.default.arg == 1
    assert schema_row == (1, 1)
    assert CoderusSchema.__table__.c.version.default.arg == 1
    legacy_engine.dispose()


def test_schema_compatibility_refuses_to_downgrade_future_database(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO coderus_schema (singleton, version) VALUES (1, 2) "
            "ON CONFLICT(singleton) DO UPDATE SET version=2"
        )

    with pytest.raises(RuntimeError, match="newer than this Coderus release"):
        ensure_schema_compatibility(engine)

    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version FROM coderus_schema WHERE singleton=1"
        ).scalar_one() == 2
