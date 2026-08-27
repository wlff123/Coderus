"""仓库管理路由：添加、同步与启停。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coderus.auth.security import verify_csrf_token
from coderus.forge import ForgeCapability, ForgeRegistry
from coderus.issues.poller import IssuePoller
from coderus.issues.service import sync_repository
from coderus.models import Repository
from coderus.providers.urls import parse_repository_url
from coderus.web.presentation import provider_error_message
from coderus.web.ui import WebUI, redirect


def build_repository_router(
    *,
    ui: WebUI,
    session_factory: Callable[[], Session],
    providers: Mapping[str, object],
    forges: ForgeRegistry,
    issue_poller: IssuePoller,
    forge_status: Callable[[], dict[str, dict[str, bool | str]]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/repositories", response_class=HTMLResponse)
    def repositories_page(request: Request):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            repositories = session.scalars(
                select(Repository).order_by(Repository.created_at)
            ).all()
            return ui.templates.TemplateResponse(
                request,
                "repositories.html",
                ui.context(
                    request,
                    current,
                    repositories=repositories,
                    forge_status=forge_status(),
                ),
            )

    @router.post("/repositories")
    async def add_repository(
        request: Request,
        url: str = Form(),
        csrf_token: str = Form(),
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            try:
                parsed = parse_repository_url(url)
                provider = providers[parsed.provider]
                metadata = await asyncio.to_thread(
                    provider.get_repository, parsed.canonical_url
                )
                if metadata.is_private or metadata.issues_enabled is False:
                    raise ValueError("仓库必须公开且启用 Issue")
                fork = None
                forge = forges.get(metadata.provider)
                if forges.supports(metadata.provider, ForgeCapability.ENSURE_FORK):
                    fork = await forge.ensure_fork(metadata.owner, metadata.name)
                repository = Repository(
                    provider=metadata.provider,
                    owner=metadata.owner,
                    name=metadata.name,
                    canonical_url=metadata.canonical_url,
                    default_branch=metadata.default_branch or "main",
                    fork_owner=fork.owner if fork else None,
                    fork_url=fork.url if fork else None,
                    created_by=current.id,
                )
                session.add(repository)
                session.commit()
                ui.flash(request, f"仓库 {repository.owner}/{repository.name} 已添加")
            except Exception as exc:
                session.rollback()
                ui.flash(request, provider_error_message(exc), "danger")
        return redirect("/repositories")

    @router.post("/repositories/{repository_id}/sync")
    def force_sync(request: Request, repository_id: int, csrf_token: str = Form()):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            repository = session.get(Repository, repository_id)
            if repository is None:
                return HTMLResponse("Not found", status_code=404)
            if repository.sync_status == "running":
                ui.flash(request, "仓库正在同步，请稍后刷新状态", "warning")
                return redirect("/repositories")
            try:
                sync_repository(session, repository, providers[repository.provider])
                session.commit()
                ui.flash(request, f"{repository.owner}/{repository.name} 同步完成")
            except Exception as exc:
                repository.sync_status = "failed"
                repository.last_sync_error = provider_error_message(exc)[:1000]
                session.commit()
                ui.flash(request, repository.last_sync_error, "danger")
        return redirect("/repositories")

    @router.post("/repositories/{repository_id}/toggle")
    def toggle_repository(
        request: Request, repository_id: int, csrf_token: str = Form()
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            repository = session.get(Repository, repository_id)
            if repository is None:
                return HTMLResponse("Not found", status_code=404)
            if repository.sync_status == "running":
                ui.flash(request, "仓库正在同步，当前不能修改启用状态", "warning")
                return redirect("/repositories")
            repository.is_enabled = not repository.is_enabled
            session.commit()
            ui.flash(
                request,
                f"{repository.owner}/{repository.name} "
                f"已{'启用' if repository.is_enabled else '停用'}",
            )
        return redirect("/repositories")

    @router.post("/repositories/sync-all")
    async def force_sync_all(request: Request, csrf_token: str = Form()):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
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
