from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
import shutil
import weakref
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from coderus.assistant import ModelAssistant
from coderus.auth.security import (
    hash_password,
    new_csrf_token,
    verify_csrf_token,
    verify_password,
)
from coderus.auth.service import authenticate, create_user, ensure_bootstrap_admin
from coderus.config import Settings, load_settings
from coderus.db import (
    create_engine_from_settings,
    create_session_factory,
    ensure_schema_compatibility,
)
from coderus.forge import ForgeCapability, ForgeNotConfigured, ForgeRegistry
from coderus.integrations.feishu import FeishuClient, FeishuConfig, FeishuRequestError
from coderus.integrations.feishu.bot import FeishuBot
from coderus.integrations.feishu.commands import IncomingFeishuMessage
from coderus.integrations.feishu.gateway import FeishuGateway
from coderus.integrations.feishu.service import FeishuCommandService
from coderus.integrations.feishu.settings import (
    FeishuSettingsManager,
    ensure_feishu_bot_user,
)
from coderus.integrations.gitcode_credentials import (
    GitCodeCredentialEncryptionUnavailable,
    GitCodeCredentialManager,
    GitCodeCredentialValidationError,
)
from coderus.integrations.github_credentials import (
    GitHubCredentialEncryptionUnavailable,
    GitHubCredentialManager,
    GitHubCredentialValidationError,
    ResolvedGitHubCredential,
)
from coderus.issues.poller import IssuePoller
from coderus.issues.service import dispatch_issue, sync_repository, upsert_provider_issue
from coderus.model_proxy import CredentialBroker, create_proxy_app
from coderus.models import (
    Base,
    FeishuEvent,
    Issue,
    PRFeedback,
    PRReviewTask,
    Repository,
    Task,
    User,
)
from coderus.pr_review.orchestrator import PRReviewOrchestrator
from coderus.pr_review.scheduler import PRReviewScheduler
from coderus.pr_review.service import enqueue_pr_review
from coderus.pr_review.workspace import PRWorkspace
from coderus.providers import GitCodeProvider, GitHubProvider
from coderus.providers.errors import InvalidProviderUrl
from coderus.providers.urls import parse_issue_url, parse_pull_request_url, parse_repository_url
from coderus.readiness import readiness_report
from coderus.release_gate import ReleaseGate
from coderus.release_status import load_release_status
from coderus.runner import LocalCodexRunner, RunnerConfig, resolve_codex_command
from coderus.runtime_lock import ActiveManagerLock
from coderus.security import CredentialCipher, inspect_codex_auth
from coderus.tasks.statuses import RUNNING_TASK_STATES
from coderus.web.forge_runtime import (
    build_gitcode_runtime,
    build_github_runtime,
    install_forge_runtime,
)
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
from coderus.workflow.feedback import upsert_pr_feedback
from coderus.workflow.limited_runner import LimitedRunner
from coderus.workflow.notifications import FeishuTaskNotifier
from coderus.workflow.orchestrator import TaskOrchestrator
from coderus.workflow.pr_status import PRStatusPoller
from coderus.workflow.scheduler import TaskScheduler
from coderus.workflow.task_state import cas_task_status
from coderus.workflow.workspace_git import WorkspaceGit

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
ISSUES_PAGE_SIZE = 25
REVIEWS_PAGE_SIZE = 20
DEFAULT_TASK_HIDDEN_STATUSES = frozenset(
    {"completed", "closed", "dismissed", "cancelled"}
)
CLOSABLE_TASK_STATUSES = frozenset(
    {"awaiting_human_review", "failed", "manual_intervention", "cancelled"}
)
RuntimeMode = Literal["active", "preview", "maintenance"]
RUNTIME_MODES = frozenset({"active", "preview", "maintenance"})

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


def create_maintenance_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="Coderus",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.runtime_mode = "maintenance"
    app.state.background_enabled = False

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "mode": settings.server.mode, "runtime": "maintenance"}
        )

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        status_code, payload = readiness_report(
            settings,
            runtime="maintenance",
            template_root=PACKAGE_ROOT / "templates",
        )
        return JSONResponse(payload, status_code=status_code)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    def maintenance_unavailable(path: str) -> JSONResponse:
        del path
        return JSONResponse(
            {"status": "maintenance", "message": "服务正在切换版本"},
            status_code=503,
        )

    return app


