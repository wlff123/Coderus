"""工作台首页路由：仓库页签与任务聚合展示。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from coderus.models import Issue, PRReviewTask, Repository, Task
from coderus.web.presentation import (
    review_status_label,
    review_status_tone,
    status_label,
    status_tone,
)
from coderus.web.routes.tasks import DEFAULT_TASK_HIDDEN_STATUSES
from coderus.web.ui import WebUI, enabled_repository, redirect


def build_dashboard_router(
    *,
    ui: WebUI,
    session_factory: Callable[[], Session],
    forge_status: Callable[[], dict[str, dict[str, bool | str]]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, repository: int | None = None):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            repositories = session.scalars(
                select(Repository)
                .where(Repository.is_enabled.is_(True))
                .order_by(Repository.provider, Repository.owner, Repository.name)
            ).all()
            selected_repository = enabled_repository(session, repository)
            issue_scope = (
                [Issue.repository_id == selected_repository.id]
                if selected_repository is not None
                else []
            )
            review_scope = (
                [PRReviewTask.repository_id == selected_repository.id]
                if selected_repository is not None
                else []
            )
            counts = {
                "issues": session.scalar(
                    select(func.count())
                    .select_from(Issue)
                    .where(
                        Issue.triage_state == "discovered",
                        Issue.state == "open",
                        *issue_scope,
                    )
                )
                or 0,
                "issue_tasks": session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .join(Task.issue)
                    .where(
                        Task.status.not_in(DEFAULT_TASK_HIDDEN_STATUSES), *issue_scope
                    )
                )
                or 0,
                "review_tasks": session.scalar(
                    select(func.count())
                    .select_from(PRReviewTask)
                    .where(PRReviewTask.status != "completed", *review_scope)
                )
                or 0,
                "attention": (
                    session.scalar(
                        select(func.count())
                        .select_from(Task)
                        .join(Task.issue)
                        .where(
                            Task.status.in_({"failed", "manual_intervention"}),
                            *issue_scope,
                        )
                    )
                    or 0
                )
                + (
                    session.scalar(
                        select(func.count())
                        .select_from(PRReviewTask)
                        .where(PRReviewTask.status == "failed", *review_scope)
                    )
                    or 0
                ),
            }
            recent_issues = session.scalars(
                select(Issue)
                .options(selectinload(Issue.repository))
                .where(
                    Issue.triage_state == "discovered",
                    Issue.state == "open",
                    *issue_scope,
                )
                .order_by(Issue.source_updated_at.desc(), Issue.id.desc())
                .limit(8)
            ).all()
            issue_tasks = session.scalars(
                select(Task)
                .join(Task.issue)
                .options(
                    selectinload(Task.issue).selectinload(Issue.repository),
                    selectinload(Task.creator),
                )
                .where(Task.status.not_in(DEFAULT_TASK_HIDDEN_STATUSES), *issue_scope)
                .order_by(Task.created_at.desc())
                .limit(10)
            ).all()
            review_tasks = session.scalars(
                select(PRReviewTask)
                .options(selectinload(PRReviewTask.repository))
                .where(PRReviewTask.status != "completed", *review_scope)
                .order_by(PRReviewTask.created_at.desc())
                .limit(10)
            ).all()
            recent_tasks = [
                {
                    "key": f"RE-{task.id}",
                    "type": "Issue 处理",
                    "detail_url": f"/tasks/{task.id}",
                    "repository": task.issue.repository,
                    "target": f"#{task.issue.number} {task.issue.title}",
                    "status": task.status,
                    "status_label": status_label(task.status),
                    "status_tone": status_tone(task.status),
                    "created_at": task.created_at,
                }
                for task in issue_tasks
            ] + [
                {
                    "key": f"RV-{task.id}",
                    "type": "代码检视",
                    "detail_url": f"/reviews/{task.id}",
                    "repository": task.repository,
                    "target": f"PR #{task.pr_number}",
                    "status": task.status,
                    "status_label": review_status_label(task.status),
                    "status_tone": review_status_tone(task.status),
                    "created_at": task.created_at,
                }
                for task in review_tasks
            ]
            recent_tasks.sort(key=lambda item: item["created_at"], reverse=True)
            recent_tasks = recent_tasks[:10]
            return ui.templates.TemplateResponse(
                request,
                "dashboard.html",
                ui.context(
                    request,
                    current,
                    counts=counts,
                    issues=recent_issues,
                    tasks=recent_tasks,
                    repositories=repositories,
                    selected_repository=selected_repository,
                    selected_repository_id=(
                        selected_repository.id
                        if selected_repository is not None
                        else None
                    ),
                    forge_status=forge_status(),
                ),
            )

    return router
