from __future__ import annotations

from sqlalchemy.orm import Session

from coderus.assistant.context import build_context
from coderus.models import Issue, PRReviewTask, Repository, Task, User


def seed_task(session: Session, *, status: str = "queued") -> Task:
    user = User(username="admin", password_hash="hash", role="admin")
    repository = Repository(
        provider="github",
        owner="octo",
        name="demo",
        canonical_url="https://github.com/octo/demo",
        created_by_user=user,
    )
    issue = Issue(
        repository=repository,
        external_id="1",
        number=1,
        title="crash on start",
        body="details",
        state="open",
        source_url="https://github.com/octo/demo/issues/1",
    )
    task = Task(issue=issue, creator=user, status=status)
    session.add(task)
    session.commit()
    return task


def test_empty_database_snapshot(session: Session) -> None:
    context = build_context(session, "现在忙吗")

    assert "任务统计：" in context
    assert "最近任务：暂无" in context
    assert "已启用仓库：暂无" in context


def test_snapshot_lists_tasks_and_repositories(session: Session) -> None:
    task = seed_task(session, status="developer_working")

    context = build_context(session, "任务进展如何")

    assert f"RE-{task.id} [developer_working] octo/demo#1 crash on start" in context
    assert "github/octo/demo" in context


def test_referenced_tasks_include_details_once(session: Session) -> None:
    task = seed_task(session, status="failed")
    task.failure_summary = "tests failed"
    review = PRReviewTask(
        repository=task.issue.repository,
        pr_number=7,
        pr_url="https://github.com/octo/demo/pull/7",
        status="queued",
        source_chat_id="oc_1",
        source_message_id="om_ctx",
        source_sender_open_id="ou_1",
    )
    session.add(review)
    session.commit()

    question = f"re-{task.id} 和 RE-{task.id} 以及 RV-{review.id} 怎么样了，RE-999 呢"
    context = build_context(session, question)

    assert context.count(f"任务 RE-{task.id} 详情：") == 1
    assert "失败摘要 tests failed" in context
    assert f"检视任务 RV-{review.id} 详情：" in context
    assert "任务 RE-999：不存在" in context
