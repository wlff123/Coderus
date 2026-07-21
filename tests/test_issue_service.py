from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from coderus.issues.service import dispatch_issue, sync_repository, upsert_provider_issue
from coderus.models import Issue, Repository, Task, User
from coderus.providers.models import Issue as ProviderIssue
from coderus.providers.models import Repository as ProviderRepository


def provider_issue(title: str = "First title") -> ProviderIssue:
    repository = ProviderRepository(
        provider="github",
        owner="octo",
        name="demo",
        canonical_url="https://github.com/octo/demo",
        default_branch="main",
    )
    return ProviderIssue(
        repository=repository,
        external_id="101",
        number=1,
        title=title,
        body="details",
        state="open",
        labels=("bug",),
        canonical_url="https://github.com/octo/demo/issues/1",
        created_at=None,
        updated_at=datetime(2026, 7, 15, tzinfo=UTC),
    )


def repository_fixture(session) -> tuple[User, Repository]:
    user = User(username="admin", password_hash="hash", role="admin")
    repository = Repository(
        provider="github",
        owner="octo",
        name="demo",
        canonical_url="https://github.com/octo/demo",
        default_branch="main",
        created_by_user=user,
    )
    session.add(repository)
    session.commit()
    return user, repository


def test_upsert_preserves_ignored_triage(session) -> None:
    user, repository = repository_fixture(session)
    issue = upsert_provider_issue(session, repository, provider_issue())
    issue.triage_state = "ignored"
    issue.ignored_by = user.id
    session.commit()

    updated = upsert_provider_issue(session, repository, provider_issue("Updated title"))
    session.commit()

    assert updated.id == issue.id
    assert updated.title == "Updated title"
    assert updated.triage_state == "ignored"
    assert updated.ignored_by == user.id


def test_sync_repository_is_idempotent(session) -> None:
    _, repository = repository_fixture(session)

    class Provider:
        def list_open_issues(self, _repository):
            return [provider_issue()]

    assert sync_repository(session, repository, Provider()) == 1
    assert sync_repository(session, repository, Provider()) == 1
    assert session.query(Issue).count() == 1
    assert repository.sync_status == "succeeded"


def test_dispatch_issue_creates_one_active_task(session) -> None:
    user, repository = repository_fixture(session)
    issue = upsert_provider_issue(session, repository, provider_issue())
    session.commit()

    task = dispatch_issue(session, issue, user, "Please reproduce before changing code")

    assert task.status == "queued"
    assert task.created_by == user.id
    assert issue.triage_state == "dispatched"


def test_dispatch_issue_can_defer_commit_to_caller(session) -> None:
    user, repository = repository_fixture(session)
    issue = upsert_provider_issue(session, repository, provider_issue())
    session.commit()

    task = dispatch_issue(session, issue, user, commit=False)

    assert task.id is not None
    session.rollback()
    assert session.query(Task).count() == 0
    assert session.get(Issue, issue.id).triage_state == "discovered"


def test_dispatch_issue_translates_active_task_race_to_domain_error(
    session,
    monkeypatch,
) -> None:
    user, repository = repository_fixture(session)
    issue = upsert_provider_issue(session, repository, provider_issue())
    session.commit()

    def fail_commit() -> None:
        raise IntegrityError("INSERT INTO tasks", {}, Exception("unique constraint"))

    monkeypatch.setattr(session, "commit", fail_commit)

    with pytest.raises(ValueError, match="该 Issue 已有未结束任务"):
        dispatch_issue(session, issue, user)

    assert session.query(Task).count() == 0
