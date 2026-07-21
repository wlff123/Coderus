from sqlalchemy import inspect

from coderus.db import ensure_schema_compatibility
from coderus.models import (
    Base,
    IntegrationCredential,
    Issue,
    PRReviewTask,
    Repository,
    Task,
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
