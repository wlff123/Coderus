"""Web 层公共 UI 助手：模板、会话用户、CSRF 与一次性提示。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.auth.security import new_csrf_token
from coderus.models import Repository, User
from coderus.web.presentation import (
    provider_error_message,
    review_status_label,
    review_status_tone,
    role_label,
    severity_label,
    status_label,
    status_tone,
    task_failure_message,
    task_summary,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
templates.env.globals.update(
    status_label=status_label,
    status_tone=status_tone,
    task_summary=task_summary,
    role_label=role_label,
    task_failure=task_failure_message,
    severity_label=severity_label,
    provider_error=provider_error_message,
    review_status_label=review_status_label,
    review_status_tone=review_status_tone,
)


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def enabled_repository(session: Session, repository_id: int | None) -> Repository | None:
    if repository_id is None:
        return None
    return session.scalar(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.is_enabled.is_(True),
        )
    )


def repository_scoped_path(
    path: str, repository_id: int | None, **params: str
) -> str:
    if repository_id is not None:
        params["repository"] = str(repository_id)
    return f"{path}?{urlencode(params)}" if params else path


class WebUI:
    def __init__(self, template_engine: Jinja2Templates = templates) -> None:
        self.templates = template_engine

    @staticmethod
    def csrf(request: Request) -> str:
        token = request.session.get("csrf_token")
        if not token:
            token = new_csrf_token()
            request.session["csrf_token"] = token
        return token

    @staticmethod
    def current_user(request: Request, session: Session) -> User | None:
        user_id = request.session.get("user_id")
        version = request.session.get("session_version")
        if not isinstance(user_id, int):
            return None
        user = session.get(User, user_id)
        if user is None or not user.is_active or user.session_version != version:
            request.session.clear()
            return None
        return user

    @staticmethod
    def flash(request: Request, message: str, tone: str = "ok") -> None:
        request.session["flash"] = {"message": message, "tone": tone}

    def context(
        self, request: Request, current_user: User | None = None, **values: object
    ) -> dict:
        return {
            "request": request,
            "current_user": current_user,
            "csrf_token": self.csrf(request),
            "flash": request.session.pop("flash", None),
            **values,
        }
