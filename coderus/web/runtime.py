"""运行时装配：数据库、平台凭据、编排器与后台组件的构建和生命周期。

`build_runtime` 负责创建所有有生命周期的对象并挂到 ``app.state``；
`RuntimeComponents.start/stop/close` 拥有启动、逆序回滚和资源释放的全部逻辑。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

import httpx
import uvicorn
from sqlalchemy import select

from coderus.application import IssueCommands, ReviewCommands, TaskCommands
from coderus.application.repositories import RepositoryCommands
from coderus.assistant import ModelAssistant
from coderus.auth.service import ensure_bootstrap_admin
from coderus.config import Settings
from coderus.db import (
    create_engine_from_settings,
    create_session_factory,
    ensure_schema_compatibility,
)
from coderus.forge import ForgeRegistry, GitCodeProvider, GitHubProvider
from coderus.integrations.feishu import FeishuClient, FeishuConfig
from coderus.integrations.feishu.bot import FeishuBot
from coderus.integrations.feishu.commands import IncomingFeishuMessage
from coderus.integrations.feishu.gateway import FeishuGateway
from coderus.integrations.feishu.service import FeishuCommandService
from coderus.integrations.feishu.settings import (
    FeishuSettingsManager,
    ensure_feishu_bot_user,
)
from coderus.integrations.gitcode_credentials import GitCodeCredentialManager
from coderus.integrations.github_credentials import (
    GitHubCredentialManager,
    ResolvedGitHubCredential,
)
from coderus.issues.poller import IssuePoller
from coderus.model_proxy import CredentialBroker, create_proxy_app
from coderus.models import Base, FeishuEvent, Repository
from coderus.pr_review.orchestrator import PRReviewOrchestrator
from coderus.pr_review.scheduler import PRReviewScheduler
from coderus.pr_review.workspace import PRWorkspace
from coderus.release_gate import ReleaseGate
from coderus.runner import LocalCodexRunner, RunnerConfig, resolve_codex_command
from coderus.security import CredentialCipher
from coderus.web.forge_runtime import build_gitcode_runtime, build_github_runtime
from coderus.web.presentation import provider_error_message
from coderus.workflow.limited_runner import LimitedRunner
from coderus.workflow.notifications import FeishuTaskNotifier
from coderus.workflow.orchestrator import TaskOrchestrator
from coderus.workflow.pr_status import PRStatusPoller
from coderus.workflow.scheduler import TaskScheduler
from coderus.workflow.workspace_git import WorkspaceGit

logger = logging.getLogger(__name__)


class RuntimeComponents:
    """持有全部运行时对象；start 失败时逆序回滚已启动组件并释放自有资源。"""

    def __init__(
        self,
        *,
        state: Any,
        settings: Settings,
        engine: Any,
        sessions: Callable[..., Any],
        github_http_client: httpx.Client,
        owns_github_http_client: bool,
        feishu_http_client: httpx.Client,
        owns_feishu_http_client: bool,
        issue_commands: IssueCommands,
        review_commands: ReviewCommands,
        task_commands: TaskCommands,
        repository_commands: RepositoryCommands,
        issue_poller: IssuePoller,
        scheduler: TaskScheduler,
        pr_status_poller: PRStatusPoller,
        pr_review_scheduler: PRReviewScheduler,
        feishu_bot: FeishuBot | None,
        model_proxy_app: Any,
    ) -> None:
        self.state = state
        self.settings = settings
        self.engine = engine
        self.sessions = sessions
        self.github_http_client = github_http_client
        self.owns_github_http_client = owns_github_http_client
        self.feishu_http_client = feishu_http_client
        self.owns_feishu_http_client = owns_feishu_http_client
        self.issue_commands = issue_commands
        self.review_commands = review_commands
        self.task_commands = task_commands
        self.repository_commands = repository_commands
        self.issue_poller = issue_poller
        self.scheduler = scheduler
        self.pr_status_poller = pr_status_poller
        self.pr_review_scheduler = pr_review_scheduler
        self.feishu_bot = feishu_bot
        self.model_proxy_app = model_proxy_app
        self._proxy_server: uvicorn.Server | None = None
        self._proxy_task: asyncio.Task | None = None
        self._started_components: list[tuple[str, Callable[[], object]]] = []
        self._github_http_client_closed = False
        self._feishu_http_client_closed = False
        self._engine_disposed = False

    async def start(self) -> None:
        try:
            await self._start_model_proxy()
            for name, component in (
                ("Issue scheduler", self.state.scheduler),
                ("Issue poller", self.state.issue_poller),
                ("PR status poller", self.state.pr_status_poller),
                ("PR review scheduler", self.state.pr_review_scheduler),
            ):
                component.start()
                self._started_components.append((name, component.stop))

            if self.feishu_bot is not None:
                try:
                    self.feishu_bot.start()
                except BaseException as exc:
                    self.state.feishu_connection_error = type(exc).__name__
                    raise
                self._started_components.append(("Feishu bot", self.feishu_bot.stop))
                self.state.feishu_running = True
        except BaseException:
            await self.stop()
            self.close()
            raise

    async def stop(self) -> None:
        await self._stop_started_components()
        await self._stop_model_proxy()

    def close(self) -> None:
        if self.owns_github_http_client and not self._github_http_client_closed:
            self._github_http_client_closed = True
            self._cleanup_sync("GitHub HTTP client", self.github_http_client.close)
        if self.owns_feishu_http_client and not self._feishu_http_client_closed:
            self._feishu_http_client_closed = True
            self._cleanup_sync("Feishu HTTP client", self.feishu_http_client.close)
        if not self._engine_disposed:
            self._engine_disposed = True
            self._cleanup_sync("database engine", self.engine.dispose)

    async def _start_model_proxy(self) -> None:
        if self.model_proxy_app is None:
            return
        self._proxy_server = uvicorn.Server(
            uvicorn.Config(
                self.model_proxy_app,
                host="127.0.0.1",
                port=self.settings.codex.proxy_port,
                log_level="warning",
                access_log=False,
            )
        )
        self._proxy_server.capture_signals = lambda: nullcontext()
        self._proxy_task = asyncio.create_task(self._proxy_server.serve())
        self.state.model_proxy_task = self._proxy_task
        for _ in range(100):
            if self._proxy_server.started:
                break
            if self._proxy_task.done():
                await self._proxy_task
                raise RuntimeError("model credential proxy failed to start")
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("model credential proxy startup timed out")
        self.state.model_proxy_running = True

    async def _stop_started_components(self) -> None:
        while self._started_components:
            name, stop = self._started_components.pop()
            try:
                result = stop()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                logger.warning("%s cleanup failed: %s", name, type(exc).__name__)
            finally:
                if name == "Feishu bot":
                    self.state.feishu_running = False

    async def _stop_model_proxy(self) -> None:
        if self._proxy_server is not None:
            self._proxy_server.should_exit = True
        if self._proxy_task is not None:
            result = (await asyncio.gather(self._proxy_task, return_exceptions=True))[0]
            if isinstance(result, BaseException):
                logger.warning(
                    "model proxy cleanup observed: %s", type(result).__name__
                )
        self._proxy_server = None
        self._proxy_task = None
        self.state.model_proxy_running = False
        self.state.model_proxy_task = None

    @staticmethod
    def _cleanup_sync(name: str, cleanup: Callable[[], object]) -> None:
        try:
            cleanup()
        except BaseException as exc:
            logger.warning("%s cleanup failed: %s", name, type(exc).__name__)


def build_runtime(
    settings: Settings,
    *,
    state: Any,
    release_gate: ReleaseGate,
    repair_interrupted: bool,
    providers: Mapping[str, object] | None = None,
    publisher: object | None = None,
    github_client: httpx.Client | None = None,
    feishu_http_client: httpx.Client | None = None,
    feishu_gateway_factory: Callable[
        [str, str, Callable[[IncomingFeishuMessage], None]], object
    ]
    | None = None,
) -> RuntimeComponents:
    engine = create_engine_from_settings(settings.database)
    settings.workspace.root.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    ensure_schema_compatibility(engine)
    sessions = create_session_factory(engine)
    with sessions() as session:
        ensure_bootstrap_admin(session, "admin", settings.bootstrap_admin_password)
        if repair_interrupted:
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

    state.engine = engine
    state.sessions = sessions
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
    state.github_http_client = github_http_client
    state.github_credentials = github_credentials
    state.github_credential = resolved_github
    state.github_encryption_ready = credential_cipher is not None
    state.github_encryption_error = credential_key_error
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
    state.gitcode_credentials = gitcode_credentials
    state.gitcode_credential = resolved_gitcode
    state.gitcode_encryption_ready = credential_cipher is not None
    state.gitcode_encryption_error = credential_key_error
    feishu_cipher = None
    if settings.credential_encryption_key is not None and credential_key_error is None:
        feishu_cipher = CredentialCipher.for_feishu_app_secret(
            settings.credential_encryption_key
        )
    state.feishu_settings = FeishuSettingsManager(cipher=feishu_cipher)
    state.feishu_encryption_ready = feishu_cipher is not None
    state.feishu_encryption_error = credential_key_error
    state.feishu_restart_required = False
    state.feishu_connection_error = None
    owns_feishu_http_client = feishu_http_client is None
    feishu_http_client = feishu_http_client or httpx.Client(
        timeout=10.0,
        follow_redirects=False,
    )
    state.feishu_http_client = feishu_http_client
    github_token = (
        resolved_github.token.get_secret_value()
        if resolved_github.token is not None
        else None
    )
    gitcode_token = (
        resolved_gitcode.token.get_secret_value()
        if resolved_gitcode.token is not None
        else None
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
    state.providers = dict(providers)
    initial_forges = {}
    if publisher is not None:
        initial_forges["github"] = publisher
    elif github_runtime is not None:
        initial_forges["github"] = github_runtime.registration
    if gitcode_runtime is not None:
        initial_forges["gitcode"] = gitcode_runtime.registration
    state.forges = ForgeRegistry(initial_forges)

    issue_commands = IssueCommands(session_factory=sessions, providers=state.providers)
    review_commands = ReviewCommands(session_factory=sessions, forges=state.forges)
    task_commands = TaskCommands(session_factory=sessions, forges=state.forges)
    repository_commands = RepositoryCommands(
        session_factory=sessions,
        providers=state.providers,
        forges=state.forges,
        error_formatter=provider_error_message,
    )

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
        resolved_feishu = state.feishu_settings.resolve(session)
        if resolved_feishu.enabled:
            if resolved_feishu.app_id is None or resolved_feishu.app_secret is None:
                state.feishu_connection_error = (
                    resolved_feishu.error or "飞书配置不完整"
                )
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
                        release_gate.allows_work() and state.codex_auth.ready
                    ),
                    mutation_block_reason=lambda: (
                        state.codex_auth.detail
                        if not state.codex_auth.ready
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
                        state.feishu_connection_error = error
                        state.feishu_running = False

                    def connection_recovered() -> None:
                        state.feishu_connection_error = None
                        state.feishu_running = True

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
    state.feishu_bot = feishu_bot
    state.feishu_running = False
    state.model_proxy_running = False
    state.model_proxy_task = None
    credential_broker = None
    model_proxy_app = None
    runner_api_base = "https://api.openai.com/v1"
    if settings.model_api_key is not None:
        credential_broker = CredentialBroker(
            configured_model=settings.codex.model,
            default_ttl_seconds=settings.codex.stage_timeout_seconds + 300,
        )
        model_proxy_app = create_proxy_app(
            credential_broker,
            settings.codex.base_url,
            settings.model_api_key.get_secret_value(),
        )
        runner_api_base = f"http://127.0.0.1:{settings.codex.proxy_port}/v1"
    state.credential_broker = credential_broker
    state.model_proxy_app = model_proxy_app
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
        forges=state.forges,
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
        can_claim=lambda: release_gate.allows_work() and state.codex_auth.ready,
    )
    state.orchestrator = orchestrator
    state.scheduler = scheduler
    issue_poller = IssuePoller(
        session_factory=sessions,
        providers=state.providers,
        poll_seconds=settings.scheduler.issue_poll_seconds,
        can_run=release_gate.allows_work,
    )
    state.issue_poller = issue_poller
    pr_status_poller = PRStatusPoller(
        session_factory=sessions,
        forges=state.forges,
        poll_seconds=settings.scheduler.issue_poll_seconds,
        can_run=release_gate.allows_work,
    )
    state.pr_status_poller = pr_status_poller
    pr_review_orchestrator = PRReviewOrchestrator(
        session_factory=sessions,
        forges=state.forges,
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
        can_claim=lambda: release_gate.allows_work() and state.codex_auth.ready,
    )
    state.pr_review_orchestrator = pr_review_orchestrator
    state.pr_review_scheduler = pr_review_scheduler

    return RuntimeComponents(
        state=state,
        settings=settings,
        engine=engine,
        sessions=sessions,
        github_http_client=github_http_client,
        owns_github_http_client=owns_github_http_client,
        feishu_http_client=feishu_http_client,
        owns_feishu_http_client=owns_feishu_http_client,
        issue_commands=issue_commands,
        review_commands=review_commands,
        task_commands=task_commands,
        repository_commands=repository_commands,
        issue_poller=issue_poller,
        scheduler=scheduler,
        pr_status_poller=pr_status_poller,
        pr_review_scheduler=pr_review_scheduler,
        feishu_bot=feishu_bot,
        model_proxy_app=model_proxy_app,
    )
