"""仓库管理路由：添加、同步与启停。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coderus.application import Conflict, NotFound
from coderus.application.repositories import RepositoryCommands, SyncFailed
from coderus.auth.security import verify_csrf_token
from coderus.issues.poller import IssuePoller
from coderus.models import Repository
from coderus.web.presentation import provider_error_message
from coderus.web.ui import WebUI, redirect


def build_repository_router(
    *,
    ui: WebUI,
    session_factory: Callable[[], Session],
    repositories: RepositoryCommands,
    issue_poller: IssuePoller,
    forge_status: Callable[[], dict[str, dict[str, bool | str]]],
) -> APIRouter:
    router = APIRouter()

    def admin_or_response(request: Request, csrf_token: str):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return None, redirect("/login")
            if current.role != "admin":
                return None, HTMLResponse("Forbidden", status_code=403)
            current_id = current.id
        if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
            return None, HTMLResponse("Invalid CSRF token", status_code=400)
        return current_id, None

    @router.get("/repositories", response_class=HTMLResponse)
    def repositories_page(request: Request):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            rows = session.scalars(
                select(Repository).order_by(Repository.created_at)
            ).all()
            return ui.templates.TemplateResponse(
                request,
                "repositories.html",
                ui.context(
                    request,
                    current,
                    repositories=rows,
                    forge_status=forge_status(),
                ),
            )

    @router.post("/repositories")
    async def add_repository(
        request: Request,
        url: str = Form(),
        csrf_token: str = Form(),
    ):
        current_id, error = admin_or_response(request, csrf_token)
        if error is not None:
            return error
        try:
            ref = await repositories.add(url, current_id)
            ui.flash(request, f"仓库 {ref.owner}/{ref.name} 已添加")
        except Exception as exc:
            ui.flash(request, provider_error_message(exc), "danger")
        return redirect("/repositories")

    @router.post("/repositories/{repository_id}/sync")
    def force_sync(request: Request, repository_id: int, csrf_token: str = Form()):
        _, error = admin_or_response(request, csrf_token)
        if error is not None:
            return error
        try:
            ref = repositories.sync(repository_id)
            ui.flash(request, f"{ref.owner}/{ref.name} 同步完成")
        except NotFound:
            return HTMLResponse("Not found", status_code=404)
        except Conflict as exc:
            ui.flash(request, str(exc), "warning")
        except SyncFailed as exc:
            ui.flash(request, str(exc), "danger")
        return redirect("/repositories")

    @router.post("/repositories/{repository_id}/toggle")
    def toggle_repository(
        request: Request, repository_id: int, csrf_token: str = Form()
    ):
        _, error = admin_or_response(request, csrf_token)
        if error is not None:
            return error
        try:
            ref = repositories.toggle(repository_id)
            ui.flash(
                request,
                f"{ref.owner}/{ref.name} 已{'启用' if ref.is_enabled else '停用'}",
            )
        except NotFound:
            return HTMLResponse("Not found", status_code=404)
        except Conflict as exc:
            ui.flash(request, str(exc), "warning")
        return redirect("/repositories")

    @router.post("/repositories/sync-all")
    async def force_sync_all(request: Request, csrf_token: str = Form()):
        _, error = admin_or_response(request, csrf_token)
        if error is not None:
            return error
        with session_factory() as session:
            if session.scalar(
                select(func.count())
                .select_from(Repository)
                .where(Repository.sync_status == "running")
            ):
                ui.flash(request, "已有仓库正在同步，请稍后再刷新全部", "warning")
                return redirect("/repositories")
        await issue_poller.tick()
        with session_factory() as session:
            failed_count = (
                session.scalar(
                    select(func.count())
                    .select_from(Repository)
                    .where(
                        Repository.is_enabled.is_(True),
                        Repository.sync_status == "failed",
                    )
                )
                or 0
            )
        if failed_count:
            ui.flash(request, f"刷新完成，{failed_count} 个仓库刷新失败", "danger")
        else:
            ui.flash(request, "全部仓库刷新完成")
        return redirect("/repositories")

    return router
