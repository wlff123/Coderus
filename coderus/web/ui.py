"""Web 层公共 UI 助手：模板、会话用户、CSRF 与一次性提示。"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from coderus.auth.security import new_csrf_token
from coderus.models import User
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
