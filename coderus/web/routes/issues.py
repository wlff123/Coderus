"""Issue 列表、手工添加、派发与忽略/恢复路由。"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from coderus.application import Conflict, IssueCommands, NotFound
from coderus.auth.security import verify_csrf_token
from coderus.models import Issue, Repository
from coderus.web.presentation import provider_error_message
from coderus.web.ui import (
    WebUI,
    enabled_repository,
    redirect,
    repository_scoped_path,
)

ISSUES_PAGE_SIZE = 25


def build_issue_router(
    *,
    ui: WebUI,
    session_factory: Callable[[], Session],
    issues: IssueCommands,
    forge_status: Callable[[], dict[str, dict[str, bool | str]]],
    codex_auth: Callable[[], object],
) -> APIRouter:
    router = APIRouter()

    @router.get("/issues", response_class=HTMLResponse)
    def issues_page(
        request: Request,
        triage: str = "discovered",
        page: int = 1,
        q: str = "",
        repository: int | None = None,
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if triage not in {"discovered", "dispatched", "ignored", "all"}:
                triage = "discovered"
            repositories = session.scalars(
                select(Repository)
                .where(Repository.is_enabled.is_(True))
                .order_by(Repository.provider, Repository.owner, Repository.name)
            ).all()
            selected_repository = enabled_repository(session, repository)
            if selected_repository is not None:
                repositories = [selected_repository] + [
                    item for item in repositories if item.id != selected_repository.id
                ]
            q = q.strip()[:200]
            filters = []
            if selected_repository is not None:
                filters.append(Issue.repository_id == selected_repository.id)
            if triage != "all":
                filters.append(Issue.triage_state == triage)
            if triage == "discovered":
                filters.append(Issue.state == "open")
            if q:
                search = f"%{q}%"
                search_filters = [
                    Issue.title.ilike(search),
                    Repository.owner.ilike(search),
                    Repository.name.ilike(search),
                ]
                issue_number = q.removeprefix("#")
                if issue_number.isdigit():
                    search_filters.append(Issue.number == int(issue_number))
                filters.append(or_(*search_filters))
            total_issues = (
                session.scalar(
                    select(func.count())
                    .select_from(Issue)
                    .join(Issue.repository)
                    .where(*filters)
                )
                or 0
            )
            total_pages = max(
                1, (total_issues + ISSUES_PAGE_SIZE - 1) // ISSUES_PAGE_SIZE
            )
            page = min(max(page, 1), total_pages)
            statement = (
                select(Issue)
                .join(Issue.repository)
                .options(selectinload(Issue.repository))
                .where(*filters)
                .order_by(Issue.source_updated_at.desc(), Issue.id.desc())
                .offset((page - 1) * ISSUES_PAGE_SIZE)
                .limit(ISSUES_PAGE_SIZE)
            )
            issue_rows = session.scalars(statement).all()
            return ui.templates.TemplateResponse(
                request,
                "issues.html",
                ui.context(
                    request,
                    current,
                    issues=issue_rows,
                    selected_triage=triage,
                    page=page,
                    total_pages=total_pages,
                    total_issues=total_issues,
                    search_query=q,
                    repositories=repositories,
                    selected_repository=selected_repository,
                    selected_repository_id=(
                        selected_repository.id
                        if selected_repository is not None
                        else None
                    ),
                    pagination_query=urlencode(
                        {
                            "triage": triage,
                            **({"q": q} if q else {}),
                            **(
                                {"repository": selected_repository.id}
                                if selected_repository is not None
                                else {}
                            ),
                        }
                    ),
                    repository_tab_query=urlencode(
                        {"triage": triage, **({"q": q} if q else {})}
                    ),
                    forge_status=forge_status(),
                    codex_auth=codex_auth(),
                ),
            )

    @router.post("/issues/manual")
    def add_issue_manually(
        request: Request,
        url: str = Form(),
        csrf_token: str = Form(),
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
        if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        try:
            number = issues.add_issue(url)
            ui.flash(request, f"Issue #{number} 已添加")
        except Exception as exc:
            ui.flash(request, provider_error_message(exc), "danger")
        return redirect("/issues")

    @router.post("/issues/{issue_id}/dispatch")
    def dispatch(
        request: Request,
        issue_id: int,
        csrf_token: str = Form(),
        instructions: str = Form(default=""),
        repository: int | None = Form(default=None),
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            issue = session.get(Issue, issue_id)
            if issue is None:
                return HTMLResponse("Not found", status_code=404)
            repository_id = (
                issue.repository_id if repository == issue.repository_id else None
            )
            issue_number = issue.number
            try:
                task_id = issues.dispatch_in_session(
                    session, issue_id, current.id, instructions
                )
            except ValueError as exc:
                ui.flash(request, str(exc), "danger")
                return redirect(repository_scoped_path("/issues", repository_id))
            auth = codex_auth()
            if not auth.ready:
                session.rollback()
                ui.flash(request, auth.detail, "danger")
                return redirect(repository_scoped_path("/issues", repository_id))
            session.commit()
            ui.flash(request, f"Issue #{issue_number} 已派发为 RE-{task_id}")
        return redirect(repository_scoped_path("/tasks", repository_id))

    @router.post("/issues/{issue_id}/ignore")
    def ignore_issue(
        request: Request,
        issue_id: int,
        csrf_token: str = Form(),
        reason: str = Form(default=""),
        repository: int | None = Form(default=None),
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            current_id = current.id
        if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        try:
            ref = issues.ignore(issue_id, current_id, reason)
        except NotFound:
            return HTMLResponse("Not found", status_code=404)
        except Conflict as exc:
            ui.flash(request, str(exc), "danger")
            return redirect(repository_scoped_path("/issues", repository, triage="all"))
        repository_id = ref.repository_id if repository == ref.repository_id else None
        ui.flash(request, f"Issue #{ref.number} 已忽略")
        return redirect(
            repository_scoped_path("/issues", repository_id, triage="ignored")
        )

    @router.post("/issues/{issue_id}/restore")
    def restore_issue(
        request: Request,
        issue_id: int,
        csrf_token: str = Form(),
        repository: int | None = Form(default=None),
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            current_id = current.id
        if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        try:
            ref = issues.restore(issue_id, current_id)
        except NotFound:
            return HTMLResponse("Not found", status_code=404)
        except Conflict as exc:
            ui.flash(request, str(exc), "danger")
            return redirect(repository_scoped_path("/issues", repository, triage="all"))
        repository_id = ref.repository_id if repository == ref.repository_id else None
        ui.flash(request, f"Issue #{ref.number} 已恢复到待处理")
        return redirect(repository_scoped_path("/issues", repository_id))

    return router
