"""任务列表、详情与任务操作路由；写操作全部委托 TaskCommands。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from coderus.application import Conflict, Forbidden, NotFound, TaskCommands
from coderus.auth.security import verify_csrf_token
from coderus.forge import ForgeCapability, ForgeRegistry
from coderus.models import Issue, Task, User
from coderus.web.ui import WebUI, enabled_repository, redirect

DEFAULT_TASK_HIDDEN_STATUSES = frozenset(
    {"completed", "closed", "dismissed", "cancelled"}
)


def build_task_router(
    *,
    ui: WebUI,
    session_factory: Callable[[], Session],
    tasks: TaskCommands,
    forges: ForgeRegistry,
    forge_status: Callable[[], dict[str, dict[str, bool | str]]],
    signal_cancel: Callable[[int], None],
) -> APIRouter:
    router = APIRouter()

    @router.get("/tasks", response_class=HTMLResponse)
    def tasks_page(
        request: Request,
        status: str = "active",
        owner: str | None = None,
        repository: int | None = None,
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            selected_repository = enabled_repository(session, repository)
            statement = (
                select(Task)
                .join(Task.creator)
                .join(Task.issue)
                .options(
                    selectinload(Task.issue).selectinload(Issue.repository),
                    selectinload(Task.creator),
                )
                .order_by(Task.created_at.desc())
            )
            if status == "active":
                statement = statement.where(
                    Task.status.not_in(DEFAULT_TASK_HIDDEN_STATUSES)
                )
            elif status != "all":
                statement = statement.where(Task.status == status)
            if owner:
                statement = statement.where(User.username == owner.strip().lower())
            if selected_repository is not None:
                statement = statement.where(
                    Issue.repository_id == selected_repository.id
                )
            task_rows = session.scalars(statement).all()
            users = session.scalars(select(User).order_by(User.username)).all()
            return ui.templates.TemplateResponse(
                request,
                "tasks.html",
                ui.context(
                    request,
                    current,
                    tasks=task_rows,
                    status_filter=status,
                    owner_filter=owner or "",
                    users=users,
                    selected_repository=selected_repository,
                    selected_repository_id=(
                        selected_repository.id
                        if selected_repository is not None
                        else None
                    ),
                ),
            )

    @router.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(request: Request, task_id: int):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            task = session.scalar(
                select(Task)
                .options(
                    selectinload(Task.creator),
                    selectinload(Task.issue).selectinload(Issue.repository),
                    selectinload(Task.agent_runs),
                    selectinload(Task.reviews),
                    selectinload(Task.pr_feedback),
                )
                .where(Task.id == task_id)
            )
            if task is None:
                return HTMLResponse("Not found", status_code=404)
            current_runs = [
                run
                for run in task.agent_runs
                if run.role in {"developer", "reviewer_a", "reviewer_b"}
            ]
            latest_runs_by_role = {}
            for run in sorted(current_runs, key=lambda item: item.id):
                latest_runs_by_role[run.role] = run
            latest_reviews_by_role = {}
            for review in sorted(task.reviews, key=lambda item: item.id):
                latest_reviews_by_role[review.reviewer_role] = review
            latest_agent_runs = sorted(
                latest_runs_by_role.values(), key=lambda item: item.id
            )
            latest_reviews = sorted(
                latest_reviews_by_role.values(), key=lambda item: item.id
            )
            latest_run_ids = {run.id for run in latest_agent_runs}
            latest_review_ids = {review.id for review in latest_reviews}
            can_publish_wip = bool(
                forges.supports(
                    task.issue.repository.provider, ForgeCapability.PUBLISH
                )
                and task.status in {"manual_intervention", "failed"}
                and task.commit_sha
                and task.workspace_path
                and task.branch_name
                and Path(task.workspace_path).is_dir()
            )
            return ui.templates.TemplateResponse(
                request,
                "task_detail.html",
                ui.context(
                    request,
                    current,
                    task=task,
                    latest_agent_runs=latest_agent_runs,
                    historical_agent_runs=[
                        run for run in task.agent_runs if run.id not in latest_run_ids
                    ],
                    latest_reviews=latest_reviews,
                    historical_reviews=[
                        review
                        for review in task.reviews
                        if review.id not in latest_review_ids
                    ],
                    can_publish_wip=can_publish_wip,
                    provider=task.issue.repository.provider,
                    forge_status=forge_status(),
                    can_sync_pr_feedback=bool(
                        task.status == "awaiting_human_review"
                        and task.pr_number
                        and forges.supports(
                            task.issue.repository.provider,
                            ForgeCapability.LIST_PR_FEEDBACK,
                        )
                    ),
                ),
            )

    def guarded(request: Request, csrf_token: str):
        """通过则返回当前用户，否则返回应直接发送的响应。"""
        with session_factory() as session:
            current = ui.current_user(request, session)
        if current is None:
            return None, redirect("/login")
        if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
            return None, HTMLResponse("Invalid CSRF token", status_code=400)
        return current, None

    def command_error(exc: NotFound | Forbidden | Conflict) -> HTMLResponse:
        if isinstance(exc, NotFound):
            return HTMLResponse("Not found", status_code=404)
        if isinstance(exc, Forbidden):
            return HTMLResponse("Forbidden", status_code=403)
        return HTMLResponse(str(exc), status_code=409)

    @router.post("/tasks/{task_id}/cancel")
    def cancel_task(request: Request, task_id: int, csrf_token: str = Form()):
        current, error = guarded(request, csrf_token)
        if error is not None:
            return error
        try:
            result = tasks.request_cancel(task_id, current.id)
        except (NotFound, Forbidden, Conflict) as exc:
            return command_error(exc)
        ui.flash(request, "任务取消请求已提交")
        if result.should_signal_runner:
            signal_cancel(task_id)
        return redirect(f"/tasks/{task_id}")

    @router.post("/tasks/{task_id}/close")
    def close_task(request: Request, task_id: int, csrf_token: str = Form()):
        current, error = guarded(request, csrf_token)
        if error is not None:
            return error
        try:
            tasks.close(task_id, current.id)
        except (NotFound, Forbidden, Conflict) as exc:
            return command_error(exc)
        ui.flash(request, "任务已关闭")
        return redirect(f"/tasks/{task_id}")

    @router.post("/tasks/{task_id}/feedback/sync")
    async def sync_task_feedback(
        request: Request, task_id: int, csrf_token: str = Form()
    ):
        current, error = guarded(request, csrf_token)
        if error is not None:
            return error
        try:
            count = await tasks.sync_feedback(task_id, current.id)
        except (NotFound, Forbidden, Conflict) as exc:
            return command_error(exc)
        ui.flash(request, f"已同步 {count} 条 PR 意见")
        return redirect(f"/tasks/{task_id}")

    @router.post("/tasks/{task_id}/publish-wip")
    def publish_existing_wip(
        request: Request, task_id: int, csrf_token: str = Form()
    ):
        current, error = guarded(request, csrf_token)
        if error is not None:
            return error
        try:
            tasks.queue_existing_publish(task_id, current.id)
        except (NotFound, Forbidden, Conflict) as exc:
            return command_error(exc)
        ui.flash(request, "任务已重新入队，将复用现有提交发布 PR")
        return redirect(f"/tasks/{task_id}")

    @router.post("/tasks/{task_id}/feedback/handle")
    def handle_task_feedback(
        request: Request,
        task_id: int,
        csrf_token: str = Form(),
        feedback_ids: Annotated[list[int] | None, Form()] = None,
    ):
        current, error = guarded(request, csrf_token)
        if error is not None:
            return error
        try:
            tasks.queue_feedback_revision(
                task_id, current.id, tuple(feedback_ids or ())
            )
        except (NotFound, Forbidden, Conflict) as exc:
            return command_error(exc)
        return redirect(f"/tasks/{task_id}")

    return router