def create_app(
    settings: Settings | None = None,
    *,
    providers: Mapping[str, object] | None = None,
    publisher: object | None = None,
    github_client: httpx.Client | None = None,
    feishu_http_client: httpx.Client | None = None,
    feishu_gateway_factory: Callable[
        [str, str, Callable[[IncomingFeishuMessage], None]], object
    ]
    | None = None,
    start_scheduler: bool | None = None,
    runtime: RuntimeMode | None = None,
    preview_isolated: bool = False,
) -> FastAPI:
    if settings is None:
        config_path = Path(os.environ.get("CODERUS_CONFIG", "config.yaml"))
        settings = load_settings(config_path)

    if runtime is not None and runtime not in RUNTIME_MODES:
        raise ValueError(f"invalid runtime: {runtime}")
    if runtime is not None and start_scheduler is not None:
        expected_scheduler = runtime == "active"
        if start_scheduler != expected_scheduler:
            raise ValueError("runtime conflicts with start_scheduler")
    runtime_mode: RuntimeMode = runtime or (
        "active" if start_scheduler is not False else "preview"
    )
    if runtime == "preview" and not preview_isolated:
        raise ValueError("explicit preview requires isolated preview paths")
    if runtime_mode == "maintenance":
        return create_maintenance_app(settings)
    background_enabled = runtime_mode == "active"
    start_scheduler = background_enabled

    manager_lock = None
    if runtime_mode == "active":
        database_path = settings.database.path.expanduser().resolve()
        manager_lock = ActiveManagerLock(
            database_path.with_name(f"{database_path.name}.manager.lock")
        )
        manager_lock.acquire()
    try:
        engine = create_engine_from_settings(settings.database)
        settings.workspace.root.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(engine)
        ensure_schema_compatibility(engine)
        sessions = create_session_factory(engine)
        with sessions() as session:
            ensure_bootstrap_admin(session, "admin", settings.bootstrap_admin_password)
            if runtime != "preview":
                interrupted_repositories = session.scalars(
                    select(Repository).where(Repository.sync_status == "running")
                ).all()
                for repository in interrupted_repositories:
                    repository.sync_status = "failed"
                    repository.sync_started_at = None
                    repository.last_sync_error = "同步被服务重启中断，请重新刷新"
                interrupted_feishu_events = session.scalars(
                    select(FeishuEvent).where(FeishuEvent.status == "processing")
                ).all()
                for event in interrupted_feishu_events:
                    event.status = "failed"
                    event.error_summary = "服务重启中断了飞书命令"
                    event.processed_at = datetime.now(UTC)
                    event.reply_text = "命令被服务重启中断，请重新发送"
                    event.reply_status = "pending"
            session.commit()
    except BaseException:
        if manager_lock is not None:
            manager_lock.release()
        raise

    app = FastAPI(title="Coderus", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.runtime_mode = runtime_mode
    app.state.background_enabled = background_enabled
    app.state.manager_lock = manager_lock
    if manager_lock is not None:
        weakref.finalize(app, manager_lock.release)
    release_gate = ReleaseGate.from_settings(settings)
    app.state.release_gate = release_gate
    app.state.release_status = load_release_status(settings)
    app.state.codex_auth = inspect_codex_auth(settings)

    @app.middleware("http")
    async def reject_mutations_while_draining(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not release_gate.allows_work():
            return JSONResponse(
                {
                    "status": "unavailable",
                    "error_code": "release_draining",
                    "message": "系统正在发布新版本，请稍后重试",
                },
                status_code=503,
            )
        return await call_next(request)
    app.state.engine = engine
    app.state.sessions = sessions
    owns_github_http_client = github_client is None
    github_http_client = github_client or httpx.Client(timeout=20)
    credential_cipher = None
    credential_key_error = None
    if settings.credential_encryption_key is not None:
        try:
            credential_cipher = CredentialCipher(settings.credential_encryption_key)
        except ValueError:
            credential_key_error = "凭据加密密钥格式无效"
    github_credentials = GitHubCredentialManager(
        cipher=credential_cipher,
        client=github_http_client,
    )
    with sessions() as session:
        resolved_github = github_credentials.resolve(session, settings.github_token)
    if credential_key_error is not None and resolved_github.source == "error":
        resolved_github = ResolvedGitHubCredential(
            provider="github",
            account_name=resolved_github.account_name,
            token=None,
            source="error",
            updated_at=resolved_github.updated_at,
            error=credential_key_error,
        )
    app.state.github_http_client = github_http_client
    app.state.github_credentials = github_credentials
    app.state.github_credential = resolved_github
    app.state.github_encryption_ready = credential_cipher is not None
    app.state.github_encryption_error = credential_key_error
    gitcode_credentials = GitCodeCredentialManager(
        cipher=credential_cipher,
        client=github_http_client,
    )
    with sessions() as session:
        resolved_gitcode = gitcode_credentials.resolve(session, settings.gitcode_token)
    if credential_key_error is not None and resolved_gitcode.source == "error":
        resolved_gitcode = type(resolved_gitcode)(
            provider="gitcode",
            account_name=resolved_gitcode.account_name,
            token=None,
            source="error",
            updated_at=resolved_gitcode.updated_at,
            error=credential_key_error,
        )
    app.state.gitcode_credentials = gitcode_credentials
    app.state.gitcode_credential = resolved_gitcode
    app.state.gitcode_encryption_ready = credential_cipher is not None
    app.state.gitcode_encryption_error = credential_key_error
    feishu_cipher = None
    if settings.credential_encryption_key is not None and credential_key_error is None:
        feishu_cipher = CredentialCipher.for_feishu_app_secret(
            settings.credential_encryption_key
        )
    app.state.feishu_settings = FeishuSettingsManager(cipher=feishu_cipher)
    app.state.feishu_encryption_ready = feishu_cipher is not None
    app.state.feishu_encryption_error = credential_key_error
    app.state.feishu_restart_required = False
    app.state.feishu_connection_error = None
    owns_feishu_http_client = feishu_http_client is None
    feishu_http_client = feishu_http_client or httpx.Client(
        timeout=10.0,
        follow_redirects=False,
    )
    app.state.feishu_http_client = feishu_http_client
    github_token = (
        resolved_github.token.get_secret_value() if resolved_github.token is not None else None
    )
    gitcode_token = (
        resolved_gitcode.token.get_secret_value() if resolved_gitcode.token is not None else None
    )
    github_runtime = (
        build_github_runtime(
            github_token,
            client=github_http_client,
            session_factory=sessions,
        )
        if github_token is not None
        else None
    )
    gitcode_runtime = (
        build_gitcode_runtime(
            gitcode_token,
            account_name=resolved_gitcode.account_name,
            client=github_http_client,
            session_factory=sessions,
        )
        if gitcode_token is not None
        else None
    )
    if providers is None:
        providers = {
            "github": (
                github_runtime.provider_client
                if github_runtime is not None
                else GitHubProvider(client=github_http_client)
            ),
            "gitcode": (
                gitcode_runtime.provider_client
                if gitcode_runtime is not None
                else GitCodeProvider(client=github_http_client)
            ),
        }
    app.state.providers = dict(providers)
    initial_forges = {}
    if publisher is not None:
        initial_forges["github"] = publisher
    elif github_runtime is not None:
        initial_forges["github"] = github_runtime.registration
    if gitcode_runtime is not None:
        initial_forges["gitcode"] = gitcode_runtime.registration
    app.state.forges = ForgeRegistry(initial_forges)

    def forge_for_repository(repository: Repository) -> object | None:
        return app.state.forges.get(repository.provider)

    def forge_status() -> dict[str, dict[str, bool | str]]:
        return {
            provider: {
                "label": {"github": "GitHub", "gitcode": "GitCode"}[provider],
                "configured": app.state.forges.configured(provider),
                "publish": app.state.forges.supports(
                    provider, ForgeCapability.PUBLISH
                ),
            }
            for provider in ("github", "gitcode")
        }

    def enabled_repository(
        session: Session, repository_id: int | None
    ) -> Repository | None:
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

    assistant = None
    if settings.assistant.enabled and settings.model_api_key is not None:
        assistant = ModelAssistant(
            base_url=settings.codex.base_url,
            api_key=settings.model_api_key.get_secret_value(),
            model=settings.codex.model,
        )
    notifier = None
    feishu_bot = None
    feishu_client = None
    with sessions() as session:
        resolved_feishu = app.state.feishu_settings.resolve(session)
        if resolved_feishu.enabled:
            if resolved_feishu.app_id is None or resolved_feishu.app_secret is None:
                app.state.feishu_connection_error = resolved_feishu.error or "飞书配置不完整"
            else:
                ensure_feishu_bot_user(session)
                session.commit()
                feishu_client = FeishuClient(
                    FeishuConfig(
                        app_id=resolved_feishu.app_id,
                        app_secret=resolved_feishu.app_secret,
                    ),
                    http_client=feishu_http_client,
                )
                notifier = FeishuTaskNotifier(
                    session_factory=sessions,
                    default_chat_id=resolved_feishu.default_chat_id,
                )
                command_service = FeishuCommandService(
                    session_factory=sessions,
                    providers=app.state.providers,
                    forges=app.state.forges,
                    assistant=assistant,
                    can_mutate=lambda: (
                        release_gate.allows_work() and app.state.codex_auth.ready
                    ),
                    mutation_block_reason=lambda: (
                        app.state.codex_auth.detail
                        if not app.state.codex_auth.ready
                        else "系统正在发布新版本，暂不接收新任务，请稍后重试"
                    ),
                )

                def build_gateway(callback: Callable[[IncomingFeishuMessage], None]):
                    if feishu_gateway_factory is not None:
                        return feishu_gateway_factory(
                            resolved_feishu.app_id,
                            resolved_feishu.app_secret.get_secret_value(),
                            callback,
                        )

                    def connection_failed(error: str) -> None:
                        app.state.feishu_connection_error = error
                        app.state.feishu_running = False

                    def connection_recovered() -> None:
                        app.state.feishu_connection_error = None
                        app.state.feishu_running = True

                    return FeishuGateway(
                        resolved_feishu.app_id,
                        resolved_feishu.app_secret.get_secret_value(),
                        on_message=callback,
                        on_error=connection_failed,
                        on_recovered=connection_recovered,
                    )

                feishu_bot = FeishuBot(
                    service=command_service,
                    client=feishu_client,
                    gateway_factory=build_gateway,
                )
    app.state.feishu_bot = feishu_bot
    app.state.feishu_running = False
    app.state.model_proxy_running = False
    app.state.model_proxy_task = None
    credential_broker = None
    model_proxy_app = None
    runner_api_base = "https://api.openai.com/v1"
    if settings.model_api_key is not None:
        credential_broker = CredentialBroker(
            configured_model=settings.codex.model,
            default_ttl_seconds=settings.codex.stage_timeout_seconds + 300
        )
        model_proxy_app = create_proxy_app(
            credential_broker,
            settings.codex.base_url,
            settings.model_api_key.get_secret_value(),
        )
        runner_api_base = f"http://127.0.0.1:{settings.codex.proxy_port}/v1"
    app.state.credential_broker = credential_broker
    app.state.model_proxy_app = model_proxy_app
    runner = LocalCodexRunner(
        RunnerConfig(
            workspace_root=settings.workspace.root,
            codex_command=resolve_codex_command(settings.codex.binary),
            api_base_url=runner_api_base,
            model=settings.codex.model,
            network_access=settings.runner.network_access,
            sandbox_mode=settings.codex.sandbox_mode,
        )
    )
    limited_runner = LimitedRunner(runner, settings.scheduler.max_agent_processes)
    orchestrator = TaskOrchestrator(
        session_factory=sessions,
        runner=limited_runner,
        workspace_git=WorkspaceGit(settings.workspace.root),
        forges=app.state.forges,
        artifacts_root=settings.artifacts.root,
        git_user_name=settings.git.user_name,
        git_user_email=settings.git.user_email,
        stage_timeout_seconds=settings.codex.stage_timeout_seconds,
        notifier=notifier,
        credential_broker=credential_broker,
    )
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=orchestrator,
        global_limit=settings.scheduler.global_task_limit,
        per_user_limit=settings.scheduler.per_user_task_limit,
        poll_seconds=2,
        can_claim=lambda: release_gate.allows_work() and app.state.codex_auth.ready,
    )
    app.state.orchestrator = orchestrator
    app.state.scheduler = scheduler
    issue_poller = IssuePoller(
        session_factory=sessions,
        providers=app.state.providers,
        poll_seconds=settings.scheduler.issue_poll_seconds,
        can_run=release_gate.allows_work,
    )
    app.state.issue_poller = issue_poller
    pr_status_poller = PRStatusPoller(
        session_factory=sessions,
        forges=app.state.forges,
        poll_seconds=settings.scheduler.issue_poll_seconds,
        can_run=release_gate.allows_work,
    )
    app.state.pr_status_poller = pr_status_poller
    pr_review_orchestrator = PRReviewOrchestrator(
        session_factory=sessions,
        forges=app.state.forges,
        runner=limited_runner,
        workspace=PRWorkspace(settings.workspace.root),
        notifier=feishu_client,
        credential_broker=credential_broker,
        stage_timeout_seconds=settings.codex.stage_timeout_seconds,
    )
    pr_review_scheduler = PRReviewScheduler(
        session_factory=sessions,
        orchestrator=pr_review_orchestrator,
        poll_seconds=2,
        can_claim=lambda: release_gate.allows_work() and app.state.codex_auth.ready,
    )
    app.state.pr_review_orchestrator = pr_review_orchestrator
    app.state.pr_review_scheduler = pr_review_scheduler

    github_http_client_closed = False
    feishu_http_client_closed = False
    engine_disposed = False

    def close_github_http_client() -> None:
        nonlocal github_http_client_closed
        if owns_github_http_client and not github_http_client_closed:
            github_http_client_closed = True
            github_http_client.close()

    def close_feishu_http_client() -> None:
        nonlocal feishu_http_client_closed
        if owns_feishu_http_client and not feishu_http_client_closed:
            feishu_http_client_closed = True
            feishu_http_client.close()

    def dispose_engine() -> None:
        nonlocal engine_disposed
        if not engine_disposed:
            engine_disposed = True
            engine.dispose()

    if start_scheduler:
        proxy_server = None
        proxy_task = None
        started_components: list[tuple[str, Callable[[], object]]] = []

        async def stop_started_components() -> None:
            while started_components:
                name, stop = started_components.pop()
                try:
                    result = stop()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as exc:
                    logger.warning(
                        "%s cleanup failed: %s", name, type(exc).__name__
                    )
                finally:
                    if name == "Feishu bot":
                        app.state.feishu_running = False

        async def stop_model_proxy() -> None:
            nonlocal proxy_server, proxy_task
            if proxy_server is not None:
                proxy_server.should_exit = True
            if proxy_task is not None:
                result = (await asyncio.gather(proxy_task, return_exceptions=True))[0]
                if isinstance(result, BaseException):
                    logger.warning(
                        "model proxy cleanup observed: %s", type(result).__name__
                    )
            proxy_server = None
            proxy_task = None
            app.state.model_proxy_running = False
            app.state.model_proxy_task = None

        def cleanup_sync(name: str, cleanup: Callable[[], object]) -> None:
            try:
                cleanup()
            except BaseException as exc:
                logger.warning("%s cleanup failed: %s", name, type(exc).__name__)

        async def cleanup_runtime(*, close_clients: bool) -> None:
            await stop_started_components()
            await stop_model_proxy()
            if close_clients:
                cleanup_sync("GitHub HTTP client", close_github_http_client)
                cleanup_sync("Feishu HTTP client", close_feishu_http_client)
            cleanup_sync("database engine", dispose_engine)

        @app.on_event("startup")
        async def start_task_scheduler() -> None:
            nonlocal proxy_server, proxy_task
            try:
                if model_proxy_app is not None:
                    proxy_server = uvicorn.Server(
                        uvicorn.Config(
                            model_proxy_app,
                            host="127.0.0.1",
                            port=settings.codex.proxy_port,
                            log_level="warning",
                            access_log=False,
                        )
                    )
                    proxy_server.capture_signals = lambda: nullcontext()
                    proxy_task = asyncio.create_task(proxy_server.serve())
                    app.state.model_proxy_task = proxy_task
                    for _ in range(100):
                        if proxy_server.started:
                            break
                        if proxy_task.done():
                            await proxy_task
                            raise RuntimeError(
                                "model credential proxy failed to start"
                            )
                        await asyncio.sleep(0.05)
                    else:
                        raise RuntimeError(
                            "model credential proxy startup timed out"
                        )
                    app.state.model_proxy_running = True

                for name, component in (
                    ("Issue scheduler", scheduler),
                    ("Issue poller", issue_poller),
                    ("PR status poller", pr_status_poller),
                    ("PR review scheduler", pr_review_scheduler),
                ):
                    component.start()
                    started_components.append((name, component.stop))

                if feishu_bot is not None:
                    try:
                        feishu_bot.start()
                    except BaseException as exc:
                        app.state.feishu_connection_error = type(exc).__name__
                        raise
                    started_components.append(("Feishu bot", feishu_bot.stop))
                    app.state.feishu_running = True
            except BaseException:
                await cleanup_runtime(close_clients=True)
                raise

        @app.on_event("shutdown")
        async def stop_task_scheduler() -> None:
            await cleanup_runtime(close_clients=False)

    app.router.add_event_handler("shutdown", close_github_http_client)
    app.router.add_event_handler("shutdown", close_feishu_http_client)
    app.router.add_event_handler("shutdown", dispose_engine)
    if manager_lock is not None:
        app.router.add_event_handler("shutdown", manager_lock.release)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="coderus_session",
        same_site="lax",
        https_only=settings.server.mode == "public",
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")

    def csrf(request: Request) -> str:
        token = request.session.get("csrf_token")
        if not token:
            token = new_csrf_token()
            request.session["csrf_token"] = token
        return token

    def user_for(request: Request, session: Session) -> User | None:
        user_id = request.session.get("user_id")
        version = request.session.get("session_version")
        if not isinstance(user_id, int):
            return None
        user = session.get(User, user_id)
        if user is None or not user.is_active or user.session_version != version:
            request.session.clear()
            return None
        return user

    def flash(request: Request, message: str, tone: str = "ok") -> None:
        request.session["flash"] = {"message": message, "tone": tone}

    def context(request: Request, current_user: User | None = None, **values: object) -> dict:
        return {
            "request": request,
            "current_user": current_user,
            "csrf_token": csrf(request),
            "flash": request.session.pop("flash", None),
            **values,
        }

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "mode": settings.server.mode})

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        status_code, payload = readiness_report(
            settings,
            runtime=runtime_mode,
            template_root=PACKAGE_ROOT / "templates",
        )
        if status_code == 200 and runtime_mode == "active":
            loop_components = (
                app.state.scheduler,
                app.state.issue_poller,
                app.state.pr_status_poller,
                app.state.pr_review_scheduler,
            )
            loops_ready = all(
                component._loop_task is not None
                and not component._loop_task.done()
                for component in loop_components
            )
            proxy_ready = (
                app.state.model_proxy_app is None
                or (
                    app.state.model_proxy_running
                    and app.state.model_proxy_task is not None
                    and not app.state.model_proxy_task.done()
                )
            )
            feishu_ready = app.state.feishu_bot is None or (
                app.state.feishu_running and app.state.feishu_bot.is_running()
            )
            payload["checks"]["components"] = (
                "ok" if loops_ready and proxy_ready and feishu_ready else "error"
            )
            if not (loops_ready and proxy_ready and feishu_ready):
                status_code = 503
                payload["status"] = "not_ready"
                payload["error_codes"] = ["components_unavailable"]
        return JSONResponse(payload, status_code=status_code)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        with sessions() as session:
            if user_for(request, session):
                return redirect("/")
        return templates.TemplateResponse(request, "login.html", context(request))

    @app.post("/login")
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
                context(request, error="页面已过期，请重试"),
                status_code=400,
            )
        with sessions() as session:
            user = authenticate(session, username, password)
            if user is None:
                return templates.TemplateResponse(
                    request,
                    "login.html",
                    context(request, error="用户名或密码错误"),
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

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form()):
        if verify_csrf_token(request.session.get("csrf_token"), csrf_token):
            request.session.clear()
        return redirect("/login")

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            return templates.TemplateResponse(
                request,
                "account.html",
                context(request, current),
            )

    @app.post("/account/password")
    def change_own_password(
        request: Request,
        csrf_token: str = Form(),
        current_password: str = Form(),
        new_password: str = Form(min_length=8),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            if not verify_password(current_password, current.password_hash):
                return templates.TemplateResponse(
                    request,
                    "account.html",
                    context(request, current, error="当前密码错误"),
                    status_code=400,
                )
            current.password_hash = hash_password(new_password)
            current.session_version += 1
            session.commit()
            request.session["session_version"] = current.session_version
            flash(request, "密码已更新")
        return redirect("/account")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, repository: int | None = None):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            repositories = session.scalars(
                select(Repository)
                .where(Repository.is_enabled.is_(True))
                .order_by(Repository.provider, Repository.owner, Repository.name)
            ).all()
            selected_repository = enabled_repository(session, repository)
            issue_scope = (
                [Issue.repository_id == selected_repository.id]
                if selected_repository is not None
                else []
            )
            review_scope = (
                [PRReviewTask.repository_id == selected_repository.id]
                if selected_repository is not None
                else []
            )
            counts = {
                "issues": session.scalar(
                    select(func.count())
                    .select_from(Issue)
                    .where(
                        Issue.triage_state == "discovered",
                        Issue.state == "open",
                        *issue_scope,
                    )
                )
                or 0,
                "issue_tasks": session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .join(Task.issue)
                    .where(Task.status.not_in(DEFAULT_TASK_HIDDEN_STATUSES), *issue_scope)
                )
                or 0,
                "review_tasks": session.scalar(
                    select(func.count())
                    .select_from(PRReviewTask)
                    .where(PRReviewTask.status != "completed", *review_scope)
                )
                or 0,
                "attention": (
                    session.scalar(
                        select(func.count())
                        .select_from(Task)
                        .join(Task.issue)
                        .where(
                            Task.status.in_({"failed", "manual_intervention"}),
                            *issue_scope,
                        )
                    )
                    or 0
                )
                + (
                    session.scalar(
                        select(func.count())
                        .select_from(PRReviewTask)
                        .where(PRReviewTask.status == "failed", *review_scope)
                    )
                    or 0
                )
            }
            recent_issues = session.scalars(
                select(Issue)
                .options(selectinload(Issue.repository))
                .where(
                    Issue.triage_state == "discovered",
                    Issue.state == "open",
                    *issue_scope,
                )
                .order_by(Issue.source_updated_at.desc(), Issue.id.desc())
                .limit(8)
            ).all()
            issue_tasks = session.scalars(
                select(Task)
                .join(Task.issue)
                .options(
                    selectinload(Task.issue).selectinload(Issue.repository),
                    selectinload(Task.creator),
                )
                .where(Task.status.not_in(DEFAULT_TASK_HIDDEN_STATUSES), *issue_scope)
                .order_by(Task.created_at.desc())
                .limit(10)
            ).all()
            review_tasks = session.scalars(
                select(PRReviewTask)
                .options(selectinload(PRReviewTask.repository))
                .where(PRReviewTask.status != "completed", *review_scope)
                .order_by(PRReviewTask.created_at.desc())
                .limit(10)
            ).all()
            recent_tasks = [
                {
                    "key": f"RE-{task.id}",
                    "type": "Issue 处理",
                    "detail_url": f"/tasks/{task.id}",
                    "repository": task.issue.repository,
                    "target": f"#{task.issue.number} {task.issue.title}",
                    "status": task.status,
                    "status_label": status_label(task.status),
                    "status_tone": status_tone(task.status),
                    "created_at": task.created_at,
                }
                for task in issue_tasks
            ] + [
                {
                    "key": f"RV-{task.id}",
                    "type": "代码检视",
                    "detail_url": f"/reviews/{task.id}",
                    "repository": task.repository,
                    "target": f"PR #{task.pr_number}",
                    "status": task.status,
                    "status_label": review_status_label(task.status),
                    "status_tone": review_status_tone(task.status),
                    "created_at": task.created_at,
                }
                for task in review_tasks
            ]
            recent_tasks.sort(key=lambda item: item["created_at"], reverse=True)
            recent_tasks = recent_tasks[:10]
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                context(
                    request,
                    current,
                    counts=counts,
                    issues=recent_issues,
                    tasks=recent_tasks,
                    repositories=repositories,
                    selected_repository=selected_repository,
                    selected_repository_id=(
                        selected_repository.id if selected_repository is not None else None
                    ),
                    forge_status=forge_status(),
                ),
            )

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            users = session.scalars(select(User).order_by(User.created_at)).all()
            return templates.TemplateResponse(
                request,
                "users.html",
                context(request, current, users=users),
            )

    @app.get("/system", response_class=HTMLResponse)
    def system_page(request: Request):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            workspace_path = settings.workspace.root.resolve()
            disk = shutil.disk_usage(workspace_path)
            resolved_feishu = app.state.feishu_settings.resolve(session)
            checks = {
                "server_mode": settings.server.mode,
                "codex_binary": settings.codex.binary,
                "codex_auth": app.state.codex_auth,
                "feishu": resolved_feishu.enabled,
                "service_url": settings.server.public_url
                or f"http://{settings.server.bind}:{settings.server.port}",
                "database_path": str(settings.database.path.resolve()),
                "workspace_path": str(workspace_path),
                "workspace_free_gib": disk.free / (1024**3),
                "scheduler": start_scheduler,
                "running_tasks": session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.status.in_(RUNNING_TASK_STATES))
                )
                or 0,
                "queued_tasks": session.scalar(
                    select(func.count()).select_from(Task).where(Task.status == "queued")
                )
                or 0,
            }
            return templates.TemplateResponse(
                request,
                "system.html",
                context(
                    request,
                    current,
                    checks=checks,
                    release_status=app.state.release_status,
                    forge_status=forge_status(),
                    github_credential={
                        "source": app.state.github_credential.source,
                        "account_name": app.state.github_credential.account_name,
                        "updated_at": app.state.github_credential.updated_at,
                        "error": app.state.github_credential.error,
                        "encryption_ready": app.state.github_encryption_ready,
                        "encryption_error": app.state.github_encryption_error,
                    },
                    gitcode_credential={
                        "source": app.state.gitcode_credential.source,
                        "account_name": app.state.gitcode_credential.account_name,
                        "updated_at": app.state.gitcode_credential.updated_at,
                        "error": app.state.gitcode_credential.error,
                        "encryption_ready": app.state.gitcode_encryption_ready,
                        "encryption_error": app.state.gitcode_encryption_error,
                    },
                    feishu_settings={
                        "app_id": resolved_feishu.app_id,
                        "default_chat_id": resolved_feishu.default_chat_id,
                        "enabled": resolved_feishu.enabled,
                        "running": app.state.feishu_running,
                        "has_secret": resolved_feishu.app_secret is not None,
                        "updated_at": resolved_feishu.updated_at,
                        "error": resolved_feishu.error,
                        "encryption_ready": app.state.feishu_encryption_ready,
                        "encryption_error": app.state.feishu_encryption_error,
                        "restart_required": app.state.feishu_restart_required,
                        "connection_error": app.state.feishu_connection_error,
                    },
                ),
            )

    @app.post("/system/github-credential")
    def save_github_credential(
        request: Request,
        account_name: str = Form(),
        token: str = Form(),
        csrf_token: str = Form(),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            try:
                prepared = app.state.github_credentials.prepare(account_name, token)
                candidate = build_github_runtime(
                    prepared.token.get_secret_value(),
                    client=app.state.github_http_client,
                    session_factory=sessions,
                )
                stored = app.state.github_credentials.save(
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
                flash(request, str(exc), "danger")
                return redirect("/system")
            except Exception:
                session.rollback()
                flash(request, "GitHub 凭据保存失败", "danger")
                return redirect("/system")

        install_forge_runtime(app, "github", candidate)
        app.state.github_credential = ResolvedGitHubCredential(
            provider="github",
            account_name=prepared.account_name,
            token=prepared.token,
            source="database",
            updated_at=updated_at,
        )
        flash(request, "GitHub 凭据已保存")
        return redirect("/system")

    @app.post("/system/gitcode-credential")
    def save_gitcode_credential(
        request: Request,
        account_name: str = Form(),
        token: str = Form(),
        csrf_token: str = Form(),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            try:
                prepared = app.state.gitcode_credentials.prepare(account_name, token)
                candidate = build_gitcode_runtime(
                    prepared.token.get_secret_value(),
                    account_name=prepared.account_name,
                    client=app.state.github_http_client,
                    session_factory=sessions,
                )
                stored = app.state.gitcode_credentials.save(
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
                flash(request, str(exc), "danger")
                return redirect("/system")
            except Exception:
                session.rollback()
                flash(request, "GitCode 凭据保存失败", "danger")
                return redirect("/system")

        install_forge_runtime(app, "gitcode", candidate)
        app.state.gitcode_credential = type(app.state.gitcode_credential)(
            provider="gitcode",
            account_name=prepared.account_name,
            token=prepared.token,
            source="database",
            updated_at=updated_at,
        )
        flash(request, "GitCode 凭据已保存")
        return redirect("/system")

    @app.post("/system/feishu-bot")
    def save_feishu_bot_settings(
        request: Request,
        csrf_token: str = Form(),
        app_id: str = Form(""),
        app_secret: str = Form(""),
        default_chat_id: str = Form(""),
        enabled: bool = Form(False),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            try:
                prepared = app.state.feishu_settings.prepare(
                    app_id,
                    app_secret,
                    default_chat_id,
                    enabled,
                )
                resolved = app.state.feishu_settings.resolve(session)
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
                        http_client=app.state.feishu_http_client,
                    ).validate_credentials()
                app.state.feishu_settings.save(
                    session,
                    prepared,
                    updated_by=current,
                )
                if prepared.enabled:
                    ensure_feishu_bot_user(session)
                session.commit()
            except FeishuRequestError:
                session.rollback()
                flash(request, "飞书凭据验证失败", "danger")
                return redirect("/system")
            except ValueError as exc:
                session.rollback()
                flash(request, str(exc), "danger")
                return redirect("/system")
            except Exception:
                session.rollback()
                flash(request, "飞书配置保存失败", "danger")
                return redirect("/system")
        app.state.feishu_restart_required = True
        flash(request, "飞书配置已保存，重启服务后生效")
        return redirect("/system")

    @app.post("/system/feishu-bot/test")
    def test_feishu_bot(request: Request, csrf_token: str = Form()):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            resolved = app.state.feishu_settings.resolve(session)
            if (
                resolved.app_id is None
                or resolved.app_secret is None
                or resolved.default_chat_id is None
            ):
                flash(request, "请先配置 App ID、App Secret 和默认通知群", "danger")
                return redirect("/system")
            try:
                FeishuClient(
                    FeishuConfig(
                        app_id=resolved.app_id,
                        app_secret=resolved.app_secret,
                    ),
                    http_client=app.state.feishu_http_client,
                ).send_text(
                    resolved.default_chat_id,
                    "chat_id",
                    "Coderus 飞书机器人测试消息",
                )
            except FeishuRequestError:
                flash(request, "飞书测试消息发送失败", "danger")
                return redirect("/system")
        flash(request, "飞书测试消息已发送")
        return redirect("/system")

    @app.post("/users")
    def add_user(
        request: Request,
        username: str = Form(),
        password: str = Form(),
        csrf_token: str = Form(),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            try:
                user = create_user(session, username, password)
                flash(request, f"用户 {user.username} 已添加")
            except ValueError as exc:
                message = {
                    "invalid user": "用户名格式无效",
                    "password too short": "密码至少需要 8 位",
                    "username already exists": "用户名已存在",
                }.get(str(exc), str(exc))
                flash(request, message, "danger")
        return redirect("/users")

    @app.post("/users/{user_id}/toggle")
    def toggle_user(request: Request, user_id: int, csrf_token: str = Form()):
        running_task_ids: list[int] = []
        with sessions() as session:
            current = user_for(request, session)
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
            app.state.orchestrator.cancel(task_id)
        flash(
            request,
            f"用户 {target_username} 已{'启用' if target_is_active else '停用'}",
        )
        return redirect("/users")

    @app.post("/users/{user_id}/reset-password")
    def reset_user_password(
        request: Request,
        user_id: int,
        csrf_token: str = Form(),
        password: str = Form(min_length=8),
    ):
        with sessions() as session:
            current = user_for(request, session)
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
            flash(request, f"用户 {target_username} 的密码已重置")
        return redirect("/users")

    @app.get("/repositories", response_class=HTMLResponse)
    def repositories_page(request: Request):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            repositories = session.scalars(select(Repository).order_by(Repository.created_at)).all()
            return templates.TemplateResponse(
                request,
                "repositories.html",
                context(
                    request,
                    current,
                    repositories=repositories,
                    forge_status=forge_status(),
                ),
            )

    @app.post("/repositories")
    async def add_repository(
        request: Request,
        url: str = Form(),
        csrf_token: str = Form(),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            try:
                parsed = parse_repository_url(url)
                provider = app.state.providers[parsed.provider]
                metadata = await asyncio.to_thread(provider.get_repository, parsed.canonical_url)
                if metadata.is_private or metadata.issues_enabled is False:
                    raise ValueError("仓库必须公开且启用 Issue")
                fork = None
                forge = forge_for_repository(metadata)
                if app.state.forges.supports(
                    metadata.provider, ForgeCapability.ENSURE_FORK
                ):
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
                flash(request, f"仓库 {repository.owner}/{repository.name} 已添加")
            except Exception as exc:
                session.rollback()
                flash(request, provider_error_message(exc), "danger")
        return redirect("/repositories")

    @app.post("/repositories/{repository_id}/sync")
    def force_sync(request: Request, repository_id: int, csrf_token: str = Form()):
        with sessions() as session:
            current = user_for(request, session)
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
                flash(request, "仓库正在同步，请稍后刷新状态", "warning")
                return redirect("/repositories")
            try:
                sync_repository(session, repository, app.state.providers[repository.provider])
                session.commit()
                flash(request, f"{repository.owner}/{repository.name} 同步完成")
            except Exception as exc:
                repository.sync_status = "failed"
                repository.last_sync_error = provider_error_message(exc)[:1000]
                session.commit()
                flash(request, repository.last_sync_error, "danger")
        return redirect("/repositories")

    @app.post("/repositories/{repository_id}/toggle")
    def toggle_repository(request: Request, repository_id: int, csrf_token: str = Form()):
        with sessions() as session:
            current = user_for(request, session)
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
                flash(request, "仓库正在同步，当前不能修改启用状态", "warning")
                return redirect("/repositories")
            repository.is_enabled = not repository.is_enabled
            session.commit()
            flash(
                request,
                f"{repository.owner}/{repository.name} "
                f"已{'启用' if repository.is_enabled else '停用'}",
            )
        return redirect("/repositories")

    @app.post("/repositories/sync-all")
    async def force_sync_all(request: Request, csrf_token: str = Form()):
        with sessions() as session:
            current = user_for(request, session)
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
                flash(request, "已有仓库正在同步，请稍后再刷新全部", "warning")
                return redirect("/repositories")
        await issue_poller.tick()
        with sessions() as session:
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
            flash(request, f"刷新完成，{failed_count} 个仓库刷新失败", "danger")
        else:
            flash(request, "全部仓库刷新完成")
        return redirect("/repositories")

    @app.get("/issues", response_class=HTMLResponse)
    def issues_page(
        request: Request,
        triage: str = "discovered",
        page: int = 1,
        q: str = "",
        repository: int | None = None,
    ):
        with sessions() as session:
            current = user_for(request, session)
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
            total_pages = max(1, (total_issues + ISSUES_PAGE_SIZE - 1) // ISSUES_PAGE_SIZE)
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
            issues = session.scalars(statement).all()
            return templates.TemplateResponse(
                request,
                "issues.html",
                context(
                    request,
                    current,
                    issues=issues,
                    selected_triage=triage,
                    page=page,
                    total_pages=total_pages,
                    total_issues=total_issues,
                    search_query=q,
                    repositories=repositories,
                    selected_repository=selected_repository,
                    selected_repository_id=(
                        selected_repository.id if selected_repository is not None else None
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
                    codex_auth=app.state.codex_auth,
                ),
            )

    @app.post("/issues/manual")
    def add_issue_manually(
        request: Request,
        url: str = Form(),
        csrf_token: str = Form(),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            try:
                source_repository, number = parse_issue_url(url)
                repository = session.scalar(
                    select(Repository).where(
                        Repository.provider == source_repository.provider,
                        Repository.owner == source_repository.owner,
                        Repository.name == source_repository.name,
                        Repository.is_enabled.is_(True),
                    )
                )
                if repository is None:
                    raise ValueError("该 Issue 所属仓库未由管理员授权")
                provider = app.state.providers[repository.provider]
                source = provider.get_issue(source_repository, number)
                upsert_provider_issue(session, repository, source)
                session.commit()
                flash(request, f"Issue #{number} 已添加")
            except Exception as exc:
                session.rollback()
                flash(request, provider_error_message(exc), "danger")
        return redirect("/issues")

    @app.post("/issues/{issue_id}/dispatch")
    def dispatch(
        request: Request,
        issue_id: int,
        csrf_token: str = Form(),
        instructions: str = Form(default=""),
        repository: int | None = Form(default=None),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            issue = session.get(Issue, issue_id)
            if issue is None:
                return HTMLResponse("Not found", status_code=404)
            repository_id = issue.repository_id if repository == issue.repository_id else None
            try:
                task = dispatch_issue(
                    session, issue, current, instructions, commit=False
                )
            except ValueError as exc:
                flash(request, str(exc), "danger")
                return redirect(repository_scoped_path("/issues", repository_id))
            if not app.state.codex_auth.ready:
                session.rollback()
                flash(request, app.state.codex_auth.detail, "danger")
                return redirect(repository_scoped_path("/issues", repository_id))
            session.commit()
            flash(request, f"Issue #{issue.number} 已派发为 RE-{task.id}")
        return redirect(repository_scoped_path("/tasks", repository_id))

    @app.post("/issues/{issue_id}/ignore")
    def ignore_issue(
        request: Request,
        issue_id: int,
        csrf_token: str = Form(),
        reason: str = Form(default=""),
        repository: int | None = Form(default=None),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            issue = session.get(Issue, issue_id)
            if issue is None:
                return HTMLResponse("Not found", status_code=404)
            repository_id = issue.repository_id if repository == issue.repository_id else None
            if issue.triage_state != "discovered" or issue.state != "open":
                flash(request, "只有待处理 Issue 可以忽略", "danger")
                return redirect(
                    repository_scoped_path(
                        "/issues", repository_id, triage="all"
                    )
                )
            issue.triage_state = "ignored"
            issue.ignored_by = current.id
            issue.ignored_reason = reason.strip()[:1000] or None
            issue.ignored_at = datetime.now(UTC)
            session.commit()
            flash(request, f"Issue #{issue.number} 已忽略")
        return redirect(
            repository_scoped_path(
                "/issues", repository_id, triage="ignored"
            )
        )

    @app.post("/issues/{issue_id}/restore")
    def restore_issue(
        request: Request,
        issue_id: int,
        csrf_token: str = Form(),
        repository: int | None = Form(default=None),
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if current.role != "admin":
                return HTMLResponse("Forbidden", status_code=403)
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            issue = session.get(Issue, issue_id)
            if issue is None:
                return HTMLResponse("Not found", status_code=404)
            repository_id = issue.repository_id if repository == issue.repository_id else None
            if issue.triage_state != "ignored":
                flash(request, "只有已忽略 Issue 可以恢复", "danger")
                return redirect(
                    repository_scoped_path(
                        "/issues", repository_id, triage="all"
                    )
                )
            issue.triage_state = "discovered"
            issue.ignored_by = None
            issue.ignored_reason = None
            issue.ignored_at = None
            session.commit()
            flash(request, f"Issue #{issue.number} 已恢复到待处理")
        return redirect(repository_scoped_path("/issues", repository_id))

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_page(
        request: Request,
        status: str = "active",
        owner: str | None = None,
        repository: int | None = None,
    ):
        with sessions() as session:
            current = user_for(request, session)
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
                statement = statement.where(Task.status.not_in(DEFAULT_TASK_HIDDEN_STATUSES))
            elif status != "all":
                statement = statement.where(Task.status == status)
            if owner:
                statement = statement.where(User.username == owner.strip().lower())
            if selected_repository is not None:
                statement = statement.where(
                    Issue.repository_id == selected_repository.id
                )
            tasks = session.scalars(statement).all()
            users = session.scalars(select(User).order_by(User.username)).all()
            return templates.TemplateResponse(
                request,
                "tasks.html",
                context(
                    request,
                    current,
                    tasks=tasks,
                    status_filter=status,
                    owner_filter=owner or "",
                    users=users,
                    selected_repository=selected_repository,
                    selected_repository_id=(
                        selected_repository.id if selected_repository is not None else None
                    ),
                ),
            )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(request: Request, task_id: int):
        with sessions() as session:
            current = user_for(request, session)
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
            latest_agent_runs = sorted(latest_runs_by_role.values(), key=lambda item: item.id)
            latest_reviews = sorted(latest_reviews_by_role.values(), key=lambda item: item.id)
            latest_run_ids = {run.id for run in latest_agent_runs}
            latest_review_ids = {review.id for review in latest_reviews}
            can_publish_wip = bool(
                app.state.forges.supports(
                    task.issue.repository.provider, ForgeCapability.PUBLISH
                )
                and task.status in {"manual_intervention", "failed"}
                and task.commit_sha
                and task.workspace_path
                and task.branch_name
                and Path(task.workspace_path).is_dir()
            )
            return templates.TemplateResponse(
                request,
                "task_detail.html",
                context(
                    request,
                    current,
                    task=task,
                    latest_agent_runs=latest_agent_runs,
                    historical_agent_runs=[
                        run for run in task.agent_runs if run.id not in latest_run_ids
                    ],
                    latest_reviews=latest_reviews,
                    historical_reviews=[
                        review for review in task.reviews if review.id not in latest_review_ids
                    ],
                    can_publish_wip=can_publish_wip,
                    provider=task.issue.repository.provider,
                    forge_status=forge_status(),
                    can_sync_pr_feedback=bool(
                        task.status == "awaiting_human_review"
                        and task.pr_number
                        and app.state.forges.supports(
                            task.issue.repository.provider,
                            ForgeCapability.LIST_PR_FEEDBACK,
                        )
                    ),
                ),
            )

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews_page(
        request: Request,
        status: str = "all",
        page: int = 1,
        repository: int | None = None,
    ):
        with sessions() as session:
            current = user_for(request, session)
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
            reviews = session.scalars(
                select(PRReviewTask)
                .options(selectinload(PRReviewTask.repository))
                .where(*filters)
                .order_by(PRReviewTask.created_at.desc(), PRReviewTask.id.desc())
                .offset((page - 1) * REVIEWS_PAGE_SIZE)
                .limit(REVIEWS_PAGE_SIZE)
            ).all()
            return templates.TemplateResponse(
                request,
                "reviews.html",
                context(
                    request,
                    current,
                    reviews=reviews,
                    status_filter=status,
                    page=page,
                    total_pages=total_pages,
                    total_reviews=total_reviews,
                    selected_repository=selected_repository,
                    selected_repository_id=(
                        selected_repository.id if selected_repository is not None else None
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
                    codex_auth=app.state.codex_auth,
                ),
            )

    @app.get("/reviews/{review_id}", response_class=HTMLResponse)
    def review_detail(request: Request, review_id: int):
        with sessions() as session:
            current = user_for(request, session)
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
            return templates.TemplateResponse(
                request,
                "review_detail.html",
                context(
                    request,
                    current,
                    review=review,
                    findings=findings,
                    forge_status=forge_status(),
                ),
            )

    @app.post("/reviews")
    def create_review(
        request: Request,
        pr_url: Annotated[str, Form()],
        csrf_token: Annotated[str, Form()],
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            if not app.state.codex_auth.ready:
                flash(request, app.state.codex_auth.detail, "danger")
                return redirect("/reviews")
            try:
                source_repository, _ = parse_pull_request_url(pr_url)
                if not app.state.forges.supports(
                    source_repository.provider,
                    ForgeCapability.GET_PULL_REQUEST,
                    ForgeCapability.PUBLISH_PR_COMMENT,
                ):
                    raise ForgeNotConfigured(source_repository.provider)
                task = enqueue_pr_review(
                    session,
                    pr_url,
                    "",
                    f"web-review:{secrets.token_urlsafe(24)}",
                    f"web-user:{current.id}",
                )
                session.commit()
            except (InvalidProviderUrl, ValueError) as exc:
                session.rollback()
                flash(request, str(exc), "error")
                return redirect("/reviews")
            return redirect(f"/reviews/{task.id}")

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(request: Request, task_id: int, csrf_token: str = Form()):
        should_signal = False
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            task = session.get(Task, task_id)
            if task is None:
                return HTMLResponse("Not found", status_code=404)
            if current.role != "admin" and task.created_by != current.id:
                return HTMLResponse("Forbidden", status_code=403)
            if task.status == "queued":
                new_status = "cancelled"
                updates = {"finished_at": datetime.now(UTC)}
            elif task.status in RUNNING_TASK_STATES:
                new_status = "cancelling"
                updates = None
                should_signal = True
            else:
                return HTMLResponse("当前状态不能取消", status_code=409)
            if not cas_task_status(
                session,
                task_id,
                expected=task.status,
                new_status=new_status,
                updates=updates,
            ):
                session.rollback()
                return HTMLResponse("任务状态已变化，请刷新后重试", status_code=409)
            session.commit()
            flash(request, "任务取消请求已提交")
        if should_signal:
            app.state.orchestrator.cancel(task_id)
        return redirect(f"/tasks/{task_id}")

    @app.post("/tasks/{task_id}/close")
    def close_task(request: Request, task_id: int, csrf_token: str = Form()):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            task = session.get(Task, task_id)
            if task is None:
                return HTMLResponse("Not found", status_code=404)
            if current.role != "admin" and task.created_by != current.id:
                return HTMLResponse("Forbidden", status_code=403)
            if task.status not in CLOSABLE_TASK_STATUSES:
                return HTMLResponse("当前状态不能关闭", status_code=409)
            if not cas_task_status(
                session,
                task_id,
                expected=task.status,
                new_status="dismissed",
                updates={"finished_at": datetime.now(UTC)},
            ):
                session.rollback()
                return HTMLResponse("任务状态已变化，请刷新后重试", status_code=409)
            session.commit()
            flash(request, "任务已关闭")
        return redirect(f"/tasks/{task_id}")

    @app.post("/tasks/{task_id}/feedback/sync")
    async def sync_task_feedback(request: Request, task_id: int, csrf_token: str = Form()):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            task = session.scalar(
                select(Task)
                .options(selectinload(Task.issue).selectinload(Issue.repository))
                .where(Task.id == task_id)
            )
            if task is None:
                return HTMLResponse("Not found", status_code=404)
            if current.role != "admin" and task.created_by != current.id:
                return HTMLResponse("Forbidden", status_code=403)
            if task.status != "awaiting_human_review" or not task.pr_number:
                return HTMLResponse("当前任务不能同步 PR 意见", status_code=409)
            owner = task.issue.repository.owner
            name = task.issue.repository.name
            pr_number = task.pr_number
            provider = task.issue.repository.provider
        forge = forge_for_repository(task.issue.repository)
        if not app.state.forges.supports(
            provider, ForgeCapability.LIST_PR_FEEDBACK
        ):
            return HTMLResponse("发布器不支持 PR 意见同步", status_code=409)
        feedback = await forge.list_pr_feedback(owner, name, pr_number)
        pr_status = None
        if app.state.forges.supports(provider, ForgeCapability.GET_PR_STATUS):
            pr_status = await forge.get_pr_status(owner, name, pr_number)
        with sessions() as session:
            new_status = "awaiting_human_review"
            updates: dict[str, object] = {}
            if pr_status == "merged":
                new_status = "completed"
                updates = {"pr_state": "merged", "finished_at": datetime.now(UTC)}
            elif pr_status == "closed":
                new_status = "closed"
                updates = {"pr_state": "closed", "finished_at": datetime.now(UTC)}
            if not cas_task_status(
                session,
                task_id,
                expected="awaiting_human_review",
                new_status=new_status,
                updates=updates,
            ):
                session.rollback()
                return HTMLResponse("任务状态已变化，未写入本次同步", status_code=409)
            for item in feedback:
                upsert_pr_feedback(
                    session,
                    task_id=task_id,
                    provider=provider,
                    item=item,
                )
            session.commit()
            flash(request, f"已同步 {len(feedback)} 条 PR 意见")
        return redirect(f"/tasks/{task_id}")

    @app.post("/tasks/{task_id}/publish-wip")
    def publish_existing_wip(request: Request, task_id: int, csrf_token: str = Form()):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            task = session.get(Task, task_id)
            if task is None:
                return HTMLResponse("Not found", status_code=404)
            if current.role != "admin" and task.created_by != current.id:
                return HTMLResponse("Forbidden", status_code=403)
            if (
                not app.state.forges.supports(
                    task.issue.repository.provider, ForgeCapability.PUBLISH
                )
                or task.status not in {"manual_intervention", "failed"}
                or not task.commit_sha
                or not task.workspace_path
                or not task.branch_name
                or not Path(task.workspace_path).is_dir()
            ):
                return HTMLResponse("当前任务不能按现状发布", status_code=409)
            if not cas_task_status(
                session,
                task_id,
                expected=task.status,
                new_status="queued",
                updates={
                    "failure_code": "publish_existing",
                    "failure_summary": None,
                    "finished_at": None,
                },
            ):
                session.rollback()
                return HTMLResponse("任务状态已变化，请刷新后重试", status_code=409)
            session.commit()
            flash(request, "任务已重新入队，将复用现有提交发布 PR")
        return redirect(f"/tasks/{task_id}")

    @app.post("/tasks/{task_id}/feedback/handle")
    def handle_task_feedback(
        request: Request,
        task_id: int,
        csrf_token: str = Form(),
        feedback_ids: Annotated[list[int] | None, Form()] = None,
    ):
        with sessions() as session:
            current = user_for(request, session)
            if current is None:
                return redirect("/login")
            if not verify_csrf_token(request.session.get("csrf_token"), csrf_token):
                return HTMLResponse("Invalid CSRF token", status_code=400)
            task = session.get(Task, task_id)
            if task is None:
                return HTMLResponse("Not found", status_code=404)
            if current.role != "admin" and task.created_by != current.id:
                return HTMLResponse("Forbidden", status_code=403)
            if task.status != "awaiting_human_review" or not feedback_ids:
                return HTMLResponse("当前任务不能处理 PR 意见", status_code=409)
            selected_ids = set(feedback_ids)
            rows = session.scalars(
                select(PRFeedback).where(
                    PRFeedback.task_id == task_id,
                    PRFeedback.id.in_(selected_ids),
                    PRFeedback.processed_at.is_(None),
                    PRFeedback.author_association.in_(("OWNER", "MEMBER", "COLLABORATOR")),
                )
            ).all()
            if len(rows) != len(selected_ids):
                return HTMLResponse("只能处理可信维护者的未处理意见", status_code=409)
            now = datetime.now(UTC)
            if not cas_task_status(
                session,
                task_id,
                expected="awaiting_human_review",
                new_status="queued",
                updates={
                    "failure_code": "pr_feedback_revision",
                    "failure_summary": None,
                    "finished_at": None,
                },
            ):
                session.rollback()
                return HTMLResponse("任务状态已变化，请刷新后重试", status_code=409)
            for row in rows:
                row.selected_at = now
            session.commit()
        return redirect(f"/tasks/{task_id}")

    return app
