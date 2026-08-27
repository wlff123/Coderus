"""系统设置路由：平台凭据与飞书机器人配置。Token 不进入日志与模板上下文。"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coderus.auth.security import verify_csrf_token
from coderus.config import Settings
from coderus.integrations.feishu import FeishuClient, FeishuConfig, FeishuRequestError
from coderus.integrations.feishu.settings import ensure_feishu_bot_user
from coderus.integrations.gitcode_credentials import (
    GitCodeCredentialEncryptionUnavailable,
    GitCodeCredentialValidationError,
)
from coderus.integrations.github_credentials import (
    GitHubCredentialEncryptionUnavailable,
    GitHubCredentialValidationError,
    ResolvedGitHubCredential,
)
from coderus.models import Task
from coderus.tasks.statuses import RUNNING_TASK_STATES
from coderus.web.forge_runtime import build_gitcode_runtime, build_github_runtime
from coderus.web.ui import WebUI, redirect


def build_system_router(
    *,
    ui: WebUI,
    session_factory: Callable[[], Session],
    settings: Settings,
    state: Any,
    scheduler_enabled: bool,
    forge_status: Callable[[], dict[str, dict[str, bool | str]]],
    install_forge: Callable[[str, object], None],
) -> APIRouter:
    router = APIRouter()

    @router.get("/system", response_class=HTMLResponse)
    def system_page(request: Request):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            workspace_path = settings.workspace.root.resolve()
            disk = shutil.disk_usage(workspace_path)
            resolved_feishu = state.feishu_settings.resolve(session)
            checks = {
                "server_mode": settings.server.mode,
                "codex_binary": settings.codex.binary,
                "codex_auth": state.codex_auth,
                "feishu": resolved_feishu.enabled,
                "service_url": settings.server.public_url
                or f"http://{settings.server.bind}:{settings.server.port}",
                "database_path": str(settings.database.path.resolve()),
                "workspace_path": str(workspace_path),
                "workspace_free_gib": disk.free / (1024**3),
                "scheduler": scheduler_enabled,
                "running_tasks": session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.status.in_(RUNNING_TASK_STATES))
                )
                or 0,
                "queued_tasks": session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.status == "queued")
                )
                or 0,
            }
            return ui.templates.TemplateResponse(
                request,
                "system.html",
                ui.context(
                    request,
                    current,
                    checks=checks,
                    release_status=state.release_status,
                    forge_status=forge_status(),
                    github_credential={
                        "source": state.github_credential.source,
                        "account_name": state.github_credential.account_name,
                        "updated_at": state.github_credential.updated_at,
                        "error": state.github_credential.error,
                        "encryption_ready": state.github_encryption_ready,
                        "encryption_error": state.github_encryption_error,
                    },
                    gitcode_credential={
                        "source": state.gitcode_credential.source,
                        "account_name": state.gitcode_credential.account_name,
                        "updated_at": state.gitcode_credential.updated_at,
                        "error": state.gitcode_credential.error,
                        "encryption_ready": state.gitcode_encryption_ready,
                        "encryption_error": state.gitcode_encryption_error,
                    },
                    feishu_settings={
                        "app_id": resolved_feishu.app_id,
                        "default_chat_id": resolved_feishu.default_chat_id,
                        "enabled": resolved_feishu.enabled,
                        "running": state.feishu_running,
                        "has_secret": resolved_feishu.app_secret is not None,
                        "updated_at": resolved_feishu.updated_at,
                        "error": resolved_feishu.error,
                        "encryption_ready": state.feishu_encryption_ready,
                        "encryption_error": state.feishu_encryption_error,
                        "restart_required": state.feishu_restart_required,
                        "connection_error": state.feishu_connection_error,
                    },
                ),
            )

    @router.post("/system/github-credential")
    def save_github_credential(
        request: Request,
        account_name: str = Form(),
        token: str = Form(),
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
                prepared = state.github_credentials.prepare(account_name, token)
                candidate = build_github_runtime(
                    prepared.token.get_secret_value(),
                    client=state.github_http_client,
                    session_factory=session_factory,
                )
                stored = state.github_credentials.save(
                    session,
                    prepared,
                    updated_by=current,
                )
                session.commit()
                updated_at = stored.updated_at
            except (
                GitHubCredentialEncryptionUnavailable,
                GitHubCredentialValidationError,
            ) as exc:
                session.rollback()
                ui.flash(request, str(exc), "danger")
                return redirect("/system")
            except Exception:
                session.rollback()
                ui.flash(request, "GitHub 凭据保存失败", "danger")
                return redirect("/system")

        install_forge("github", candidate)
        state.github_credential = ResolvedGitHubCredential(
            provider="github",
            account_name=prepared.account_name,
            token=prepared.token,
            source="database",
            updated_at=updated_at,
        )
        ui.flash(request, "GitHub 凭据已保存")
        return redirect("/system")

    @router.post("/system/gitcode-credential")
    def save_gitcode_credential(
        request: Request,
        account_name: str = Form(),
        token: str = Form(),
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
                prepared = state.gitcode_credentials.prepare(account_name, token)
                candidate = build_gitcode_runtime(
                    prepared.token.get_secret_value(),
                    account_name=prepared.account_name,
                    client=state.github_http_client,
                    session_factory=session_factory,
                )
                stored = state.gitcode_credentials.save(
                    session,
                    prepared,
                    updated_by=current,
                )
                session.commit()
                updated_at = stored.updated_at
            except (
                GitCodeCredentialEncryptionUnavailable,
                GitCodeCredentialValidationError,
            ) as exc:
                session.rollback()
                ui.flash(request, str(exc), "danger")
                return redirect("/system")
            except Exception:
                session.rollback()
                ui.flash(request, "GitCode 凭据保存失败", "danger")
                return redirect("/system")

        install_forge("gitcode", candidate)
        state.gitcode_credential = type(state.gitcode_credential)(
            provider="gitcode",
            account_name=prepared.account_name,
            token=prepared.token,
            source="database",
            updated_at=updated_at,
        )
        ui.flash(request, "GitCode 凭据已保存")
        return redirect("/system")

    @router.post("/system/feishu-bot")
    def save_feishu_bot_settings(
        request: Request,
        csrf_token: str = Form(),
        app_id: str = Form(""),
        app_secret: str = Form(""),
        default_chat_id: str = Form(""),
        enabled: bool = Form(False),
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
                prepared = state.feishu_settings.prepare(
                    app_id,
                    app_secret,
                    default_chat_id,
                    enabled,
                )
                resolved = state.feishu_settings.resolve(session)
                candidate_secret = prepared.app_secret or resolved.app_secret
                if prepared.enabled and (
                    prepared.app_id is None or candidate_secret is None
                ):
                    raise ValueError("启用飞书机器人需要 App ID 和 App Secret")
                if prepared.app_id is not None and candidate_secret is not None:
                    FeishuClient(
                        FeishuConfig(
                            app_id=prepared.app_id,
                            app_secret=candidate_secret,
                        ),
                        http_client=state.feishu_http_client,
                    ).validate_credentials()
                state.feishu_settings.save(
                    session,
                    prepared,
                    updated_by=current,
                )
                if prepared.enabled:
                    ensure_feishu_bot_user(session)
                session.commit()
            except FeishuRequestError:
                session.rollback()
                ui.flash(request, "飞书凭据验证失败", "danger")
                return redirect("/system")
            except ValueError as exc:
                session.rollback()
                ui.flash(request, str(exc), "danger")
                return redirect("/system")
            except Exception:
                session.rollback()
                ui.flash(request, "飞书配置保存失败", "danger")
                return redirect("/system")
        state.feishu_restart_required = True
        ui.flash(request, "飞书配置已保存，重启服务后生效")
        return redirect("/system")

    @router.post("/system/feishu-bot/test")
    def test_feishu_bot(request: Request, csrf_token: str = Form()):
        with session_factory() as session:
            current = ui.current_user(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            resolved = state.feishu_settings.resolve(session)
            if (
                resolved.app_id is None
                or resolved.app_secret is None
                or resolved.default_chat_id is None
            ):
                ui.flash(request, "请先配置 App ID、App Secret 和默认通知群", "danger")
                return redirect("/system")
            try:
                FeishuClient(
                    FeishuConfig(
                        app_id=resolved.app_id,
                        app_secret=resolved.app_secret,
                    ),
                    http_client=state.feishu_http_client,
                ).send_text(
                    resolved.default_chat_id,
                    "chat_id",
                    "Coderus 飞书机器人测试消息",
                )
            except FeishuRequestError:
                ui.flash(request, "飞书测试消息发送失败", "danger")
                return redirect("/system")
        ui.flash(request, "飞书测试消息已发送")
        return redirect("/system")

    return router
