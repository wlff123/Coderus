from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from coderus.db import create_session_factory
from coderus.models import Issue, Repository, Task, TaskTransition, User
from coderus.workflow.task_state import cas_task_status, claim_queued_task


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


def test_claim_queued_task_is_atomic_and_records_a_lease(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = _task(session).id
        task = session.get(Task, task_id)
        task.status = "queued"
        session.commit()

    with sessions() as first_session:
        first_token = claim_queued_task(
            first_session,
            task_id,
            global_limit=8,
            per_user_limit=2,
            lease_seconds=120,
        )
        first_session.commit()
    with sessions() as second_session:
        second_token = claim_queued_task(
            second_session,
            task_id,
            global_limit=8,
            per_user_limit=2,
            lease_seconds=120,
        )
        second_session.commit()

    assert first_token is not None
    assert second_token is None
    with sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "preparing"
        assert task.claim_token == first_token
        assert task.claim_expires_at > datetime.now(UTC).replace(tzinfo=None)


def test_stale_claim_cannot_transition_a_new_owner_task(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = _task(session).id
        task = session.get(Task, task_id)
        task.status = "preparing"
        task.claim_token = "fresh-owner"
        task.claim_expires_at = datetime.now(UTC) + timedelta(minutes=2)
        session.commit()

    with sessions() as session:
        changed = cas_task_status(
            session,
            task_id,
            expected="preparing",
            new_status="failed",
            claim_token="stale-owner",
        )
        session.commit()

    assert changed is False
    with sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "preparing"
        assert task.claim_token == "fresh-owner"


def test_successful_cas_appends_a_versioned_transition(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = _task(session).id
        changed = cas_task_status(
            session,
            task_id,
            expected="awaiting_human_review",
            new_status="closed",
            actor="test",
        )
        session.commit()

    assert changed is True
    with sessions() as session:
        transition = session.scalar(
            select(TaskTransition).where(TaskTransition.task_id == task_id)
        )
        assert transition.from_status == "awaiting_human_review"
        assert transition.to_status == "closed"
        assert transition.actor == "test"
        assert transition.contract_version == 1
