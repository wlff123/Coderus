from __future__ import annotations

import logging
import os
import weakref
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from coderus.config import Settings, load_settings
from coderus.forge import ForgeCapability
from coderus.integrations.feishu.commands import IncomingFeishuMessage
from coderus.readiness import readiness_report
from coderus.release_gate import ReleaseGate
from coderus.release_status import load_release_status
from coderus.runtime_lock import ActiveManagerLock
from coderus.security import inspect_codex_auth
from coderus.web.forge_runtime import install_forge_runtime
from coderus.web.routes.auth import build_auth_router
from coderus.web.routes.dashboard import build_dashboard_router
from coderus.web.routes.issues import build_issue_router
from coderus.web.routes.repositories import build_repository_router
from coderus.web.routes.reviews import build_review_router
from coderus.web.routes.system import build_system_router
from coderus.web.routes.tasks import build_task_router
from coderus.web.routes.users import build_user_router
from coderus.web.runtime import RuntimeComponents, build_runtime
from coderus.web.ui import WebUI, templates

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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    runtime: RuntimeComponents = app.state.runtime
    if app.state.background_enabled:
        await runtime.start()
    try:
        yield
    finally:
        if app.state.background_enabled:
            await runtime.stop()
        runtime.close()
        if app.state.manager_lock is not None:
            app.state.manager_lock.release()


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

    app = FastAPI(title="Coderus", docs_url=None, redoc_url=None, lifespan=_lifespan)
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
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and not release_gate.allows_work()
        ):
            return JSONResponse(
                {
                    "status": "unavailable",
                    "error_code": "release_draining",
                    "message": "系统正在发布新版本，请稍后重试",
                },
                status_code=503,
            )
        return await call_next(request)

    try:
        components = build_runtime(
            settings,
            state=app.state,
            release_gate=release_gate,
            repair_interrupted=runtime != "preview",
            providers=providers,
            publisher=publisher,
            github_client=github_client,
            feishu_http_client=feishu_http_client,
            feishu_gateway_factory=feishu_gateway_factory,
        )
    except BaseException:
        if manager_lock is not None:
            manager_lock.release()
        raise
    app.state.runtime = components
    sessions = components.sessions

    def forge_status() -> dict[str, dict[str, bool | str]]:
        return {
            provider: {
                "label": {"github": "GitHub", "gitcode": "GitCode"}[provider],
                "configured": app.state.forges.configured(provider),
                "publish": app.state.forges.supports(provider, ForgeCapability.PUBLISH),
            }
            for provider in ("github", "gitcode")
        }

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
                component._loop_task is not None and not component._loop_task.done()
                for component in loop_components
            )
            proxy_ready = app.state.model_proxy_app is None or (
                app.state.model_proxy_running
                and app.state.model_proxy_task is not None
                and not app.state.model_proxy_task.done()
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
            repositories=components.repository_commands,
            issue_poller=components.issue_poller,
            forge_status=forge_status,
        )
    )
    app.include_router(
        build_issue_router(
            ui=ui,
            session_factory=sessions,
            issues=components.issue_commands,
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
            tasks=components.task_commands,
            forges=app.state.forges,
            forge_status=forge_status,
            signal_cancel=lambda task_id: app.state.orchestrator.cancel(task_id),
        )
    )
    app.include_router(
        build_review_router(
            ui=ui,
            session_factory=sessions,
            reviews=components.review_commands,
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
            install_forge=lambda provider, forge: install_forge_runtime(
                app, provider, forge
            ),
        )
    )

    return app
