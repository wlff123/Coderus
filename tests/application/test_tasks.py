from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from coderus.application.errors import Conflict, Forbidden, NotFound
from coderus.application.tasks import TaskCommands
from coderus.models import PRFeedback, Task
from coderus.publisher import PRFeedbackItem

from .conftest import seed_task, seed_user


class FakeForge:
    def __init__(self, *, pr_status: str | None = None) -> None:
        self.pr_status = pr_status
        self.feedback = [
            PRFeedbackItem(
                provider_id="1",
                kind="issue_comment",
                author="octocat",
                author_association="OWNER",
                body="please add a test",
                url="https://github.com/octo/demo/pull/9#issuecomment-1",
            )
        ]

    async def list_pr_feedback(self, owner: str, name: str, pr_number: int):
        return self.feedback

    async def get_pr_status(self, owner: str, name: str, pr_number: int):
        return self.pr_status


class FakeForges:
    def __init__(self, *, forge: FakeForge | None = None, publish: bool = True) -> None:
        self.forge = forge or FakeForge()
        self.publish = publish

    def supports(self, provider: str, *capabilities) -> bool:
        return self.publish

    def get(self, provider: str):
        return self.forge


def commands(session_factory, forges: FakeForges | None = None) -> TaskCommands:
    return TaskCommands(session_factory=session_factory, forges=forges or FakeForges())


def test_cancel_queued_task_finishes_without_signal(session_factory) -> None:
    with session_factory() as session:
        task = seed_task(session, status="queued")
        task_id, actor_id = task.id, task.created_by

    result = commands(session_factory).request_cancel(task_id, actor_id)

    assert result.should_signal_runner is False
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == "cancelled"
        assert task.finished_at is not None


def test_cancel_running_task_requests_signal(session_factory) -> None:
    with session_factory() as session:
        task = seed_task(session, status="developer_working")
        task_id, actor_id = task.id, task.created_by

    result = commands(session_factory).request_cancel(task_id, actor_id)

    assert result.should_signal_runner is True
    with session_factory() as session:
        assert session.get(Task, task_id).status == "cancelling"


def test_cancel_rejects_terminal_status(session_factory) -> None:
    with session_factory() as session:
        task = seed_task(session, status="completed")
        task_id, actor_id = task.id, task.created_by

    with pytest.raises(Conflict, match="当前状态不能取消"):
        commands(session_factory).request_cancel(task_id, actor_id)


def test_only_owner_or_admin_can_operate(session_factory) -> None:
    with session_factory() as session:
        task = seed_task(session, status="queued")
        other = seed_user(session, username="other", role="user")
        task_id, other_id = task.id, other.id

    with pytest.raises(Forbidden):
        commands(session_factory).request_cancel(task_id, other_id)
    with pytest.raises(NotFound):
        commands(session_factory).request_cancel(999, other_id)


def test_close_only_accepts_closable_statuses(session_factory) -> None:
    with session_factory() as session:
        task = seed_task(session, status="failed")
        task_id, actor_id = task.id, task.created_by

    commands(session_factory).close(task_id, actor_id)

    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == "dismissed"
        assert task.finished_at is not None

    with pytest.raises(Conflict, match="当前状态不能关闭"):
        commands(session_factory).close(task_id, actor_id)


def test_queue_existing_publish_requires_reusable_commit(
    session_factory, tmp_path: Path
) -> None:
    with session_factory() as session:
        task = seed_task(session, status="manual_intervention")
        task_id, actor_id = task.id, task.created_by

    with pytest.raises(Conflict, match="当前任务不能按现状发布"):
        commands(session_factory).queue_existing_publish(task_id, actor_id)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with session_factory() as session:
        task = session.get(Task, task_id)
        task.commit_sha = "abc123"
        task.workspace_path = str(workspace)
        task.branch_name = "coderus/issue-1-1"
        session.commit()

    commands(session_factory).queue_existing_publish(task_id, actor_id)

    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == "queued"
        assert task.failure_code == "publish_existing"
        assert task.failure_summary is None
        assert task.finished_at is None


def test_queue_existing_publish_requires_publish_capability(
    session_factory, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with session_factory() as session:
        task = seed_task(session, status="failed")
        task.commit_sha = "abc123"
        task.workspace_path = str(workspace)
        task.branch_name = "coderus/issue-1-1"
        session.commit()
        task_id, actor_id = task.id, task.created_by

    with pytest.raises(Conflict, match="当前任务不能按现状发布"):
        commands(session_factory, FakeForges(publish=False)).queue_existing_publish(
            task_id, actor_id
        )


def test_queue_feedback_revision_selects_trusted_rows(session_factory) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        task = seed_task(session, status="awaiting_human_review")
        trusted = PRFeedback(
            task_id=task.id,
            provider_id="github:1",
            kind="issue_comment",
            author="octocat",
            author_association="OWNER",
            body="please add a test",
            url="https://github.com/octo/demo/pull/9#issuecomment-1",
            created_at=now,
        )
        untrusted = PRFeedback(
            task_id=task.id,
            provider_id="github:2",
            kind="issue_comment",
            author="random",
            author_association="NONE",
            body="drive-by comment",
            url="https://github.com/octo/demo/pull/9#issuecomment-2",
            created_at=now,
        )
        session.add_all([trusted, untrusted])
        session.commit()
        task_id, actor_id = task.id, task.created_by
        trusted_id, untrusted_id = trusted.id, untrusted.id

    with pytest.raises(Conflict, match="只能处理可信维护者的未处理意见"):
        commands(session_factory).queue_feedback_revision(
            task_id, actor_id, (trusted_id, untrusted_id)
        )

    commands(session_factory).queue_feedback_revision(task_id, actor_id, (trusted_id,))

    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == "queued"
        assert task.failure_code == "pr_feedback_revision"
        assert session.get(PRFeedback, trusted_id).selected_at is not None
        assert session.get(PRFeedback, untrusted_id).selected_at is None


def test_queue_feedback_revision_requires_selection(session_factory) -> None:
    with session_factory() as session:
        task = seed_task(session, status="awaiting_human_review")
        task_id, actor_id = task.id, task.created_by

    with pytest.raises(Conflict, match="当前任务不能处理 PR 意见"):
        commands(session_factory).queue_feedback_revision(task_id, actor_id, ())


def test_sync_feedback_upserts_and_completes_merged_pr(session_factory) -> None:
    with session_factory() as session:
        task = seed_task(session, status="awaiting_human_review")
        task.pr_number = 9
        session.commit()
        task_id, actor_id = task.id, task.created_by

    forges = FakeForges(forge=FakeForge(pr_status="merged"))
    count = asyncio.run(
        commands(session_factory, forges).sync_feedback(task_id, actor_id)
    )

    assert count == 1
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task.status == "completed"
        assert task.pr_state == "merged"
        assert task.finished_at is not None
        feedback = session.scalars(select(PRFeedback)).all()
        assert len(feedback) == 1
        assert feedback[0].provider_id == "github:1"


def test_sync_feedback_requires_awaiting_review_with_pr(session_factory) -> None:
    with session_factory() as session:
        task = seed_task(session, status="awaiting_human_review")
        task_id, actor_id = task.id, task.created_by

    with pytest.raises(Conflict, match="当前任务不能同步 PR 意见"):
        asyncio.run(commands(session_factory).sync_feedback(task_id, actor_id))
