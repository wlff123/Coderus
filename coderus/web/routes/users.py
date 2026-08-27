"""用户管理路由：仅管理员可访问。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.auth.security import hash_password, verify_csrf_token
from coderus.auth.service import create_user
from coderus.models import Task, User
from coderus.tasks.statuses import RUNNING_TASK_STATES
from coderus.web.ui import WebUI, redirect
from coderus.workflow.task_state import cas_task_status


def build_user_router(
    *,
    ui: WebUI,
    session_factory: Callable[[], Session],
    cancel_running_task: Callable[[int], None],
) -> APIRouter:
    router = APIRouter()

    @router.get("/users", response_class=HTMLResponse)
    def users_page(request: Request):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            users = session.scalars(select(User).order_by(User.created_at)).all()
            return ui.templates.TemplateResponse(
                request,
                "users.html",
                ui.context(request, current, users=users),
            )

    @router.post("/users")
    def add_user(
        request: Request,
        username: str = Form(),
        password: str = Form(),
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
                user = create_user(session, username, password)
                ui.flash(request, f"用户 {user.username} 已添加")
            except ValueError as exc:
                message = {
                    "invalid user": "用户名格式无效",
                    "password too short": "密码至少需要 8 位",
                    "username already exists": "用户名已存在",
                }.get(str(exc), str(exc))
                ui.flash(request, message, "danger")
        return redirect("/users")

    @router.post("/users/{user_id}/toggle")
    def toggle_user(request: Request, user_id: int, csrf_token: str = Form()):
        running_task_ids: list[int] = []
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            target = session.get(User, user_id)
            if target is None:
                return HTMLResponse("Not found", status_code=404)
            if target.id == current.id:
                return HTMLResponse("管理员不能停用当前账号", status_code=409)
            target.is_active = not target.is_active
            target_username = target.username
            target_is_active = target.is_active
            target.session_version += 1
            if not target.is_active:
                tasks = session.scalars(
                    select(Task).where(
                        Task.created_by == target.id,
                        Task.status.in_(("queued", *RUNNING_TASK_STATES)),
                    )
                ).all()
                for task in tasks:
                    if task.status == "queued":
                        cas_task_status(
                            session,
                            task.id,
                            expected="queued",
                            new_status="cancelled",
                            updates={"finished_at": datetime.now(UTC)},
                        )
                    else:
                        if cas_task_status(
                            session,
                            task.id,
                            expected=task.status,
                            new_status="cancelling",
                        ):
                            running_task_ids.append(task.id)
            session.commit()
        for task_id in running_task_ids:
            cancel_running_task(task_id)
        ui.flash(
            request,
            f"用户 {target_username} 已{'启用' if target_is_active else '停用'}",
        )
        return redirect("/users")

    @router.post("/users/{user_id}/reset-password")
    def reset_user_password(
        request: Request,
        user_id: int,
        csrf_token: str = Form(),
        password: str = Form(min_length=8),
    ):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            target = session.get(User, user_id)
            if target is None:
                return HTMLResponse("Not found", status_code=404)
            target.password_hash = hash_password(password)
            target.session_version += 1
            target_username = target.username
            session.commit()
            ui.flash(request, f"用户 {target_username} 的密码已重置")
        return redirect("/users")

    return router
