from sqlalchemy import select

from coderus.db import create_session_factory
from coderus.models import Issue, Repository, Task, User
from coderus.workflow.task_state import cas_task_status


def _task(session) -> Task:
    user = User(username="cas-admin", password_hash="hash", role="admin")
    repository = Repository(
        provider="github",
        owner="octo",
        name="cas-demo",
        canonical_url="https://github.com/octo/cas-demo",
        default_branch="main",
        created_by_user=user,
    )
    task = Task(
        issue=Issue(
            repository=repository,
            external_id="1",
            number=1,
            title="CAS",
            body="",
            state="open",
            source_url="https://github.com/octo/cas-demo/issues/1",
            triage_state="dispatched",
        ),
        creator=user,
        status="awaiting_human_review",
    )
    session.add(task)
    session.commit()
    return task


def test_cas_rejects_a_stale_expected_state(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as stale_session:
        task = _task(stale_session)
        stale_status = task.status
        with sessions() as concurrent_session:
            concurrent_task = concurrent_session.get(Task, task.id)
            concurrent_task.status = "queued"
            concurrent_session.commit()

        changed = cas_task_status(
            stale_session,
            task.id,
            expected=stale_status,
            new_status="dismissed",
        )
        stale_session.commit()

    assert changed is False
    with sessions() as session:
        assert session.scalar(select(Task.status)) == "queued"


def test_cas_updates_only_an_expected_state(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task = _task(session)

        changed = cas_task_status(
            session,
            task.id,
            expected={"awaiting_human_review", "failed"},
            new_status="dismissed",
        )
        session.commit()

    assert changed is True
    with sessions() as session:
        assert session.scalar(select(Task.status)) == "dismissed"
