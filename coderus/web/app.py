from __future__ import annotations

import asyncio
import inspect
import logging
import os
import weakref
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from coderus.application import IssueCommands, ReviewCommands, TaskCommands
from coderus.assistant import ModelAssistant
from coderus.auth.service import ensure_bootstrap_admin
from coderus.config import Settings, load_settings
from coderus.db import (
    create_engine_from_settings,
    create_session_factory,
    ensure_schema_compatibility,
)
from coderus.forge import ForgeCapability, ForgeRegistry
from coderus.integrations.feishu import FeishuClient, FeishuConfig
from coderus.integrations.feishu.bot import FeishuBot
from coderus.integrations.feishu.commands import IncomingFeishuMessage
from coderus.integrations.feishu.gateway import FeishuGateway
from coderus.integrations.feishu.service import FeishuCommandService
from coderus.integrations.feishu.settings import (
    FeishuSettingsManager,
    ensure_feishu_bot_user,
)
from coderus.integrations.gitcode_credentials import (
    GitCodeCredentialManager,
)
from coderus.integrations.github_credentials import (
    GitHubCredentialManager,
    ResolvedGitHubCredential,
)
from coderus.issues.poller import IssuePoller
from coderus.model_proxy import CredentialBroker, create_proxy_app
from coderus.models import (
    Base,
    FeishuEvent,
    Repository,
)
from coderus.pr_review.orchestrator import PRReviewOrchestrator
from coderus.pr_review.scheduler import PRReviewScheduler
from coderus.pr_review.workspace import PRWorkspace
from coderus.providers import GitCodeProvider, GitHubProvider
from coderus.readiness import readiness_report
from coderus.release_gate import ReleaseGate
from coderus.release_status import load_release_status
from coderus.runner import LocalCodexRunner, RunnerConfig, resolve_codex_command
from coderus.runtime_lock import ActiveManagerLock
from coderus.security import CredentialCipher, inspect_codex_auth
from coderus.web.forge_runtime import (
    build_gitcode_runtime,
    build_github_runtime,
    install_forge_runtime,
)
from coderus.web.routes.auth import build_auth_router
from coderus.web.routes.dashboard import build_dashboard_router
from coderus.web.routes.issues import build_issue_router
from coderus.web.routes.repositories import build_repository_router
from coderus.web.routes.reviews import build_review_router
from coderus.web.routes.system import build_system_router
from coderus.web.routes.tasks import build_task_router
from coderus.web.routes.users import build_user_router
from coderus.web.ui import (
    WebUI,
    templates,
)
from coderus.workflow.limited_runner import LimitedRunner
from coderus.workflow.notifications import FeishuTaskNotifier
from coderus.workflow.orchestrator import TaskOrchestrator
from coderus.workflow.pr_status import PRStatusPoller
from coderus.workflow.scheduler import TaskScheduler
from coderus.workflow.workspace_git import WorkspaceGit

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
RuntimeMode = Literal["active", "preview", "maintenance"]
RUNTIME_MODES = frozenset({"active", "preview", "maintenance"})

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

    issue_commands = IssueCommands(
        session_factory=sessions, providers=app.state.providers
    )
    review_commands = ReviewCommands(session_factory=sessions, forges=app.state.forges)
    task_commands = TaskCommands(session_factory=sessions, forges=app.state.forges)

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
                    issues=issue_commands,
                    reviews=review_commands,
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

    ui = WebUI(templates)

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

    app.include_router(build_auth_router(ui=ui, session_factory=sessions))
    app.include_router(
        build_user_router(
            ui=ui,
            session_factory=sessions,
            cancel_running_task=lambda task_id: app.state.orchestrator.cancel(task_id),
        )
    )
    app.include_router(
        build_repository_router(
            ui=ui,
            session_factory=sessions,
            providers=app.state.providers,
            forges=app.state.forges,
            issue_poller=issue_poller,
            forge_status=forge_status,
        )
    )
    app.include_router(
        build_issue_router(
            ui=ui,
            session_factory=sessions,
            issues=issue_commands,
            forge_status=forge_status,
            codex_auth=lambda: app.state.codex_auth,
        )
    )
    app.include_router(
        build_dashboard_router(
            ui=ui, session_factory=sessions, forge_status=forge_status
        )
    )
    app.include_router(
        build_task_router(
            ui=ui,
            session_factory=sessions,
            tasks=task_commands,
            forges=app.state.forges,
            forge_status=forge_status,
            signal_cancel=lambda task_id: app.state.orchestrator.cancel(task_id),
        )
    )
    app.include_router(
        build_review_router(
            ui=ui,
            session_factory=sessions,
            reviews=review_commands,
            forge_status=forge_status,
            codex_auth=lambda: app.state.codex_auth,
        )
    )
    app.include_router(
        build_system_router(
            ui=ui,
            session_factory=sessions,
            settings=settings,
            state=app.state,
            scheduler_enabled=start_scheduler,
            forge_status=forge_status,
            install_forge=lambda provider, runtime: install_forge_runtime(
                app, provider, runtime
            ),
        )
    )

    return app
