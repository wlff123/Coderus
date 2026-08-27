"""PR 检视列表、详情与创建路由；创建委托 ReviewCommands。"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from coderus.application import ReviewCommands, ReviewSource
from coderus.auth.security import verify_csrf_token
from coderus.models import PRReviewTask
from coderus.providers.errors import InvalidProviderUrl
from coderus.web.ui import WebUI, enabled_repository, redirect

REVIEWS_PAGE_SIZE = 20


def build_review_router(
    *,
    ui: WebUI,
    session_factory: Callable[[], Session],
    reviews: ReviewCommands,
    forge_status: Callable[[], dict[str, dict[str, bool | str]]],
    codex_auth: Callable[[], object],
) -> APIRouter:
    router = APIRouter()

    @router.get("/reviews", response_class=HTMLResponse)
    def reviews_page(
        request: Request,
        status: str = "all",
        page: int = 1,
        repository: int | None = None,
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if status not in {"all", "queued", "running", "completed", "failed"}:
                status = "all"
            selected_repository = enabled_repository(session, repository)
            filters = []
            if selected_repository is not None:
                filters.append(PRReviewTask.repository_id == selected_repository.id)
            if status == "running":
                filters.append(
                    PRReviewTask.status.in_({"preparing", "reviewing", "commenting"})
                )
            elif status != "all":
                filters.append(PRReviewTask.status == status)
            total_reviews = (
                session.scalar(
                    select(func.count()).select_from(PRReviewTask).where(*filters)
                )
                or 0
            )
            total_pages = max(
                1, (total_reviews + REVIEWS_PAGE_SIZE - 1) // REVIEWS_PAGE_SIZE
            )
            page = min(max(page, 1), total_pages)
            review_rows = session.scalars(
                select(PRReviewTask)
                .options(selectinload(PRReviewTask.repository))
                .where(*filters)
                .order_by(PRReviewTask.created_at.desc(), PRReviewTask.id.desc())
                .offset((page - 1) * REVIEWS_PAGE_SIZE)
                .limit(REVIEWS_PAGE_SIZE)
            ).all()
            return ui.templates.TemplateResponse(
                request,
                "reviews.html",
                ui.context(
                    request,
                    current,
                    reviews=review_rows,
                    status_filter=status,
                    page=page,
                    total_pages=total_pages,
                    total_reviews=total_reviews,
                    selected_repository=selected_repository,
                    selected_repository_id=(
                        selected_repository.id
                        if selected_repository is not None
                        else None
                    ),
                    pagination_query=urlencode(
                        {
                            "status": status,
                            **(
                                {"repository": selected_repository.id}
                                if selected_repository is not None
                                else {}
                            ),
                        }
                    ),
                    forge_status=forge_status(),
                    codex_auth=codex_auth(),
                ),
            )

    @router.get("/reviews/{review_id}", response_class=HTMLResponse)
    def review_detail(request: Request, review_id: int):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            review = session.scalar(
                select(PRReviewTask)
                .options(selectinload(PRReviewTask.repository))
                .where(PRReviewTask.id == review_id)
            )

            if review is None:
                return HTMLResponse("Not found", status_code=404)
            structured_result = (
                review.structured_result
                if isinstance(review.structured_result, dict)
                else {}
            )
            findings = structured_result.get("findings", [])
            if not isinstance(findings, list):
                findings = []
            findings = [finding for finding in findings if isinstance(finding, dict)]
            return ui.templates.TemplateResponse(
                request,
                "review_detail.html",
                ui.context(
                    request,
                    current,
                    review=review,
                    findings=findings,
                    forge_status=forge_status(),
                ),
            )

    @router.post("/reviews")
    def create_review(
        request: Request,
        pr_url: Annotated[str, Form()],
        csrf_token: Annotated[str, Form()],
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
        if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
            return HTMLResponse("Invalid CSRF token", status_code=400)
        auth = codex_auth()
        if not auth.ready:
            ui.flash(request, auth.detail, "danger")
            return redirect("/reviews")
        try:
            review_id = reviews.enqueue(
                pr_url,
                ReviewSource(
                    chat_id="",
                    message_id=f"web-review:{secrets.token_urlsafe(24)}",
                    sender_id=f"web-user:{current.id}",
                ),
            )
        except (InvalidProviderUrl, ValueError) as exc:
            ui.flash(request, str(exc), "error")
            return redirect("/reviews")
        return redirect(f"/reviews/{review_id}")

    return router
