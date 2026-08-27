"""Read-only database snapshot injected into assistant prompts."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coderus.models import PRReviewTask, Repository, Task
from coderus.tasks.statuses import RUNNING_TASK_STATES

TASK_REFERENCE_PATTERN = re.compile(r"\b(RE|RV)-(\d+)\b", re.IGNORECASE)
_MAX_TASK_REFERENCES = 3
_MAX_RECENT_TASKS = 10
_MAX_REPOSITORIES = 20


def build_context(session: Session, question: str) -> str:
    sections = [
        _status_section(session),
        _recent_tasks_section(session),
        _recent_reviews_section(session),
        _repositories_section(session),
        *_referenced_task_sections(session, question),
    ]
    return "\n\n".join(section for section in sections if section)


def _status_section(session: Session) -> str:
    counts = dict(
        session.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    )
    review_counts = dict(
        session.execute(
            select(PRReviewTask.status, func.count()).group_by(PRReviewTask.status)
        ).all()
    )
    running = sum(counts.get(status, 0) for status in RUNNING_TASK_STATES)
    review_detail = "，".join(
        f"{status} {count}" for status, count in sorted(review_counts.items())
    )
    return "\n".join(
        (
            "任务统计：",
            f"开发任务（RE）共 {sum(counts.values())}：排队中 {counts.get('queued', 0)}，"
            f"执行中 {running}，等待人工审核 {counts.get('awaiting_human_review', 0)}，"
            f"需要人工处理 {counts.get('manual_intervention', 0)}，"
            f"失败 {counts.get('failed', 0)}",
            f"PR 检视任务（RV）共 {sum(review_counts.values())}"
            + (f"：{review_detail}" if review_detail else ""),
        )
    )


def _recent_tasks_section(session: Session) -> str:
    tasks = session.scalars(
        select(Task).order_by(Task.id.desc()).limit(_MAX_RECENT_TASKS)
    ).all()
    if not tasks:
        return "最近开发任务：暂无"
    lines = ["最近开发任务："]
    for task in tasks:
        issue = task.issue
        repository = issue.repository
        lines.append(
            f"RE-{task.id} [{task.status}] {repository.owner}/{repository.name}"
            f"#{issue.number} {issue.title}"
        )
    return "\n".join(lines)


def _recent_reviews_section(session: Session) -> str:
    reviews = session.scalars(
        select(PRReviewTask).order_by(PRReviewTask.id.desc()).limit(_MAX_RECENT_TASKS)
    ).all()
    if not reviews:
        return "最近 PR 检视任务：暂无"
    lines = ["最近 PR 检视任务："]
    for review in reviews:
        repository = review.repository
        lines.append(
            f"RV-{review.id} [{review.status}] {repository.owner}/{repository.name}"
            f" PR#{review.pr_number}"
        )
    return "\n".join(lines)


def _repositories_section(session: Session) -> str:
    repositories = session.scalars(
        select(Repository)
        .where(Repository.is_enabled.is_(True))
        .order_by(Repository.id)
        .limit(_MAX_REPOSITORIES)
    ).all()
    if not repositories:
        return "已启用仓库：暂无"
    lines = ["已启用仓库："]
    lines.extend(
        f"{repository.provider}/{repository.owner}/{repository.name}"
        for repository in repositories
    )
    return "\n".join(lines)


def _referenced_task_sections(session: Session, question: str) -> list[str]:
    references = dict.fromkeys(
        (match.group(1).upper(), int(match.group(2)))
        for match in TASK_REFERENCE_PATTERN.finditer(question)
    )
    return [
        _issue_task_section(session, task_id)
        if kind == "RE"
        else _review_task_section(session, task_id)
        for kind, task_id in list(references)[:_MAX_TASK_REFERENCES]
    ]


def _issue_task_section(session: Session, task_id: int) -> str:
    task = session.get(Task, task_id)
    if task is None:
        return f"任务 RE-{task_id}：不存在"
    issue = task.issue
    repository = issue.repository
    return "\n".join(
        (
            f"任务 RE-{task.id} 详情：",
            f"状态 {task.status}",
            f"仓库 {repository.provider}/{repository.owner}/{repository.name}",
            f"Issue #{issue.number} {issue.title}",
            f"失败摘要 {task.failure_summary or '-'}",
            f"PR {task.pr_url or '-'}",
        )
    )


def _review_task_section(session: Session, task_id: int) -> str:
    task = session.get(PRReviewTask, task_id)
    if task is None:
        return f"检视任务 RV-{task_id}：不存在"
    repository = task.repository
    return "\n".join(
        (
            f"检视任务 RV-{task.id} 详情：",
            f"状态 {task.status}",
            f"仓库 {repository.provider}/{repository.owner}/{repository.name}",
            f"PR {task.pr_url}",
            f"失败摘要 {task.failure_summary or '-'}",
            f"评论 {task.comment_url or '-'}",
        )
    )
