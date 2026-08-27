"""登录、登出与个人账号路由。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from coderus.auth.security import (
    hash_password,
    new_csrf_token,
    verify_csrf_token,
    verify_password,
)
from coderus.auth.service import authenticate
from coderus.web.ui import WebUI, redirect


def build_auth_router(
    *, ui: WebUI, session_factory: Callable[[], Session]
) -> APIRouter:
    router = APIRouter()
    templates = ui.templates

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        with session_factory() as session:
            if ui.current_user(request, session):
                return redirect("/")
        return templates.TemplateResponse(request, "login.html", ui.context(request))

    @router.post("/login")
    def login(
        request: Request,
        username: str = Form(),
        password: str = Form(),
        csrf_token: str = Form(),
    ):
        if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
            return templates.TemplateResponse(
                request,
                "login.html",
                ui.context(request, error="页面已过期，请重试"),
                status_code=400,
            )
        with session_factory() as session:
            user = authenticate(session, username, password)
            if user is None:
                return templates.TemplateResponse(
                    request,
                    "login.html",
                    ui.context(request, error="用户名或密码错误"),
                    status_code=401,
                )
            request.session.clear()
            request.session.update(
                {
                    "user_id": user.id,
                    "session_version": user.session_version,
                    "csrf_token": new_csrf_token(),
                }
            )
        return redirect("/")

    @router.post("/logout")
    def logout(request: Request, csrf_token: str = Form()):
        if verify_csrf_token(request.session.get("csrf_token"), csrf_token):
            request.session.clear()
        return redirect("/login")

    @router.get("/account", response_class=HTMLResponse)
    def account_page(request: Request):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            return templates.TemplateResponse(
                request,
                "account.html",
                ui.context(request, current),
            )

    @router.post("/account/password")
    def change_own_password(
        request: Request,
        csrf_token: str = Form(),
        current_password: str = Form(),
        new_password: str = Form(min_length=8),
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            if not verify_password(current_password, current.password_hash):
                return templates.TemplateResponse(
                    request,
                    "account.html",
                    ui.context(request, current, error="当前密码错误"),
                    status_code=400,
                )
            current.password_hash = hash_password(new_password)
            current.session_version += 1
            session.commit()
            request.session["session_version"] = current.session_version
            ui.flash(request, "密码已更新")
        return redirect("/account")

    return router
