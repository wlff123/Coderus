import asyncio
import json
import os
import re
import sqlite3
from base64 import urlsafe_b64encode
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coderus.auth.service import create_user
from coderus.config import DatabaseSettings, Settings
from coderus.db import create_engine_from_settings
from coderus.forge import ForgeCapability, ForgeRegistration, GitCodeForge
from coderus.models import (
    AgentRun,
    Base,
    FeishuBotSettings,
    IntegrationCredential,
    PRReviewTask,
    Review,
    Task,
    User,
)
from coderus.models import Issue as DbIssue
from coderus.models import PRFeedback as DbPRFeedback
from coderus.models import Repository as DbRepository
from coderus.providers import GitCodeProvider
from coderus.providers.models import Issue, Repository
from coderus.publisher import PRFeedbackItem
from coderus.web.app import create_app


class FakeGitHubProvider:
    name = "github"

    def get_repository(self, url: str) -> Repository:
        assert url == "https://github.com/octo/demo"
        return Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url=url,
            default_branch="main",
            is_private=False,
            issues_enabled=True,
        )

    def list_open_issues(self, repository: Repository) -> list[Issue]:
        return [self.get_issue(repository, 1)]

    def get_issue(self, repository: Repository, number: int) -> Issue:
        return Issue(
            repository=repository,
            external_id=str(number),
            number=number,
            title="Fix failing test",
            body="Reproduction details",
            state="open",
            labels=("bug",),
            canonical_url=f"{repository.canonical_url}/issues/{number}",
            created_at=None,
            updated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )

    def parse_issue_url(self, url: str):
        return self.get_repository("https://github.com/octo/demo"), int(url.rsplit("/", 1)[1])


class FakeGitCodeProvider:
    name = "gitcode"

    def get_repository(self, url: str) -> Repository:
        assert url == "https://gitcode.com/open/demo"
        return Repository(
            provider="gitcode",
            owner="open",
            name="demo",
            canonical_url=url,
            default_branch="main",
            is_private=False,
            issues_enabled=True,
        )


class FakeReviewPublisher:
    async def get_pull_request(self, owner: str, name: str, pr_number: int):
        raise AssertionError("review should not run during application wiring tests")

    async def publish_pr_comment(
        self, owner: str, name: str, pr_number: int, body: str, marker: str
    ):
        raise AssertionError("review should not run during application wiring tests")


class FakePublishForge:
    async def publish(self, **kwargs):
        raise AssertionError("publish should not run while queueing a legacy task")


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return Settings(
        database=DatabaseSettings(path=tmp_path / "web.db"),
        workspace={"root": tmp_path / "workspaces"},
        session_secret=SecretStr("test-session-secret-that-is-long-enough"),
        bootstrap_admin_password=SecretStr("initial-password"),
    )


@pytest.fixture
def client(app_settings: Settings):
    with TestClient(
        create_app(
            app_settings,
            providers={"github": FakeGitHubProvider()},
            start_scheduler=False,
        )
    ) as client:
        yield client


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def login_as(client: TestClient, username: str, password: str) -> str:
    page = client.get("/login")
    csrf_token = csrf_from(page)
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf_token


def login(client: TestClient) -> None:
    login_as(client, "admin", "initial-password")


def enable_agent_execution(app) -> None:
    app.state.codex_auth = replace(
        app.state.codex_auth,
        ready=True,
        detail="test authentication ready",
    )


def test_healthz_is_public(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "local"}


def test_readyz_reports_runtime_and_dependency_status(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "runtime": "preview",
        "checks": {
            "database": "ok",
            "schema": "ok",
            "workspace": "ok",
            "templates": "ok",
        },
    }


def test_maintenance_runtime_does_not_initialize_database(
    app_settings: Settings,
) -> None:
    database_path = app_settings.database.path
    app_settings.workspace.root.mkdir(parents=True)
    assert database_path.exists() is False
    app = create_app(app_settings, runtime="maintenance")

    with TestClient(app) as maintenance_client:
        health = maintenance_client.get("/healthz")
        ready = maintenance_client.get("/readyz")
        login_page = maintenance_client.get("/login")
        openapi = maintenance_client.get("/openapi.json")

    assert database_path.exists() is False
    assert health.status_code == 200
    assert health.json()["runtime"] == "maintenance"
    assert ready.status_code == 503
    assert ready.json()["error_codes"] == ["database_unavailable"]
    assert login_page.status_code == 503
    assert openapi.status_code == 503


def test_maintenance_readyz_rejects_tables_with_missing_columns(
    app_settings: Settings,
) -> None:
    app_settings.workspace.root.mkdir(parents=True)
    with sqlite3.connect(app_settings.database.path) as connection:
        for table_name in Base.metadata.tables:
            connection.execute(f'CREATE TABLE "{table_name}" (id INTEGER)')

    app = create_app(app_settings, runtime="maintenance")
    with TestClient(app) as maintenance_client:
        response = maintenance_client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["error_codes"] == ["schema_incompatible"]


def test_preview_runtime_disables_background_components(
    app_settings: Settings,
) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        runtime="preview",
        preview_isolated=True,
    )

    assert app.state.runtime_mode == "preview"
    assert app.state.background_enabled is False


def test_explicit_preview_requires_isolated_runtime_confirmation(
    app_settings: Settings,
) -> None:
    with pytest.raises(ValueError, match="isolated preview"):
        create_app(app_settings, runtime="preview")


def test_explicit_preview_does_not_run_active_recovery(
    app_settings: Settings,
) -> None:
    engine = create_engine_from_settings(app_settings.database)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        owner = create_user(session, "preview-owner", "password-123", role="admin")
        session.add(
            DbRepository(
                provider="github",
                owner="octo",
                name="preview-state",
                canonical_url="https://github.com/octo/preview-state",
                default_branch="main",
                created_by=owner.id,
                sync_status="running",
            )
        )
        session.commit()
    engine.dispose()

    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        runtime="preview",
        preview_isolated=True,
    )
    with TestClient(app):
        pass

    with app.state.sessions() as session:
        repository = session.scalar(
            select(DbRepository).where(DbRepository.name == "preview-state")
        )
        assert repository is not None
        assert repository.sync_status == "running"


def test_create_app_rejects_unknown_runtime(app_settings: Settings) -> None:
    with pytest.raises(ValueError, match="invalid runtime"):
        create_app(app_settings, runtime="unknown")  # type: ignore[arg-type]


def test_active_readyz_checks_background_components(app_settings: Settings) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        runtime="active",
    )
    with TestClient(app) as active_client:
        ready = active_client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["components"] == "ok"

        scheduler_task = app.state.scheduler._loop_task
        app.state.scheduler._loop_task = None
        try:
            unavailable = active_client.get("/readyz")
        finally:
            app.state.scheduler._loop_task = scheduler_task

    assert unavailable.status_code == 503
    assert unavailable.json()["error_codes"] == ["components_unavailable"]


def test_second_active_manager_is_rejected(app_settings: Settings) -> None:
    first = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        runtime="active",
    )
    with TestClient(first):
        with pytest.raises(RuntimeError, match="already active"):
            create_app(
                app_settings,
                providers={"github": FakeGitHubProvider()},
                runtime="active",
            )

    replacement = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        runtime="active",
    )
    with TestClient(replacement):
        assert replacement.state.manager_lock.acquired is True


def test_release_drain_gate_rejects_new_post_requests(
    app_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = tmp_path / "release-draining"
    monkeypatch.setenv("CODERUS_RELEASE_GATE", str(gate))
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        runtime="preview",
        preview_isolated=True,
    )
    with TestClient(app) as drain_client:
        assert drain_client.get("/healthz").status_code == 200
        gate.touch()
        response = drain_client.post("/login", data={})

    assert response.status_code == 503
    assert response.json()["error_code"] == "release_draining"


def test_app_shares_limited_runner_with_pr_reviews(app_settings: Settings) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        publisher=FakeReviewPublisher(),
        start_scheduler=False,
    )

    assert app.state.pr_review_scheduler is not None
    assert app.state.pr_review_orchestrator is not None
    assert app.state.orchestrator.runner is app.state.pr_review_orchestrator.runner
    assert app.state.forges.configured("github") is True
    assert app.state.pr_review_orchestrator.forges is app.state.forges
    assert app.state.pr_review_orchestrator.notifier is None


def test_app_without_review_publisher_keeps_pr_review_scheduler_unavailable(
    app_settings: Settings,
) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        start_scheduler=False,
    )

    assert app.state.pr_review_orchestrator is not None
    assert app.state.pr_review_scheduler is not None
    assert app.state.forges.configured("github") is False
    assert app.state.pr_review_orchestrator.forges is app.state.forges


def test_pr_review_scheduler_follows_application_lifecycle(
    app_settings: Settings,
) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        publisher=FakeReviewPublisher(),
    )
    events: list[str] = []

    app.state.scheduler.start = lambda: events.append("issue scheduler started")
    app.state.issue_poller.start = lambda: events.append("issue poller started")
    app.state.pr_status_poller.start = lambda: events.append("PR status poller started")
    app.state.pr_review_scheduler.start = lambda: events.append(
        "PR review scheduler started"
    )

    async def stopped(component: str) -> None:
        events.append(f"{component} stopped")

    app.state.scheduler.stop = lambda: stopped("issue scheduler")
    app.state.issue_poller.stop = lambda: stopped("issue poller")
    app.state.pr_status_poller.stop = lambda: stopped("PR status poller")
    app.state.pr_review_scheduler.stop = lambda: stopped("PR review scheduler")
    close_github_client = app.state.github_http_client.close
    close_feishu_client = app.state.feishu_http_client.close

    def close_github() -> None:
        events.append("GitHub client closed")
        close_github_client()

    def close_feishu() -> None:
        events.append("Feishu client closed")
        close_feishu_client()

    app.state.github_http_client.close = close_github
    app.state.feishu_http_client.close = close_feishu

    with TestClient(app):
        assert events == [
            "issue scheduler started",
            "issue poller started",
            "PR status poller started",
            "PR review scheduler started",
        ]

    assert events == [
        "issue scheduler started",
        "issue poller started",
        "PR status poller started",
        "PR review scheduler started",
        "PR review scheduler stopped",
        "PR status poller stopped",
        "issue poller stopped",
        "issue scheduler stopped",
        "GitHub client closed",
        "Feishu client closed",
    ]


def track_app_cleanup(app, events: list[str]) -> None:
    close_github_client = app.state.github_http_client.close
    close_feishu_client = app.state.feishu_http_client.close
    dispose_engine = app.state.engine.dispose

    def close_github() -> None:
        events.append("GitHub client closed")
        close_github_client()

    def close_feishu() -> None:
        events.append("Feishu client closed")
        close_feishu_client()

    def dispose() -> None:
        events.append("engine disposed")
        dispose_engine()

    app.state.github_http_client.close = close_github
    app.state.feishu_http_client.close = close_feishu
    app.state.engine.dispose = dispose


def test_startup_component_failure_rolls_back_without_masking_original_error(
    app_settings: Settings,
) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
    )
    events: list[str] = []
    track_app_cleanup(app, events)

    app.state.scheduler.start = lambda: events.append("issue scheduler started")

    async def scheduler_stop() -> None:
        events.append("issue scheduler stopped")
        raise RuntimeError("cleanup exploded")

    def issue_poller_start() -> None:
        events.append("issue poller start failed")
        raise RuntimeError("issue poller exploded")

    app.state.scheduler.stop = scheduler_stop
    app.state.issue_poller.start = issue_poller_start

    with pytest.raises(RuntimeError, match="issue poller exploded"):
        with TestClient(app):
            pass

    assert events == [
        "issue scheduler started",
        "issue poller start failed",
        "issue scheduler stopped",
        "GitHub client closed",
        "Feishu client closed",
        "engine disposed",
    ]


def test_proxy_task_failure_cleans_owned_resources_and_preserves_error(
    app_settings: Settings,
    monkeypatch,
) -> None:
    servers = []

    class FailingProxyServer:
        def __init__(self, config) -> None:
            self.config = config
            self.started = False
            self.should_exit = False
            self.capture_signals = None
            servers.append(self)

        async def serve(self) -> None:
            await asyncio.sleep(0)
            raise RuntimeError("proxy task exploded")

    monkeypatch.setattr(uvicorn, "Server", FailingProxyServer)
    settings = app_settings.model_copy(
        update={
                "model_api_key": SecretStr("model-api-key"),
                "codex": app_settings.codex.model_copy(
                    update={
                        "base_url": "https://api.example.com/v1",
                        "model": "test-model",
                    }
                ),
        }
    )
    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
    )
    events: list[str] = []
    track_app_cleanup(app, events)

    with pytest.raises(RuntimeError, match="proxy task exploded"):
        with TestClient(app):
            pass

    assert len(servers) == 1
    assert servers[0].should_exit is True
    assert events == [
        "GitHub client closed",
        "Feishu client closed",
        "engine disposed",
    ]


def test_feishu_start_failure_rolls_back_all_started_components(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)
    api_state: dict[str, object] = {}
    api_client = feishu_api_client(api_state)
    first_app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        feishu_http_client=api_client,
        start_scheduler=False,
    )
    with TestClient(first_app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        test_client.post(
            "/system/feishu-bot",
            data={
                "app_id": "cli_failure",
                "app_secret": "runtime-secret",
                "default_chat_id": "oc_default",
                "enabled": "true",
                "csrf_token": csrf_from(page),
            },
        )
    api_client.close()

    events: list[str] = []

    class FailingGateway:
        def start(self) -> None:
            events.append("Feishu bot start failed")
            raise RuntimeError("Feishu gateway exploded")

        def stop(self) -> None:
            events.append("Feishu bot stopped")

    def gateway_factory(app_id: str, app_secret: str, callback):
        return FailingGateway()

    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        feishu_gateway_factory=gateway_factory,
    )
    track_app_cleanup(app, events)

    async def stopped(component: str) -> None:
        events.append(f"{component} stopped")

    for name, component in (
        ("issue scheduler", app.state.scheduler),
        ("issue poller", app.state.issue_poller),
        ("PR status poller", app.state.pr_status_poller),
        ("PR review scheduler", app.state.pr_review_scheduler),
    ):
        component.start = lambda name=name: events.append(f"{name} started")
        component.stop = lambda name=name: stopped(name)

    with pytest.raises(RuntimeError, match="Feishu gateway exploded"):
        with TestClient(app):
            pass

    assert app.state.feishu_running is False
    assert app.state.feishu_connection_error == "RuntimeError"
    assert events == [
        "issue scheduler started",
        "issue poller started",
        "PR status poller started",
        "PR review scheduler started",
        "Feishu bot start failed",
        "PR review scheduler stopped",
        "PR status poller stopped",
        "issue poller stopped",
        "issue scheduler stopped",
        "GitHub client closed",
        "Feishu client closed",
        "engine disposed",
    ]


def test_dashboard_redirects_anonymous_user(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_can_login_and_see_dashboard(client: TestClient) -> None:
    login(client)
    response = client.get("/")
    assert response.status_code == 200
    assert "/static/app.css?v=20260721.1" in response.text
    assert "Issue 收件箱" in response.text
    assert "任务" in response.text


def test_dashboard_filters_issues_and_both_task_types_by_repository(
    client: TestClient,
) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        alpha = DbRepository(
            provider="github",
            owner="octo",
            name="alpha",
            canonical_url="https://github.com/octo/alpha",
            default_branch="main",
            created_by_user=admin,
        )
        beta = DbRepository(
            provider="gitcode",
            owner="open",
            name="beta",
            canonical_url="https://gitcode.com/open/beta",
            default_branch="main",
            created_by_user=admin,
        )
        disabled = DbRepository(
            provider="github",
            owner="octo",
            name="disabled",
            canonical_url="https://github.com/octo/disabled",
            default_branch="main",
            created_by_user=admin,
            is_enabled=False,
        )
        alpha_inbox = DbIssue(
            repository=alpha,
            external_id="1",
            number=1,
            title="Alpha pending issue",
            body="",
            state="open",
            source_url="https://github.com/octo/alpha/issues/1",
            triage_state="discovered",
            source_updated_at=datetime(2026, 7, 21, 9, 0, tzinfo=UTC),
        )
        beta_inbox = DbIssue(
            repository=beta,
            external_id="2",
            number=2,
            title="Beta pending issue",
            body="",
            state="open",
            source_url="https://gitcode.com/open/beta/issues/2",
            triage_state="discovered",
            source_updated_at=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
        )
        alpha_active_issue = DbIssue(
            repository=alpha,
            external_id="3",
            number=3,
            title="Alpha active task",
            body="",
            state="open",
            source_url="https://github.com/octo/alpha/issues/3",
            triage_state="dispatched",
        )
        alpha_done_issue = DbIssue(
            repository=alpha,
            external_id="4",
            number=4,
            title="Alpha completed task",
            body="",
            state="open",
            source_url="https://github.com/octo/alpha/issues/4",
            triage_state="dispatched",
        )
        beta_active_issue = DbIssue(
            repository=beta,
            external_id="5",
            number=5,
            title="Beta active task",
            body="",
            state="open",
            source_url="https://gitcode.com/open/beta/issues/5",
            triage_state="dispatched",
        )
        session.add_all([alpha_inbox, beta_inbox, disabled])
        session.add_all(
            [
                Task(issue=alpha_active_issue, creator=admin, status="developer_working"),
                Task(issue=alpha_done_issue, creator=admin, status="completed"),
                Task(issue=beta_active_issue, creator=admin, status="failed"),
                PRReviewTask(
                    repository=alpha,
                    pr_number=11,
                    pr_url="https://github.com/octo/alpha/pull/11",
                    status="queued",
                    source_chat_id="chat-alpha-active",
                    source_message_id="message-alpha-active",
                    source_sender_open_id="sender-alpha-active",
                ),
                PRReviewTask(
                    repository=alpha,
                    pr_number=12,
                    pr_url="https://github.com/octo/alpha/pull/12",
                    status="completed",
                    source_chat_id="chat-alpha-done",
                    source_message_id="message-alpha-done",
                    source_sender_open_id="sender-alpha-done",
                ),
                PRReviewTask(
                    repository=beta,
                    pr_number=21,
                    pr_url="https://gitcode.com/open/beta/pulls/21",
                    status="failed",
                    source_chat_id="chat-beta",
                    source_message_id="message-beta",
                    source_sender_open_id="sender-beta",
                ),
            ]
        )
        session.commit()
        alpha_id = alpha.id
        beta_id = beta.id

    all_repositories = client.get("/")
    assert f'href="/?repository={alpha_id}"' in all_repositories.text
    assert f'href="/?repository={beta_id}"' in all_repositories.text
    assert "octo/alpha" in all_repositories.text
    assert "open/beta" in all_repositories.text
    assert "octo/disabled" not in all_repositories.text
    assert "Alpha pending issue" in all_repositories.text
    assert "Beta pending issue" in all_repositories.text
    assert "Alpha active task" in all_repositories.text
    assert "Beta active task" in all_repositories.text
    assert "PR #11" in all_repositories.text
    assert "PR #21" in all_repositories.text
    assert "Alpha completed task" not in all_repositories.text
    assert "PR #12" not in all_repositories.text

    alpha_page = client.get(f"/?repository={alpha_id}")
    assert "Alpha pending issue" in alpha_page.text
    assert "Alpha active task" in alpha_page.text
    assert "PR #11" in alpha_page.text
    assert "Beta pending issue" not in alpha_page.text
    assert "Beta active task" not in alpha_page.text
    assert "PR #21" not in alpha_page.text
    assert f'href="/issues?repository={alpha_id}"' in alpha_page.text
    assert f'href="/tasks?repository={alpha_id}"' in alpha_page.text
    assert f'href="/reviews?repository={alpha_id}"' in alpha_page.text


def add_repository_work_items(
    session,
    admin: User,
    *,
    provider: str,
    owner: str,
    name: str,
    number: int,
) -> DbRepository:
    host = "github.com" if provider == "github" else "gitcode.com"
    repository = DbRepository(
        provider=provider,
        owner=owner,
        name=name,
        canonical_url=f"https://{host}/{owner}/{name}",
        default_branch="main",
        created_by_user=admin,
    )
    inbox_issue = DbIssue(
        repository=repository,
        external_id=str(number),
        number=number,
        title=f"{name} inbox item",
        body="",
        state="open",
        source_url=f"https://{host}/{owner}/{name}/issues/{number}",
        triage_state="discovered",
    )
    task_issue = DbIssue(
        repository=repository,
        external_id=str(number + 100),
        number=number + 100,
        title=f"{name} development item",
        body="",
        state="open",
        source_url=f"https://{host}/{owner}/{name}/issues/{number + 100}",
        triage_state="dispatched",
    )
    session.add_all(
        [
            inbox_issue,
            Task(issue=task_issue, creator=admin, status="queued"),
            PRReviewTask(
                repository=repository,
                pr_number=number,
                pr_url=(
                    f"https://{host}/{owner}/{name}/pull/{number}"
                    if provider == "github"
                    else f"https://{host}/{owner}/{name}/pulls/{number}"
                ),
                status="queued",
                source_chat_id=f"chat-{name}",
                source_message_id=f"message-{name}",
                source_sender_open_id=f"sender-{name}",
            ),
        ]
    )
    return repository


def test_issue_list_filters_by_repository_and_preserves_pagination(
    client: TestClient,
) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        alpha = add_repository_work_items(
            session,
            admin,
            provider="github",
            owner="octo",
            name="filter-alpha",
            number=1,
        )
        beta = add_repository_work_items(
            session,
            admin,
            provider="gitcode",
            owner="open",
            name="filter-beta",
            number=2,
        )
        for number in range(10, 35):
            session.add(
                DbIssue(
                    repository=alpha,
                    external_id=str(number),
                    number=number,
                    title=f"filter-alpha extra {number}",
                    body="",
                    state="open",
                    source_url=f"https://github.com/octo/filter-alpha/issues/{number}",
                    triage_state="discovered",
                )
            )
        session.commit()
        alpha_id = alpha.id
        beta_id = beta.id

    all_page = client.get("/issues?q=inbox")
    assert "filter-alpha inbox item" in all_page.text
    assert "filter-beta inbox item" in all_page.text
    assert (
        'class="active" aria-current="page" '
        'href="/issues?triage=discovered&q=inbox"'
        in all_page.text
    )
    assert (
        f'href="/issues?triage=discovered&q=inbox&repository={alpha_id}"'
        in all_page.text
    )
    assert (
        f'href="/issues?triage=discovered&q=inbox&repository={beta_id}"'
        in all_page.text
    )

    page = client.get(f"/issues?repository={alpha_id}")
    assert "filter-alpha extra 34" in page.text
    assert "filter-beta inbox item" not in page.text
    assert f'name="repository" value="{alpha_id}"' in page.text
    assert '<thead><tr><th>Issue</th><th>内容</th>' in page.text
    assert page.text.index(f"repository={alpha_id}") < page.text.index(
        f"repository={beta_id}"
    )
    assert (
        f'href="/issues?triage=discovered&repository={alpha_id}&page=2"'
        in page.text
    )

    searched = client.get(
        f"/issues?repository={alpha_id}&triage=all&q=inbox"
    )
    assert (
        f'href="/issues?triage=all&q=inbox&repository={beta_id}"'
        in searched.text
    )

    beta_page = client.get(f"/issues?repository={beta_id}")
    assert beta_page.text.index(f"repository={beta_id}") < beta_page.text.index(
        f"repository={alpha_id}"
    )


def test_task_lists_filter_by_repository_and_preserve_task_tabs(
    client: TestClient,
) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        alpha = add_repository_work_items(
            session,
            admin,
            provider="github",
            owner="octo",
            name="task-alpha",
            number=31,
        )
        add_repository_work_items(
            session,
            admin,
            provider="gitcode",
            owner="open",
            name="task-beta",
            number=32,
        )
        session.commit()
        alpha_id = alpha.id

    tasks = client.get(f"/tasks?repository={alpha_id}")
    assert "task-alpha development item" in tasks.text
    assert "task-beta development item" not in tasks.text
    assert f'name="repository" value="{alpha_id}"' in tasks.text
    assert f'href="/reviews?repository={alpha_id}"' in tasks.text

    reviews = client.get(f"/reviews?repository={alpha_id}")
    assert "task-alpha" in reviews.text
    assert "task-beta" not in reviews.text
    assert "PR #31" in reviews.text
    assert f'name="repository" value="{alpha_id}"' in reviews.text
    assert f'href="/tasks?repository={alpha_id}"' in reviews.text


def test_admin_can_create_user(client: TestClient) -> None:
    login(client)
    page = client.get("/users")
    response = client.post(
        "/users",
        data={
            "username": "developer",
            "password": "developer-password",
            "csrf_token": csrf_from(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    added_page = client.get("/users")
    assert "用户 developer 已添加" in added_page.text
    assert "developer" in added_page.text

    users = client.get("/users")
    disabled = client.post(
        "/users/2/toggle",
        data={"csrf_token": csrf_from(users)},
        follow_redirects=False,
    )
    assert disabled.status_code == 303
    disabled_page = client.get("/users")
    assert "用户 developer 已停用" in disabled_page.text
    assert "停用" in disabled_page.text

    reset = client.post(
        "/users/2/reset-password",
        data={"csrf_token": csrf_from(client.get("/users")), "password": "new-password"},
        follow_redirects=False,
    )
    assert reset.status_code == 303
    assert "用户 developer 的密码已重置" in client.get("/users").text


def test_admin_cannot_create_user_with_short_password(client: TestClient) -> None:
    login(client)
    page = client.get("/users")
    response = client.post(
        "/users",
        data={
            "username": "short-password-user",
            "password": "short",
            "csrf_token": csrf_from(page),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    users_page = client.get("/users")
    assert "密码至少需要 8 位" in users_page.text
    assert "short-password-user" not in users_page.text


def test_account_password_change_shows_feedback(client: TestClient) -> None:
    login(client)
    page = client.get("/account")
    response = client.post(
        "/account/password",
        data={
            "csrf_token": csrf_from(page),
            "current_password": "initial-password",
            "new_password": "new-password",
        },
    )
    assert "密码已更新" in response.text


def test_repository_sync_and_issue_dispatch_flow(client: TestClient) -> None:
    enable_agent_execution(client.app)
    login(client)
    repositories = client.get("/repositories")
    added = client.post(
        "/repositories",
        data={
            "url": "https://github.com/octo/demo",
            "csrf_token": csrf_from(repositories),
        },
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert "octo/demo" in client.get("/repositories").text

    synced = client.post(
        "/repositories/1/sync",
        data={"csrf_token": csrf_from(client.get("/repositories"))},
        follow_redirects=False,
    )
    assert synced.status_code == 303
    issues = client.get("/issues?repository=1")
    assert "Fix failing test" in issues.text

    ignored = client.post(
        "/issues/1/ignore",
        data={
            "csrf_token": csrf_from(issues),
            "reason": "Not actionable",
            "repository": "1",
        },
        follow_redirects=False,
    )
    assert ignored.status_code == 303
    assert ignored.headers["location"] == "/issues?triage=ignored&repository=1"
    ignored_page = client.get(ignored.headers["location"])
    assert "Fix failing test" in ignored_page.text
    restored = client.post(
        "/issues/1/restore",
        data={"csrf_token": csrf_from(ignored_page), "repository": "1"},
        follow_redirects=False,
    )
    assert restored.status_code == 303
    assert restored.headers["location"] == "/issues?repository=1"
    issues = client.get(restored.headers["location"])

    dispatched = client.post(
        "/issues/1/dispatch",
        data={
            "csrf_token": csrf_from(issues),
            "instructions": "Run the focused test first",
            "repository": "1",
        },
        follow_redirects=False,
    )
    assert dispatched.status_code == 303
    assert dispatched.headers["location"] == "/tasks?repository=1"
    tasks = client.get(dispatched.headers["location"])
    assert "RE-1" in tasks.text
    assert "Fix failing test" in tasks.text
    detail = client.get("/tasks/1")
    assert detail.status_code == 200
    assert "执行进度" in detail.text


def test_unavailable_codex_auth_blocks_issue_dispatch(client: TestClient) -> None:
    login(client)
    repositories = client.get("/repositories")
    client.post(
        "/repositories",
        data={
            "url": "https://github.com/octo/demo",
            "csrf_token": csrf_from(repositories),
        },
    )
    client.post(
        "/repositories/1/sync",
        data={"csrf_token": csrf_from(client.get("/repositories"))},
    )
    issues = client.get("/issues?repository=1")

    response = client.post(
        "/issues/1/dispatch",
        data={"csrf_token": csrf_from(issues), "repository": "1"},
    )

    assert "Agent 执行已阻止" in response.text
    with client.app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_pr_task_page_shows_human_review_handoff(client: TestClient) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="review-demo",
            canonical_url="https://github.com/octo/review-demo",
            default_branch="main",
            created_by_user=admin,
        )
        issue = DbIssue(
            repository=repository,
            external_id="2",
            number=2,
            title="Review generated change",
            body="Reproduction",
            state="open",
            source_url="https://github.com/octo/review-demo/issues/2",
            triage_state="dispatched",
        )
        task = Task(
            issue=issue,
            creator=admin,
            status="awaiting_human_review",
            base_commit_sha="a" * 40,
            branch_name="coderus/issue-2-1",
            pr_url="https://github.com/octo/review-demo/pull/3",
            pr_number=3,
            pr_state="open",
        )
        session.add(task)
        session.flush()
        session.add_all(
            [
                AgentRun(task_id=task.id, role="developer", attempt=1, status="succeeded"),
                AgentRun(task_id=task.id, role="developer", attempt=2, status="succeeded"),
                Review(
                    task_id=task.id,
                    reviewer_role="reviewer_a",
                    decision="changes_requested",
                    findings=[{"severity": "low", "message": "older finding"}],
                    blocking_count=1,
                ),
                Review(
                    task_id=task.id,
                    reviewer_role="reviewer_a",
                    decision="changes_requested",
                    findings=[{"severity": "medium", "message": "check edge case"}],
                    blocking_count=1,
                ),
            ]
        )
        session.commit()
        task_id = task.id

    detail = client.get(f"/tasks/{task_id}")
    assert detail.status_code == 200
    assert "WIP" not in detail.text
    assert "PR 已提交 #3" in detail.text
    assert "开发与自测" in detail.text
    assert "代码检视" in detail.text
    assert "集中修正" in detail.text
    assert "提交PR" in detail.text
    assert "check edge case" in detail.text
    current_runs = detail.text.split("当前 Agent 运行", 1)[1].split("</section>", 1)[0]
    latest_reviews = detail.text.split("最新检视结论", 1)[1].split("</section>", 1)[0]
    assert current_runs.count("开发 Agent") == 1
    assert ">2</td>" in current_runs
    assert "older finding" not in latest_reviews
    assert "历史记录（1 次运行，1 次检视）" in detail.text
    assert "主管分析" not in detail.text
    assert "提交确认" not in detail.text
    assert "setTimeout" not in detail.text


def test_task_creator_can_sync_and_queue_trusted_pr_feedback(app_settings: Settings) -> None:
    class FeedbackPublisher:
        async def list_pr_feedback(self, owner, name, number):
            assert (owner, name, number) == ("octo", "review-demo", 3)
            return [
                PRFeedbackItem(
                    "issue_comment:10", "issue_comment", "maintainer", "MEMBER",
                    "please add a regression test", "https://github.com/octo/review-demo/pull/3#issuecomment-10"
                ),
                PRFeedbackItem(
                    "issue_comment:11", "issue_comment", "visitor", "NONE",
                    "untrusted suggestion", "https://github.com/octo/review-demo/pull/3#issuecomment-11"
                ),
            ]

    with TestClient(
        create_app(
            app_settings,
            providers={"github": FakeGitHubProvider()},
            publisher=ForgeRegistration(
                FeedbackPublisher(),
                frozenset({ForgeCapability.LIST_PR_FEEDBACK}),
            ),
            start_scheduler=False,
        )
    ) as test_client:
        login(test_client)
        with test_client.app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            repository = DbRepository(
                provider="github", owner="octo", name="review-demo",
                canonical_url="https://github.com/octo/review-demo", default_branch="main",
                created_by_user=admin,
            )
            issue = DbIssue(
                repository=repository, external_id="3", number=3, title="Review feedback",
                body="Reproduction", state="open",
                source_url="https://github.com/octo/review-demo/issues/3",
                triage_state="dispatched",
            )
            task = Task(
                issue=issue, creator=admin, status="awaiting_human_review",
                workspace_path=str(app_settings.workspace.root / "task-1"),
                branch_name="coderus/issue-3-1", pr_url="https://github.com/octo/review-demo/pull/3",
                pr_number=3, pr_state="open",
            )
            session.add(task)
            session.commit()
            task_id = task.id

        page = test_client.get(f"/tasks/{task_id}")
        synced = test_client.post(
            f"/tasks/{task_id}/feedback/sync",
            data={"csrf_token": csrf_from(page)},
            follow_redirects=False,
        )
        assert synced.status_code == 303
        detail = test_client.get(f"/tasks/{task_id}")
        assert "please add a regression test" in detail.text
        assert "untrusted suggestion" in detail.text
        repeated = test_client.post(
            f"/tasks/{task_id}/feedback/sync",
            data={"csrf_token": csrf_from(detail)},
            follow_redirects=False,
        )
        assert repeated.status_code == 303
        with test_client.app.state.sessions() as session:
            assert session.scalar(select(func.count()).select_from(DbPRFeedback)) == 2

        rejected = test_client.post(
            f"/tasks/{task_id}/feedback/handle",
            data={"csrf_token": csrf_from(detail), "feedback_ids": "2"},
            follow_redirects=False,
        )
        assert rejected.status_code == 409

        queued = test_client.post(
            f"/tasks/{task_id}/feedback/handle",
            data={"csrf_token": csrf_from(detail), "feedback_ids": "1"},
            follow_redirects=False,
        )
        assert queued.status_code == 303
        with test_client.app.state.sessions() as session:
            task = session.get(Task, task_id)
            assert task.status == "queued"
            assert task.failure_code == "pr_feedback_revision"


def test_feedback_sync_cas_rejects_state_changed_during_remote_call(
    app_settings: Settings,
) -> None:
    runtime: dict[str, object] = {}

    class InterleavingForge:
        async def list_pr_feedback(self, owner, name, number):
            with runtime["sessions"]() as session:
                task = session.get(Task, runtime["task_id"])
                task.status = "queued"
                session.commit()
            return [
                PRFeedbackItem(
                    "issue_comment:10",
                    "issue_comment",
                    "maintainer",
                    "MEMBER",
                    "stale feedback",
                    "https://github.com/octo/race-demo/pull/3#issuecomment-10",
                )
            ]

        async def get_pr_status(self, owner, name, number):
            return "merged"

    with TestClient(
        create_app(
            app_settings,
            providers={"github": FakeGitHubProvider()},
            publisher=InterleavingForge(),
            start_scheduler=False,
        )
    ) as test_client:
        login(test_client)
        runtime["sessions"] = test_client.app.state.sessions
        with test_client.app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            repository = DbRepository(
                provider="github",
                owner="octo",
                name="race-demo",
                canonical_url="https://github.com/octo/race-demo",
                default_branch="main",
                created_by_user=admin,
            )
            task = Task(
                issue=DbIssue(
                    repository=repository,
                    external_id="3",
                    number=3,
                    title="Feedback race",
                    body="Reproduction",
                    state="open",
                    source_url="https://github.com/octo/race-demo/issues/3",
                    triage_state="dispatched",
                ),
                creator=admin,
                status="awaiting_human_review",
                pr_url="https://github.com/octo/race-demo/pull/3",
                pr_number=3,
                pr_state="open",
            )
            session.add(task)
            session.commit()
            runtime["task_id"] = task.id

        page = test_client.get(f"/tasks/{runtime['task_id']}")
        response = test_client.post(
            f"/tasks/{runtime['task_id']}/feedback/sync",
            data={"csrf_token": csrf_from(page)},
            follow_redirects=False,
        )

        assert response.status_code == 409
        with test_client.app.state.sessions() as session:
            task = session.get(Task, runtime["task_id"])
            assert task.status == "queued"
            assert session.scalar(select(func.count()).select_from(DbPRFeedback)) == 0


def test_feedback_sync_upgrades_legacy_github_feedback_in_place(app_settings: Settings) -> None:
    selected_at = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    processed_at = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)

    class FeedbackForge:
        async def list_pr_feedback(self, owner, name, number):
            assert (owner, name, number) == ("octo", "legacy-demo", 3)
            return [
                PRFeedbackItem(
                    "issue_comment:10",
                    "issue_comment",
                    "maintainer",
                    "MEMBER",
                    "updated remote feedback",
                    "https://github.com/octo/legacy-demo/pull/3#issuecomment-10",
                )
            ]

    with TestClient(
        create_app(
            app_settings,
            providers={"github": FakeGitHubProvider()},
            publisher=ForgeRegistration(
                FeedbackForge(),
                frozenset({ForgeCapability.LIST_PR_FEEDBACK}),
            ),
            start_scheduler=False,
        )
    ) as test_client:
        login(test_client)
        with test_client.app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            repository = DbRepository(
                provider="github",
                owner="octo",
                name="legacy-demo",
                canonical_url="https://github.com/octo/legacy-demo",
                default_branch="main",
                created_by_user=admin,
            )
            task = Task(
                issue=DbIssue(
                    repository=repository,
                    external_id="3",
                    number=3,
                    title="Legacy feedback",
                    body="Reproduction",
                    state="open",
                    source_url="https://github.com/octo/legacy-demo/issues/3",
                    triage_state="dispatched",
                ),
                creator=admin,
                status="awaiting_human_review",
                pr_url="https://github.com/octo/legacy-demo/pull/3",
                pr_number=3,
                pr_state="open",
            )
            session.add(task)
            session.flush()
            legacy = DbPRFeedback(
                task_id=task.id,
                provider_id="issue_comment:10",
                kind="issue_comment",
                author="old-maintainer",
                author_association="MEMBER",
                body="outdated local feedback",
                url="https://example.invalid/old",
                path="old.py",
                line=1,
                selected_at=selected_at,
                processed_at=processed_at,
            )
            session.add(legacy)
            session.commit()
            task_id = task.id
            legacy_id = legacy.id

        page = test_client.get(f"/tasks/{task_id}")
        response = test_client.post(
            f"/tasks/{task_id}/feedback/sync",
            data={"csrf_token": csrf_from(page)},
            follow_redirects=False,
        )

        assert response.status_code == 303
        with test_client.app.state.sessions() as session:
            rows = session.scalars(
                select(DbPRFeedback).where(DbPRFeedback.task_id == task_id)
            ).all()
            assert len(rows) == 1
            feedback = rows[0]
            assert feedback.id == legacy_id
            assert feedback.provider_id == "github:issue_comment:10"
            assert feedback.selected_at == selected_at.replace(tzinfo=None)
            assert feedback.processed_at == processed_at.replace(tzinfo=None)
            assert feedback.body == "updated remote feedback"
            assert feedback.url.endswith("issuecomment-10")
            assert feedback.path is None
            assert feedback.line is None


def test_feedback_sync_merges_prefixed_and_legacy_github_feedback(app_settings: Settings) -> None:
    legacy_selected_at = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    legacy_processed_at = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
    prefixed_selected_at = datetime(2026, 7, 16, 8, 30, tzinfo=UTC)
    prefixed_processed_at = datetime(2026, 7, 16, 9, 30, tzinfo=UTC)

    class FeedbackForge:
        async def list_pr_feedback(self, owner, name, number):
            assert (owner, name, number) == ("octo", "partial-upgrade-demo", 3)
            return [
                PRFeedbackItem(
                    "issue_comment:10",
                    "issue_comment",
                    "maintainer",
                    "MEMBER",
                    "remote feedback after convergence",
                    "https://github.com/octo/partial-upgrade-demo/pull/3#issuecomment-10",
                )
            ]

    with TestClient(
        create_app(
            app_settings,
            providers={"github": FakeGitHubProvider()},
            publisher=ForgeRegistration(
                FeedbackForge(),
                frozenset({ForgeCapability.LIST_PR_FEEDBACK}),
            ),
            start_scheduler=False,
        )
    ) as test_client:
        login(test_client)
        with test_client.app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            repository = DbRepository(
                provider="github",
                owner="octo",
                name="partial-upgrade-demo",
                canonical_url="https://github.com/octo/partial-upgrade-demo",
                default_branch="main",
                created_by_user=admin,
            )
            task = Task(
                issue=DbIssue(
                    repository=repository,
                    external_id="3",
                    number=3,
                    title="Partial upgrade feedback",
                    body="Reproduction",
                    state="open",
                    source_url="https://github.com/octo/partial-upgrade-demo/issues/3",
                    triage_state="dispatched",
                ),
                creator=admin,
                status="awaiting_human_review",
                pr_url="https://github.com/octo/partial-upgrade-demo/pull/3",
                pr_number=3,
                pr_state="open",
            )
            session.add(task)
            session.flush()
            legacy = DbPRFeedback(
                task_id=task.id,
                provider_id="issue_comment:10",
                kind="issue_comment",
                author="legacy-maintainer",
                author_association="MEMBER",
                body="legacy body",
                url="https://example.invalid/legacy",
                path=None,
                line=None,
                selected_at=legacy_selected_at,
                processed_at=legacy_processed_at,
            )
            prefixed = DbPRFeedback(
                task_id=task.id,
                provider_id="github:issue_comment:10",
                kind="issue_comment",
                author="prefixed-maintainer",
                author_association="MEMBER",
                body="prefixed body",
                url="https://example.invalid/prefixed",
                path=None,
                line=None,
                selected_at=prefixed_selected_at,
                processed_at=prefixed_processed_at,
            )
            session.add_all([legacy, prefixed])
            session.commit()
            task_id = task.id
            prefixed_id = prefixed.id

        page = test_client.get(f"/tasks/{task_id}")
        response = test_client.post(
            f"/tasks/{task_id}/feedback/sync",
            data={"csrf_token": csrf_from(page)},
            follow_redirects=False,
        )
        detail = test_client.get(f"/tasks/{task_id}")

        assert response.status_code == 303
        assert detail.text.count("remote feedback after convergence") == 1
        with test_client.app.state.sessions() as session:
            rows = session.scalars(
                select(DbPRFeedback).where(DbPRFeedback.task_id == task_id)
            ).all()
            assert len(rows) == 1
            feedback = rows[0]
            assert feedback.id == prefixed_id
            assert feedback.provider_id == "github:issue_comment:10"
            assert feedback.selected_at == legacy_selected_at.replace(tzinfo=None)
            assert feedback.processed_at == legacy_processed_at.replace(tzinfo=None)
            assert feedback.body == "remote feedback after convergence"
            assert feedback.url.endswith("issuecomment-10")


def test_gitcode_repository_fork_and_task_actions_use_its_forge(app_settings: Settings) -> None:
    class GitCodeForge:
        def __init__(self) -> None:
            self.fork_calls = []
            self.feedback_calls = []

        async def ensure_fork(self, owner, name):
            self.fork_calls.append((owner, name))
            return SimpleNamespace(
                owner="coderus-bot",
                url="https://gitcode.com/coderus-bot/demo",
            )

        async def publish(self, **kwargs):
            raise AssertionError("publish should not run while queueing a task")

        async def list_pr_feedback(self, owner, name, number):
            self.feedback_calls.append((owner, name, number))
            return [
                PRFeedbackItem(
                    "comment:7",
                    "issue_comment",
                    "maintainer",
                    "MEMBER",
                    "please support GitCode feedback",
                    "https://gitcode.com/open/demo/pulls/3#note_7",
                )
            ]

    forge = GitCodeForge()
    with TestClient(
        create_app(
            app_settings,
            providers={"github": FakeGitHubProvider(), "gitcode": FakeGitCodeProvider()},
            start_scheduler=False,
        )
    ) as test_client:
        test_client.app.state.forges.install(
            "gitcode",
            forge,
            capabilities=frozenset(
                {
                    ForgeCapability.ENSURE_FORK,
                    ForgeCapability.PUBLISH,
                    ForgeCapability.LIST_PR_FEEDBACK,
                }
            ),
        )
        login(test_client)
        repositories = test_client.get("/repositories")
        created = test_client.post(
            "/repositories",
            data={"url": "https://gitcode.com/open/demo", "csrf_token": csrf_from(repositories)},
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert forge.fork_calls == [("open", "demo")]
        with test_client.app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            repository = session.scalar(
                select(DbRepository).where(DbRepository.provider == "gitcode")
            )
            assert repository.fork_owner == "coderus-bot"
            task = Task(
                issue=DbIssue(
                    repository=repository,
                    external_id="3",
                    number=3,
                    title="GitCode task",
                    body="Reproduction",
                    state="open",
                    source_url="https://gitcode.com/open/demo/issues/3",
                    triage_state="dispatched",
                ),
                creator=admin,
                status="awaiting_human_review",
                workspace_path=str(app_settings.workspace.root / "task-1"),
                branch_name="coderus/issue-3-1",
                pr_url="https://gitcode.com/open/demo/pulls/3",
                pr_number=3,
                pr_state="open",
            )
            session.add(task)
            session.commit()
            task_id = task.id

        detail = test_client.get(f"/tasks/{task_id}")
        assert "同步 PR 意见" in detail.text
        synced = test_client.post(
            f"/tasks/{task_id}/feedback/sync",
            data={"csrf_token": csrf_from(detail)},
            follow_redirects=False,
        )
        assert synced.status_code == 303
        assert forge.feedback_calls == [("open", "demo", 3)]
        with test_client.app.state.sessions() as session:
            feedback = session.scalar(select(DbPRFeedback))
            assert feedback.provider_id == "gitcode:comment:7"
            workspace = app_settings.workspace.root / "gitcode-pr"
            workspace.mkdir()
            publish_task = Task(
                issue=DbIssue(
                    repository=repository,
                    external_id="4",
                    number=4,
                    title="GitCode PR task",
                    body="Reproduction",
                    state="open",
                    source_url="https://gitcode.com/open/demo/issues/4",
                    triage_state="dispatched",
                ),
                creator=admin,
                status="failed",
                workspace_path=str(workspace),
                branch_name="coderus/issue-4-1",
                commit_sha="c" * 40,
            )
            session.add(publish_task)
            session.commit()
            publish_task_id = publish_task.id

        publish_detail = test_client.get(f"/tasks/{publish_task_id}")
        assert "按现状发布 PR" in publish_detail.text
        assert "GitHub 发布未配置" not in publish_detail.text


def test_admin_can_queue_legacy_manual_task_for_pr_publish(
    app_settings: Settings,
) -> None:
    with TestClient(
        create_app(
                app_settings,
                providers={"github": FakeGitHubProvider()},
                publisher=FakePublishForge(),
            start_scheduler=False,
        )
    ) as test_client:
        login(test_client)
        workspace = app_settings.workspace.root / "task-legacy"
        workspace.mkdir(parents=True)
        with test_client.app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            repository = DbRepository(
                provider="github", owner="octo", name="legacy-demo",
                canonical_url="https://github.com/octo/legacy-demo", default_branch="main",
                created_by_user=admin,
            )
            issue = DbIssue(
                repository=repository, external_id="4", number=4, title="Legacy task",
                body="Reproduction", state="open",
                source_url="https://github.com/octo/legacy-demo/issues/4",
                triage_state="dispatched",
            )
            task = Task(
                issue=issue, creator=admin, status="failed",
                failure_code="correction_limit_exceeded", workspace_path=str(workspace),
                branch_name="coderus/issue-4-1", base_commit_sha="a" * 40,
                commit_sha="c" * 40,
            )
            session.add(task)
            session.commit()
            task_id = task.id

        detail = test_client.get(f"/tasks/{task_id}")
        assert "按现状发布 PR" in detail.text
        response = test_client.post(
            f"/tasks/{task_id}/publish-wip",
            data={"csrf_token": csrf_from(detail)},
            follow_redirects=False,
        )

        assert response.status_code == 303
        with test_client.app.state.sessions() as session:
            task = session.get(Task, task_id)
            assert task.status == "queued"
            assert task.failure_code == "publish_existing"


def test_issue_inbox_is_paginated(client: TestClient) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="pagination-demo",
            canonical_url="https://github.com/octo/pagination-demo",
            default_branch="main",
            created_by_user=admin,
        )
        session.add(repository)
        session.flush()
        for number in range(1, 31):
            session.add(
                DbIssue(
                    repository=repository,
                    external_id=str(number),
                    number=number,
                    title=f"Paged issue {number}",
                    body="Reproduction",
                    state="open",
                    source_url=f"https://github.com/octo/pagination-demo/issues/{number}",
                    source_updated_at=datetime(2026, 7, 15, 0, number, tzinfo=UTC),
                )
            )
        session.commit()

    first_page = client.get("/issues")
    assert "Paged issue 30" in first_page.text
    assert "Paged issue 6" in first_page.text
    assert "Paged issue 5" not in first_page.text
    assert "第 1 / 2 页" in first_page.text
    assert 'href="/issues?triage=discovered&page=2"' in first_page.text

    second_page = client.get("/issues?page=2")
    assert "Paged issue 5" in second_page.text
    assert "Paged issue 1" in second_page.text
    assert "Paged issue 6" not in second_page.text
    assert "第 2 / 2 页" in client.get("/issues?page=999").text
    assert "第 1 / 2 页" in client.get("/issues?page=0").text

    title_search = client.get("/issues?q=Paged+issue")
    assert "Paged issue 30" in title_search.text
    assert 'href="/issues?triage=discovered&q=Paged+issue&page=2"' in title_search.text
    number_search = client.get("/issues?q=%2330")
    assert number_search.text.count("Paged issue ") == 1
    repository_search = client.get("/issues?q=pagination-demo")
    assert "30 条记录" in repository_search.text


def test_task_status_is_rendered_in_chinese(client: TestClient) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="status-demo",
            canonical_url="https://github.com/octo/status-demo",
            default_branch="main",
            created_by_user=admin,
        )
        issue = DbIssue(
            repository=repository,
            external_id="1",
            number=1,
            title="Status task",
            body="Reproduction",
            state="open",
            source_url="https://github.com/octo/status-demo/issues/1",
            triage_state="dispatched",
        )
        session.add(Task(issue=issue, creator=admin, status="failed"))
        session.commit()

    tasks = client.get("/tasks")
    assert '<span class="status danger">失败</span>' in tasks.text
    assert ">failed<" not in tasks.text


def test_task_default_filter_hides_archived_statuses_and_combines_owner(
    client: TestClient,
) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        developer = create_user(session, "developer", "developer-password")
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="task-filter-demo",
            canonical_url="https://github.com/octo/task-filter-demo",
            default_branch="main",
            created_by_user=admin,
        )
        active_task_data = [
            ("Queued task", "queued", developer),
            ("Preparing task", "preparing", developer),
            ("Developer working task", "developer_working", developer),
            ("Reviewing task", "reviewing", developer),
            ("Developer revising task", "developer_revising", developer),
            ("Sealing task", "sealing", developer),
            ("Publishing task", "publishing", developer),
            ("Active failure", "failed", developer),
            ("Awaiting review", "awaiting_human_review", developer),
            ("Manual intervention", "manual_intervention", admin),
            ("Cancelling task", "cancelling", developer),
        ]
        archived_task_data = [
            ("Completed archive", "completed", developer),
            ("Closed archive", "closed", admin),
            ("Dismissed archive", "dismissed", developer),
            ("Cancelled archive", "cancelled", admin),
        ]
        task_data = active_task_data + archived_task_data
        for number, (title, status, creator) in enumerate(task_data, start=1):
            issue = DbIssue(
                repository=repository,
                external_id=str(number),
                number=number,
                title=title,
                body="",
                state="open",
                source_url=f"{repository.canonical_url}/issues/{number}",
                triage_state="dispatched",
            )
            session.add(Task(issue=issue, creator=creator, status=status))
        session.commit()

    default_page = client.get("/tasks")
    for title, _, _ in active_task_data:
        assert title in default_page.text
    for title, _, _ in archived_task_data:
        assert title not in default_page.text

    owner_active = client.get("/tasks?owner=developer&status=active")
    assert "Active failure" in owner_active.text
    assert "Awaiting review" in owner_active.text
    assert "Manual intervention" not in owner_active.text
    assert "Completed archive" not in owner_active.text

    all_tasks = client.get("/tasks?status=all")
    for title, _, _ in task_data:
        assert title in all_tasks.text


def test_close_awaiting_review_task_by_owner_or_admin_only(
    client: TestClient,
) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        creator = create_user(session, "creator", "creator-password")
        create_user(session, "other", "other-password")
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="awaiting-review-demo",
            canonical_url="https://github.com/octo/awaiting-review-demo",
            default_branch="main",
            created_by_user=admin,
        )
        task_ids = []
        for number, owner in enumerate((creator, admin, creator), start=1):
            issue = DbIssue(
                repository=repository,
                external_id=str(number),
                number=number,
                title=f"Awaiting review {number}",
                body="",
                state="open",
                source_url=f"{repository.canonical_url}/issues/{number}",
                triage_state="dispatched",
            )
            task = Task(
                issue=issue,
                creator=owner,
                status="awaiting_human_review",
                pr_url=f"https://github.com/octo/awaiting-review-demo/pull/{number}",
                pr_number=number,
            )
            session.add(task)
            session.flush()
            task_ids.append(task.id)
        session.commit()

    original_pr_url = "https://github.com/octo/awaiting-review-demo/pull/1"
    login_as(client, "other", "other-password")
    other_page = client.get(f"/tasks/{task_ids[0]}")
    other_response = client.post(
        f"/tasks/{task_ids[0]}/close",
        data={"csrf_token": csrf_from(other_page)},
        follow_redirects=False,
    )
    assert other_response.status_code == 403

    login_as(client, "creator", "creator-password")
    creator_page = client.get(f"/tasks/{task_ids[0]}")
    assert "不会关闭或修改远端 PR" in creator_page.text
    creator_response = client.post(
        f"/tasks/{task_ids[0]}/close",
        data={"csrf_token": csrf_from(creator_page)},
        follow_redirects=False,
    )
    assert creator_response.status_code == 303
    with client.app.state.sessions() as session:
        owner_closed = session.get(Task, task_ids[0])
        assert owner_closed.status == "dismissed"
        assert owner_closed.pr_url == original_pr_url

    login(client)
    admin_page = client.get(f"/tasks/{task_ids[1]}")
    admin_response = client.post(
        f"/tasks/{task_ids[1]}/close",
        data={"csrf_token": csrf_from(admin_page)},
        follow_redirects=False,
    )
    assert admin_response.status_code == 303
    with client.app.state.sessions() as session:
        assert session.get(Task, task_ids[1]).status == "dismissed"

    with client.app.state.sessions() as session:
        assert session.get(Task, task_ids[2]).status == "awaiting_human_review"


@pytest.mark.parametrize("initial_status", ["failed", "manual_intervention", "cancelled"])
def test_task_owner_can_close_finished_task(
    client: TestClient, initial_status: str
) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name=f"close-{initial_status}",
            canonical_url=f"https://github.com/octo/close-{initial_status}",
            default_branch="main",
            created_by_user=admin,
        )
        issue = DbIssue(
            repository=repository,
            external_id="1",
            number=1,
            title="Close finished task",
            body="Reproduction",
            state="open",
            source_url=f"{repository.canonical_url}/issues/1",
            triage_state="dispatched",
        )
        task = Task(
            issue=issue,
            creator=admin,
            status=initial_status,
            failure_summary="Preserved failure detail",
        )
        session.add(task)
        session.commit()
        task_id = task.id

    detail = client.get(f"/tasks/{task_id}")
    assert f'action="/tasks/{task_id}/close"' in detail.text

    response = client.post(
        f"/tasks/{task_id}/close",
        data={"csrf_token": csrf_from(detail)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with client.app.state.sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "dismissed"
        assert task.finished_at is not None
        assert task.failure_summary == "Preserved failure detail"
    closed_detail = client.get(f"/tasks/{task_id}")
    assert ">已关闭<" in closed_detail.text
    assert "任务已由用户手动关闭。" in closed_detail.text
    assert f'action="/tasks/{task_id}/close"' not in closed_detail.text


def test_active_task_cannot_be_closed(client: TestClient) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="active-close-demo",
            canonical_url="https://github.com/octo/active-close-demo",
            default_branch="main",
            created_by_user=admin,
        )
        issue = DbIssue(
            repository=repository,
            external_id="1",
            number=1,
            title="Active task",
            body="Reproduction",
            state="open",
            source_url="https://github.com/octo/active-close-demo/issues/1",
            triage_state="dispatched",
        )
        task = Task(issue=issue, creator=admin, status="developer_working")
        session.add(task)
        session.commit()
        task_id = task.id

    detail = client.get(f"/tasks/{task_id}")
    assert f'action="/tasks/{task_id}/close"' not in detail.text
    response = client.post(
        f"/tasks/{task_id}/close",
        data={"csrf_token": csrf_from(detail)},
        follow_redirects=False,
    )
    assert response.status_code == 409
    with client.app.state.sessions() as session:
        assert session.get(Task, task_id).status == "developer_working"


def test_pr_publish_requires_an_existing_commit(
    client: TestClient, app_settings: Settings
) -> None:
    login(client)
    workspace = app_settings.workspace.root / "task-without-commit"
    workspace.mkdir(parents=True)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="no-commit-demo",
            canonical_url="https://github.com/octo/no-commit-demo",
            default_branch="main",
            created_by_user=admin,
        )
        issue = DbIssue(
            repository=repository,
            external_id="1",
            number=1,
            title="No commit task",
            body="Reproduction",
            state="open",
            source_url="https://github.com/octo/no-commit-demo/issues/1",
            triage_state="dispatched",
        )
        task = Task(
            issue=issue,
            creator=admin,
            status="failed",
            workspace_path=str(workspace),
            branch_name="coderus/issue-1-1",
        )
        session.add(task)
        session.commit()
        task_id = task.id

    detail = client.get(f"/tasks/{task_id}")
    assert "按现状发布 PR" not in detail.text
    response = client.post(
        f"/tasks/{task_id}/publish-wip",
        data={"csrf_token": csrf_from(detail)},
        follow_redirects=False,
    )
    assert response.status_code == 409


def test_pr_publish_requires_a_configured_publisher(
    client: TestClient, app_settings: Settings
) -> None:
    login(client)
    workspace = app_settings.workspace.root / "task-without-publisher"
    workspace.mkdir(parents=True)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="no-publisher-demo",
            canonical_url="https://github.com/octo/no-publisher-demo",
            default_branch="main",
            created_by_user=admin,
        )
        issue = DbIssue(
            repository=repository,
            external_id="1",
            number=1,
            title="No publisher task",
            body="Reproduction",
            state="open",
            source_url="https://github.com/octo/no-publisher-demo/issues/1",
            triage_state="dispatched",
        )
        task = Task(
            issue=issue,
            creator=admin,
            status="failed",
            workspace_path=str(workspace),
            branch_name="coderus/issue-1-1",
            commit_sha="c" * 40,
            failure_summary="Build failed",
        )
        session.add(task)
        session.commit()
        task_id = task.id

    detail = client.get(f"/tasks/{task_id}")
    assert "按现状发布 PR" not in detail.text
    assert "GitHub 发布未配置" in detail.text
    assert detail.text.count(">失败<") == 1
    assert 'class="alert"' not in detail.text
    assert 'class="inline-note"' not in detail.text
    response = client.post(
        f"/tasks/{task_id}/publish-wip",
        data={"csrf_token": csrf_from(detail)},
        follow_redirects=False,
    )
    assert response.status_code == 409


def test_issue_actions_show_one_time_feedback(client: TestClient) -> None:
    login(client)
    repository_page = client.get("/repositories")
    client.post(
        "/repositories",
        data={
            "url": "https://github.com/octo/demo",
            "csrf_token": csrf_from(repository_page),
        },
    )
    repositories = client.get("/repositories")
    client.post(
        "/repositories/1/sync",
        data={"csrf_token": csrf_from(repositories)},
    )
    issues = client.get("/issues")
    assert '<textarea name="reason"' in issues.text

    response = client.post(
        "/issues/1/ignore",
        data={"csrf_token": csrf_from(issues), "reason": "Not actionable"},
    )
    assert "Issue #1 已忽略" in response.text
    assert "Issue #1 已忽略" not in client.get("/issues?triage=ignored").text
    with client.app.state.sessions() as session:
        assert session.get(DbIssue, 1).ignored_reason == "Not actionable"


def test_closed_or_dispatched_issue_cannot_be_mutated_by_forged_actions(
    client: TestClient,
) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="action-guard-demo",
            canonical_url="https://github.com/octo/action-guard-demo",
            default_branch="main",
            created_by_user=admin,
        )
        closed = DbIssue(
            repository=repository,
            external_id="1",
            number=1,
            title="Closed issue",
            body="",
            state="closed",
            source_url="https://github.com/octo/action-guard-demo/issues/1",
            triage_state="discovered",
        )
        dispatched = DbIssue(
            repository=repository,
            external_id="2",
            number=2,
            title="Dispatched issue",
            body="",
            state="open",
            source_url="https://github.com/octo/action-guard-demo/issues/2",
            triage_state="dispatched",
        )
        session.add_all([closed, dispatched])
        session.commit()
        closed_id = closed.id
        dispatched_id = dispatched.id

    page = client.get("/issues?triage=all")
    closed_row = page.text.split("Closed issue", 1)[1].split("</tr>", 1)[0]
    assert "确认派发" not in closed_row
    assert "Issue 已关闭" in closed_row
    response = client.post(
        f"/issues/{closed_id}/dispatch",
        data={"csrf_token": csrf_from(page), "instructions": ""},
    )
    assert "只有待处理的开放 Issue 可以派发" in response.text

    response = client.post(
        f"/issues/{dispatched_id}/ignore",
        data={"csrf_token": csrf_from(response), "reason": "forged"},
    )
    assert "只有待处理 Issue 可以忽略" in response.text
    with client.app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0
        assert session.get(DbIssue, dispatched_id).triage_state == "dispatched"


def test_refresh_all_reports_repository_failures(app_settings: Settings) -> None:
    class FailingProvider(FakeGitHubProvider):
        def list_open_issues(self, repository: Repository) -> list[Issue]:
            raise RuntimeError("github request failed with status 403")

    with TestClient(
        create_app(
            app_settings,
            providers={"github": FailingProvider()},
            start_scheduler=False,
        )
    ) as test_client:
        login(test_client)
        with test_client.app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            session.add(
                DbRepository(
                    provider="github",
                    owner="octo",
                    name="failing-demo",
                    canonical_url="https://github.com/octo/failing-demo",
                    default_branch="main",
                    created_by_user=admin,
                )
            )
            session.commit()

        page = test_client.get("/repositories")
        response = test_client.post(
            "/repositories/sync-all",
            data={"csrf_token": csrf_from(page)},
        )
        assert "1 个仓库刷新失败" in response.text
        assert 'class="notice danger"' in response.text


def test_completed_task_does_not_offer_pr_feedback_sync(client: TestClient) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="completed-demo",
            canonical_url="https://github.com/octo/completed-demo",
            default_branch="main",
            created_by_user=admin,
        )
        issue = DbIssue(
            repository=repository,
            external_id="1",
            number=1,
            title="Completed task",
            body="",
            state="open",
            source_url="https://github.com/octo/completed-demo/issues/1",
            triage_state="dispatched",
        )
        task = Task(
            issue=issue,
            creator=admin,
            status="completed",
            pr_url="https://github.com/octo/completed-demo/pull/1",
            pr_number=1,
        )
        session.add(task)
        session.commit()
        task_id = task.id

    detail = client.get(f"/tasks/{task_id}")
    assert ">同步 PR 意见</button>" not in detail.text


def test_repository_creation_error_uses_one_time_feedback(client: TestClient) -> None:
    login(client)
    page = client.get("/repositories")
    response = client.post(
        "/repositories",
        data={"url": "https://example.com/not-supported", "csrf_token": csrf_from(page)},
    )
    assert response.status_code == 200
    assert response.url.path == "/repositories"
    assert 'class="notice danger"' in response.text
    assert "仅支持 github.com 或 gitcode.com 仓库地址" in response.text
    assert client.get("/repositories").text.count('class="notice danger"') == 0


def test_task_owner_filter_uses_known_users(client: TestClient) -> None:
    login(client)
    users_page = client.get("/users")
    client.post(
        "/users",
        data={
            "username": "developer",
            "password": "developer-password",
            "csrf_token": csrf_from(users_page),
        },
    )
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        developer = session.scalar(select(User).where(User.username == "developer"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="owners-demo",
            canonical_url="https://github.com/octo/owners-demo",
            default_branch="main",
            created_by_user=admin,
        )
        admin_issue = DbIssue(
            repository=repository,
            external_id="1",
            number=1,
            title="Admin task",
            body="",
            state="open",
            source_url="https://github.com/octo/owners-demo/issues/1",
            triage_state="dispatched",
        )
        developer_issue = DbIssue(
            repository=repository,
            external_id="2",
            number=2,
            title="Developer task",
            body="",
            state="open",
            source_url="https://github.com/octo/owners-demo/issues/2",
            triage_state="dispatched",
        )
        session.add_all(
            [
                Task(issue=admin_issue, creator=admin, status="failed"),
                Task(issue=developer_issue, creator=developer, status="failed"),
            ]
        )
        session.commit()

    page = client.get("/tasks")
    assert '<option value="admin">我的任务 (admin)</option>' in page.text
    assert '<option value="developer">developer</option>' in page.text
    filtered = client.get("/tasks?owner=developer")
    assert "Developer task" in filtered.text
    assert "Admin task" not in filtered.text


def test_task_filter_lists_every_task_status(client: TestClient) -> None:
    login(client)
    page = client.get("/tasks")

    expected_statuses = {
        "queued",
        "preparing",
        "developer_working",
        "reviewing",
        "developer_revising",
        "sealing",
        "publishing",
        "awaiting_human_review",
        "manual_intervention",
        "completed",
        "closed",
        "dismissed",
        "failed",
        "cancelling",
        "cancelled",
    }
    for status in expected_statuses:
        assert f'<option value="{status}"' in page.text


def test_running_repository_cannot_be_synced_again(client: TestClient) -> None:
    login(client)
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = DbRepository(
            provider="github",
            owner="octo",
            name="running-demo",
            canonical_url="https://github.com/octo/running-demo",
            default_branch="main",
            created_by_user=admin,
            sync_status="running",
        )
        session.add(repository)
        session.commit()
        repository_id = repository.id

    page = client.get("/repositories")
    assert "同步中" in page.text
    assert 'disabled aria-label="仓库正在同步"' in page.text
    response = client.post(
        f"/repositories/{repository_id}/sync",
        data={"csrf_token": csrf_from(page)},
    )
    assert "仓库正在同步，请稍后刷新状态" in response.text
    sync_all = client.post(
        "/repositories/sync-all",
        data={"csrf_token": csrf_from(response)},
    )
    assert "已有仓库正在同步，请稍后再刷新全部" in sync_all.text
    with client.app.state.sessions() as session:
        assert session.get(DbRepository, repository_id).sync_status == "running"


def test_startup_recovers_interrupted_repository_sync(app_settings: Settings) -> None:
    with TestClient(
        create_app(
            app_settings,
            providers={"github": FakeGitHubProvider()},
            start_scheduler=False,
        )
    ) as first_client:
        with first_client.app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            session.add(
                DbRepository(
                    provider="github",
                    owner="octo",
                    name="interrupted-demo",
                    canonical_url="https://github.com/octo/interrupted-demo",
                    default_branch="main",
                    created_by_user=admin,
                    sync_status="running",
                )
            )
            session.commit()

    with TestClient(
        create_app(
            app_settings,
            providers={"github": FakeGitHubProvider()},
            start_scheduler=False,
        )
    ) as second_client:
        login(second_client)
        page = second_client.get("/repositories")
        assert "同步被服务重启中断，请重新刷新" in page.text
        with second_client.app.state.sessions() as session:
            repository = session.scalar(
                select(DbRepository).where(DbRepository.name == "interrupted-demo")
            )
            assert repository.sync_status == "failed"


def test_system_page_shows_operational_diagnostics(
    client: TestClient, app_settings: Settings
) -> None:
    login(client)
    page = client.get("/system")
    assert "服务地址" in page.text
    assert "http://127.0.0.1:18082" in page.text
    assert "数据库" in page.text
    assert str(app_settings.database.path) in page.text
    assert "工作区" in page.text
    assert str(app_settings.workspace.root) in page.text
    assert "可用空间" in page.text
    assert "运行中任务" in page.text
    assert "当前版本" in page.text
    assert "上一版本" in page.text
    assert "未启用版本化发布" in page.text


def test_system_page_shows_both_credential_panels_without_gitcode_token(
    client: TestClient,
) -> None:
    login(client)

    page = client.get("/system")

    assert "GitHub 发布凭据" in page.text
    assert 'action="/system/github-credential"' in page.text
    assert "GitCode 凭据" in page.text
    assert 'action="/system/gitcode-credential"' in page.text
    assert 'name="token" value=' not in page.text


def test_repository_and_task_pages_use_each_repository_provider_status(
    client: TestClient, app_settings: Settings
) -> None:
    login(client)
    workspace = app_settings.workspace.root / "gitcode-unconfigured"
    workspace.mkdir()
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        github_repository = DbRepository(
            provider="github",
            owner="octo",
            name="github-unconfigured",
            canonical_url="https://github.com/octo/github-unconfigured",
            default_branch="main",
            fork_owner="coderus-bot",
            fork_url="https://github.com/coderus-bot/github-unconfigured",
            created_by_user=admin,
        )
        gitcode_repository = DbRepository(
            provider="gitcode",
            owner="open",
            name="gitcode-unconfigured",
            canonical_url="https://gitcode.com/open/gitcode-unconfigured",
            default_branch="main",
            created_by_user=admin,
        )
        task = Task(
            issue=DbIssue(
                repository=gitcode_repository,
                external_id="7",
                number=7,
                title="GitCode publish status",
                body="details",
                state="open",
                source_url="https://gitcode.com/open/gitcode-unconfigured/issues/7",
                triage_state="dispatched",
            ),
            creator=admin,
            status="failed",
            workspace_path=str(workspace),
            branch_name="coderus/issue-7-1",
            commit_sha="c" * 40,
        )
        session.add_all((github_repository, gitcode_repository, task))
        session.commit()
        task_id = task.id

    repositories = client.get("/repositories")
    detail = client.get(f"/tasks/{task_id}")

    assert "GitHub" in repositories.text
    assert "GitCode" in repositories.text
    assert "https://github.com/coderus-bot/github-unconfigured" in repositories.text
    assert "GitHub 发布未配置，无法推送或创建 PR。" in repositories.text
    assert "GitCode 发布未配置，无法推送或创建 PR。" in repositories.text
    assert "GitCode 发布未配置，请配置 Token 后再发布现有提交。" in detail.text


def test_platform_forms_show_github_and_gitcode_url_examples(client: TestClient) -> None:
    login(client)

    repositories = client.get("/repositories")
    issues = client.get("/issues")
    reviews = client.get("/reviews")

    assert "https://gitcode.com/owner/repository" in repositories.text
    assert "https://gitcode.com/owner/repository/issues/123" in issues.text
    assert "https://gitcode.com/owner/repository/pulls/123" in reviews.text


def test_system_page_disables_github_form_without_encryption_key(client: TestClient) -> None:
    login(client)

    page = client.get("/system")

    assert "GitHub 发布凭据" in page.text
    assert "CODERUS_CREDENTIAL_ENCRYPTION_KEY" in page.text
    assert 'name="account_name"' in page.text
    assert 'name="token"' in page.text
    assert 'type="password"' in page.text
    assert 'name="token" value=' not in page.text
    assert "disabled" in page.text


def test_system_page_shows_unavailable_codex_authentication(client: TestClient) -> None:
    login(client)

    page = client.get("/system")

    assert "模型 API 代理" in page.text
    assert "Agent 执行已阻止" in page.text
    assert "CODERUS_MODEL_API_KEY" in page.text
    assert client.app.state.codex_auth.ready is False


def test_system_page_shows_safe_feishu_configuration_status(client: TestClient) -> None:
    login(client)

    page = client.get("/system")

    assert "飞书机器人" in page.text
    assert 'name="enabled"' in page.text
    assert 'name="app_id"' in page.text
    assert 'name="app_secret"' in page.text
    assert 'name="default_chat_id"' in page.text
    assert 'type="password"' in page.text
    assert 'name="app_secret" value=' not in page.text
    assert "CODERUS_CREDENTIAL_ENCRYPTION_KEY" in page.text
    assert "未配置" in page.text
    assert "无需重启" in page.text
    assert "最近连接错误" in page.text


def test_github_credential_route_requires_csrf(client: TestClient) -> None:
    login(client)

    response = client.post(
        "/system/github-credential",
        data={"account_name": "octocat", "token": "secret-token", "csrf_token": "bad"},
    )

    assert response.status_code == 400


def test_github_credential_route_requires_admin(client: TestClient) -> None:
    with client.app.state.sessions() as session:
        create_user(session, "developer", "developer-password")
    csrf_token = login_as(client, "developer", "developer-password")

    response = client.post(
        "/system/github-credential",
        data={
            "account_name": "octocat",
            "token": "secret-token",
            "csrf_token": csrf_token,
        },
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("path", "data"),
    [
        (
            "/system/feishu-bot",
            {
                "app_id": "cli_test",
                "app_secret": "secret-value",
                "default_chat_id": "oc_default",
                "enabled": "true",
            },
        ),
        ("/system/feishu-bot/test", {}),
    ],
)
def test_feishu_routes_require_csrf(
    client: TestClient, path: str, data: dict[str, str]
) -> None:
    login(client)

    response = client.post(path, data={**data, "csrf_token": "bad"})

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("path", "data"),
    [
        (
            "/system/feishu-bot",
            {
                "app_id": "cli_test",
                "app_secret": "secret-value",
                "default_chat_id": "oc_default",
                "enabled": "true",
            },
        ),
        ("/system/feishu-bot/test", {}),
    ],
)
def test_feishu_routes_require_admin(
    client: TestClient, path: str, data: dict[str, str]
) -> None:
    with client.app.state.sessions() as session:
        create_user(session, "developer", "developer-password")
    csrf_token = login_as(client, "developer", "developer-password")

    response = client.post(path, data={**data, "csrf_token": csrf_token})

    assert response.status_code == 403


def github_identity_client(state: dict[str, object]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/user"
        return httpx.Response(
            int(state.get("status", 200)),
            json={"login": state.get("login", "OctoCat")},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def credential_settings(app_settings: Settings) -> Settings:
    key = urlsafe_b64encode(os.urandom(32)).decode()
    return app_settings.model_copy(update={"credential_encryption_key": SecretStr(key)})


def feishu_api_client(state: dict[str, object]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls = state.setdefault("calls", [])
        assert isinstance(calls, list)
        calls.append(request)
        if request.url == "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal":
            if state.get("valid", True):
                return httpx.Response(
                    200,
                    json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
                )
            return httpx.Response(200, json={"code": 10003, "msg": "invalid credentials"})
        if request.url == "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id":
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_test"}})
        raise AssertionError(f"unexpected Feishu request: {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_admin_can_validate_and_store_encrypted_feishu_settings(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)
    api_state: dict[str, object] = {}
    api_client = feishu_api_client(api_state)
    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        feishu_http_client=api_client,
        start_scheduler=False,
    )
    with TestClient(app) as test_client:
        login(test_client)
        page = test_client.get("/system")

        response = test_client.post(
            "/system/feishu-bot",
            data={
                "app_id": "cli_test",
                "app_secret": "app-secret-value",
                "default_chat_id": "oc_default",
                "enabled": "true",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        with app.state.sessions() as session:
            stored = session.get(FeishuBotSettings, 1)
            assert stored.app_id == "cli_test"
            assert stored.default_chat_id == "oc_default"
            assert stored.enabled is True
            assert stored.encrypted_app_secret.startswith("v1:")
            assert "app-secret-value" not in stored.encrypted_app_secret
        token_request = api_state["calls"][0]
        assert json.loads(token_request.content) == {
            "app_id": "cli_test",
            "app_secret": "app-secret-value",
        }

        updated_page = test_client.get("/system")
        assert "飞书配置已保存，重启服务后生效" in updated_page.text
        assert "已配置" in updated_page.text
        assert "需要重启" in updated_page.text
        assert "cli_test" in updated_page.text
        assert "oc_default" in updated_page.text
        assert "app-secret-value" not in updated_page.text
        assert 'name="app_secret" value=' not in updated_page.text

    api_client.close()


def test_blank_feishu_secret_preserves_existing_encrypted_value(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)
    api_state: dict[str, object] = {}
    api_client = feishu_api_client(api_state)
    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        feishu_http_client=api_client,
        start_scheduler=False,
    )
    with TestClient(app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        first = test_client.post(
            "/system/feishu-bot",
            data={
                "app_id": "cli_old",
                "app_secret": "old-secret",
                "default_chat_id": "oc_old",
                "enabled": "true",
                "csrf_token": csrf_from(page),
            },
        )
        with app.state.sessions() as session:
            original_ciphertext = session.get(
                FeishuBotSettings,
                1,
            ).encrypted_app_secret

        response = test_client.post(
            "/system/feishu-bot",
            data={
                "app_id": "cli_new",
                "app_secret": "",
                "default_chat_id": "oc_new",
                "enabled": "true",
                "csrf_token": csrf_from(first),
            },
        )

        with app.state.sessions() as session:
            stored = session.get(FeishuBotSettings, 1)
            assert stored.app_id == "cli_new"
            assert stored.default_chat_id == "oc_new"
            assert stored.encrypted_app_secret == original_ciphertext
        assert "飞书配置已保存，重启服务后生效" in response.text
        assert "old-secret" not in response.text
        assert len(api_state["calls"]) == 2

    api_client.close()


def test_invalid_feishu_credentials_preserve_stored_settings(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)
    api_state: dict[str, object] = {}
    api_client = feishu_api_client(api_state)
    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        feishu_http_client=api_client,
        start_scheduler=False,
    )
    with TestClient(app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        first = test_client.post(
            "/system/feishu-bot",
            data={
                "app_id": "cli_valid",
                "app_secret": "valid-secret",
                "default_chat_id": "oc_valid",
                "enabled": "true",
                "csrf_token": csrf_from(page),
            },
        )
        with app.state.sessions() as session:
            original_ciphertext = session.get(FeishuBotSettings, 1).encrypted_app_secret
        api_state["valid"] = False

        response = test_client.post(
            "/system/feishu-bot",
            data={
                "app_id": "cli_invalid",
                "app_secret": "replacement-secret",
                "default_chat_id": "oc_invalid",
                "enabled": "true",
                "csrf_token": csrf_from(first),
            },
        )

        with app.state.sessions() as session:
            stored = session.get(FeishuBotSettings, 1)
            assert stored.app_id == "cli_valid"
            assert stored.default_chat_id == "oc_valid"
            assert stored.encrypted_app_secret == original_ciphertext
        assert "飞书凭据验证失败" in response.text
        assert "replacement-secret" not in response.text

    api_client.close()


def test_feishu_test_message_routes_to_saved_default_chat(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)
    api_state: dict[str, object] = {}
    api_client = feishu_api_client(api_state)
    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        feishu_http_client=api_client,
        start_scheduler=False,
    )
    with TestClient(app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        saved = test_client.post(
            "/system/feishu-bot",
            data={
                "app_id": "cli_test",
                "app_secret": "app-secret-value",
                "default_chat_id": "oc_default",
                "enabled": "true",
                "csrf_token": csrf_from(page),
            },
        )

        response = test_client.post(
            "/system/feishu-bot/test",
            data={"csrf_token": csrf_from(saved)},
        )

        assert "飞书测试消息已发送" in response.text
        calls = api_state["calls"]
        assert len(calls) == 3
        message_request = calls[2]
        assert str(message_request.url).endswith(
            "/open-apis/im/v1/messages?receive_id_type=chat_id"
        )
        assert json.loads(message_request.content) == {
            "receive_id": "oc_default",
            "msg_type": "text",
            "content": json.dumps(
                {"text": "Coderus 飞书机器人测试消息"},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

    api_client.close()


def test_enabled_feishu_bot_starts_after_restart_and_stops_cleanly(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)
    api_state: dict[str, object] = {}
    api_client = feishu_api_client(api_state)
    first_app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        feishu_http_client=api_client,
        start_scheduler=False,
    )
    with TestClient(first_app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        test_client.post(
            "/system/feishu-bot",
            data={
                "app_id": "cli_runtime",
                "app_secret": "runtime-secret",
                "default_chat_id": "oc_default",
                "enabled": "true",
                "csrf_token": csrf_from(page),
            },
        )

    gateways = []

    class FakeGateway:
        def __init__(self, callback) -> None:
            self.callback = callback
            self.start_count = 0
            self.stop_count = 0

        def start(self) -> None:
            self.start_count += 1

        def stop(self) -> None:
            self.stop_count += 1

    def gateway_factory(app_id: str, app_secret: str, callback):
        assert app_id == "cli_runtime"
        assert app_secret == "runtime-secret"
        gateway = FakeGateway(callback)
        gateways.append(gateway)
        return gateway

    restarted_app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        publisher=FakeReviewPublisher(),
        feishu_http_client=api_client,
        feishu_gateway_factory=gateway_factory,
    )
    with TestClient(restarted_app) as test_client:
        assert gateways[0].start_count == 1
        assert restarted_app.state.feishu_bot is not None
        assert restarted_app.state.orchestrator.notifier is not None
        assert (
            restarted_app.state.pr_review_orchestrator.notifier
            is restarted_app.state.feishu_bot._client
        )
        with restarted_app.state.sessions() as session:
            bot_user = session.scalar(select(User).where(User.username == "feishu-bot"))
            assert bot_user is not None
            assert bot_user.role == "user"
            assert bot_user.is_active is True

        login(test_client)
        page = test_client.get("/system")
        assert "运行中" in page.text

    assert gateways[0].stop_count == 1
    api_client.close()


def test_admin_can_store_encrypted_github_credential_and_switch_runtime(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)
    identity = {"login": "OctoCat"}
    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        github_client=github_identity_client(identity),
        start_scheduler=False,
    )
    assert app.state.forges.configured("github") is False
    with TestClient(app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        response = test_client.post(
            "/system/github-credential",
            data={
                "account_name": "octocat",
                "token": "secret-token",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        with app.state.sessions() as session:
            row = session.scalar(
                select(IntegrationCredential).where(
                    IntegrationCredential.provider == "github"
                )
            )
            assert row.account_name == "OctoCat"
            assert row.encrypted_token.startswith("v1:")
            assert "secret-token" not in row.encrypted_token
        assert app.state.forges.configured("github") is True
        assert app.state.orchestrator.forges is app.state.forges
        assert app.state.pr_status_poller.forges is app.state.forges
        assert app.state.pr_review_orchestrator.forges is app.state.forges

        updated_page = test_client.get("/system")
        assert "GitHub 凭据已保存" in updated_page.text
        assert "OctoCat" in updated_page.text
        assert "已加密保存" in updated_page.text
        assert "secret-token" not in updated_page.text
        assert 'name="token" value=' not in updated_page.text


def test_failed_github_validation_preserves_database_and_runtime(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)
    identity = {"login": "OctoCat"}
    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        github_client=github_identity_client(identity),
        start_scheduler=False,
    )
    with TestClient(app) as test_client:
        login(test_client)
        first_page = test_client.get("/system")
        test_client.post(
            "/system/github-credential",
            data={
                "account_name": "octocat",
                "token": "first-token",
                "csrf_token": csrf_from(first_page),
            },
        )
        with app.state.sessions() as session:
            original_ciphertext = session.scalar(select(IntegrationCredential)).encrypted_token
        original_forge = app.state.forges.require("github")
        identity["login"] = "other-user"

        second_page = test_client.get("/system")
        response = test_client.post(
            "/system/github-credential",
            data={
                "account_name": "octocat",
                "token": "replacement-token",
                "csrf_token": csrf_from(second_page),
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        with app.state.sessions() as session:
            stored = session.scalar(select(IntegrationCredential))
            assert stored.encrypted_token == original_ciphertext
            assert stored.account_name == "OctoCat"
        assert app.state.forges.require("github") is original_forge
        failed_page = test_client.get("/system")
        assert "other-user" not in failed_page.text
        assert "replacement-token" not in failed_page.text


def test_admin_can_store_gitcode_credential_and_preserve_runtime_on_failure(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)
    identity = {"login": "GitCodeUser"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.gitcode.com/api/v5/user"
        assert request.headers["Authorization"].startswith("Bearer ")
        assert not request.url.params
        return httpx.Response(200, json=identity)

    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider(), "gitcode": object()},
        github_client=httpx.Client(transport=httpx.MockTransport(handler)),
        start_scheduler=False,
    )
    github_provider = app.state.providers["github"]
    with TestClient(app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        response = test_client.post(
            "/system/gitcode-credential",
            data={
                "account_name": "gitcodeuser",
                "token": "gitcode-token",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert isinstance(app.state.providers["gitcode"], GitCodeProvider)
        assert app.state.providers["github"] is github_provider
        saved_page = test_client.get("/system")
        assert "GitCode 凭据已保存" in saved_page.text
        assert "GitCode credential saved" not in saved_page.text
        original_forge = app.state.forges.require("gitcode")
        with app.state.sessions() as session:
            row = session.scalar(
                select(IntegrationCredential).where(
                    IntegrationCredential.provider == "gitcode"
                )
            )
            assert row.account_name == "GitCodeUser"
            assert "gitcode-token" not in row.encrypted_token

        identity["login"] = "other-user"
        failed_page = test_client.get("/system")
        response = test_client.post(
            "/system/gitcode-credential",
            data={
                "account_name": "gitcodeuser",
                "token": "replacement-token",
                "csrf_token": csrf_from(failed_page),
            },
        )

        assert app.state.forges.require("gitcode") is original_forge
        updated_page = test_client.get("/system")
        assert "gitcode-token" not in updated_page.text
        assert "replacement-token" not in updated_page.text


def test_gitcode_credential_save_failure_uses_chinese_message(
    app_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(
        credential_settings(app_settings),
        providers={"github": FakeGitHubProvider(), "gitcode": object()},
        start_scheduler=False,
    )

    def fail_prepare(_: str, __: str):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(app.state.gitcode_credentials, "prepare", fail_prepare)
    with TestClient(app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        response = test_client.post(
            "/system/gitcode-credential",
            data={
                "account_name": "gitcodeuser",
                "token": "gitcode-token",
                "csrf_token": csrf_from(page),
            },
        )

    assert "GitCode 凭据保存失败" in response.text
    assert "GitCode credential save failed" not in response.text
    assert "gitcode-token" not in response.text


def test_stored_gitcode_credential_builds_real_runtime_after_restart(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"login": "GitCodeUser"})

    api_client = httpx.Client(transport=httpx.MockTransport(handler))
    first_app = create_app(
        settings,
        github_client=api_client,
        start_scheduler=False,
    )
    with TestClient(first_app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        test_client.post(
            "/system/gitcode-credential",
            data={
                "account_name": "gitcodeuser",
                "token": "gitcode-token",
                "csrf_token": csrf_from(page),
            },
        )

    restarted_app = create_app(
        settings,
        github_client=api_client,
        start_scheduler=False,
    )

    assert restarted_app.state.gitcode_credential.account_name == "GitCodeUser"
    assert isinstance(restarted_app.state.providers["gitcode"], GitCodeProvider)
    forge = restarted_app.state.forges.require("gitcode")
    assert isinstance(forge, GitCodeForge)
    assert forge._account_name == "GitCodeUser"
    api_client.close()


def test_environment_gitcode_token_keeps_issue_sync_without_pr_capability(
    app_settings: Settings,
) -> None:
    settings = app_settings.model_copy(
        update={"gitcode_token": SecretStr("environment-gitcode-token")}
    )
    app = create_app(settings, start_scheduler=False)

    assert app.state.gitcode_credential.source == "environment"
    assert app.state.gitcode_credential.account_name is None
    assert isinstance(app.state.providers["gitcode"], GitCodeProvider)
    assert app.state.providers["gitcode"].token == "environment-gitcode-token"
    assert app.state.forges.supports("gitcode", ForgeCapability.PUBLISH) is False


def test_gitcode_credential_route_requires_admin_and_csrf(client: TestClient) -> None:
    login(client)

    invalid_csrf = client.post(
        "/system/gitcode-credential",
        data={"account_name": "gitcodeuser", "token": "secret-token", "csrf_token": "bad"},
    )
    assert invalid_csrf.status_code == 400

    with client.app.state.sessions() as session:
        create_user(session, "gitcode-developer", "developer-password")
    csrf_token = login_as(client, "gitcode-developer", "developer-password")
    non_admin = client.post(
        "/system/gitcode-credential",
        data={
            "account_name": "gitcodeuser",
            "token": "secret-token",
            "csrf_token": csrf_token,
        },
    )
    assert non_admin.status_code == 403


def test_gitcode_mismatched_login_flash_does_not_expose_token(app_settings: Settings) -> None:
    settings = credential_settings(app_settings)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"login": "secret-token"})

    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider(), "gitcode": object()},
        github_client=httpx.Client(transport=httpx.MockTransport(handler)),
        start_scheduler=False,
    )
    with TestClient(app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        response = test_client.post(
            "/system/gitcode-credential",
            data={
                "account_name": "gitcodeuser",
                "token": "secret-token",
                "csrf_token": csrf_from(page),
            },
        )

        assert "GitCode 用户名与 Token 所属账号不一致" in response.text
        assert "secret-token" not in response.text


def test_gitcode_credential_enables_pr_publish_capability(
    app_settings: Settings,
) -> None:
    settings = credential_settings(app_settings)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"login": "GitCodeUser"})

    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider(), "gitcode": object()},
        github_client=httpx.Client(transport=httpx.MockTransport(handler)),
        start_scheduler=False,
    )
    workspace = settings.workspace.root / "gitcode-pr"
    workspace.mkdir()
    with TestClient(app) as test_client:
        login(test_client)
        page = test_client.get("/system")
        test_client.post(
            "/system/gitcode-credential",
            data={
                "account_name": "gitcodeuser",
                "token": "gitcode-token",
                "csrf_token": csrf_from(page),
            },
        )
        with app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            repository = DbRepository(
                provider="gitcode",
                owner="example",
                name="project",
                canonical_url="https://gitcode.com/example/project",
                default_branch="main",
                created_by_user=admin,
            )
            issue = DbIssue(
                repository=repository,
                external_id="1",
                number=1,
                title="GitCode issue",
                body="details",
                state="open",
            )
            task = Task(
                issue=issue,
                creator=admin,
                status="manual_intervention",
                commit_sha="a" * 40,
                workspace_path=str(workspace),
                branch_name="coderus/issue-1-1",
            )
            session.add(task)
            session.commit()
            task_id = task.id

        detail = test_client.get(f"/tasks/{task_id}")
        assert "按现状发布 PR" in detail.text
        response = test_client.post(
            f"/tasks/{task_id}/publish-wip",
            data={"csrf_token": csrf_from(detail)},
            follow_redirects=False,
        )
        assert response.status_code == 303


def test_stored_credential_without_key_blocks_environment_fallback(
    app_settings: Settings,
) -> None:
    configured = credential_settings(app_settings)
    identity = {"login": "OctoCat"}
    first_app = create_app(
        configured,
        providers={"github": FakeGitHubProvider()},
        github_client=github_identity_client(identity),
        start_scheduler=False,
    )
    with TestClient(first_app) as first_client:
        login(first_client)
        page = first_client.get("/system")
        first_client.post(
            "/system/github-credential",
            data={
                "account_name": "octocat",
                "token": "database-token",
                "csrf_token": csrf_from(page),
            },
        )

    missing_key = app_settings.model_copy(
        update={
            "credential_encryption_key": None,
            "github_token": SecretStr("environment-token"),
        }
    )
    second_app = create_app(
        missing_key,
        providers={"github": FakeGitHubProvider()},
        start_scheduler=False,
    )

    assert second_app.state.github_credential.source == "error"
    assert second_app.state.github_credential.error == "缺少凭据加密密钥"
    assert second_app.state.forges.configured("github") is False


def test_invalid_encryption_key_does_not_block_environment_token(
    app_settings: Settings,
) -> None:
    settings = app_settings.model_copy(
        update={
            "credential_encryption_key": SecretStr("invalid-key"),
            "github_token": SecretStr("environment-token"),
        }
    )

    app = create_app(
        settings,
        providers={"github": FakeGitHubProvider()},
        start_scheduler=False,
    )

    assert app.state.github_credential.source == "environment"
    assert app.state.forges.configured("github") is True
    assert app.state.github_encryption_ready is False


def test_app_closes_owned_github_http_client(app_settings: Settings) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        start_scheduler=False,
    )
    github_client = app.state.github_http_client

    with TestClient(app):
        assert github_client.is_closed is False

    assert github_client.is_closed is True


def test_navigation_marks_current_section(client: TestClient) -> None:
    login(client)
    page = client.get("/issues")
    assert '<a class="active" href="/issues">Issue</a>' in page.text
    assert '<a class="active" href="/tasks">任务</a>' not in page.text


def add_pr_review_page_task(
    client: TestClient,
    *,
    provider: str = "github",
    status: str = "queued",
    suffix: str = "1",
    structured_result: dict | None = None,
    failure_code: str | None = None,
    failure_summary: str | None = None,
) -> int:
    with client.app.state.sessions() as session:
        admin = session.scalar(select(User).where(User.username == "admin"))
        repository = session.scalar(
            select(DbRepository).where(
                DbRepository.provider == provider,
                DbRepository.owner == "octo",
                DbRepository.name == "review-pages",
            )
        )
        if repository is None:
            repository = DbRepository(
                provider=provider,
                owner="octo",
                name="review-pages",
                canonical_url=f"https://{provider}.com/octo/review-pages",
                default_branch="main",
                created_by_user=admin,
            )
            session.add(repository)
            session.flush()
        task = PRReviewTask(
            repository=repository,
            pr_number=int(suffix),
            pr_url=(
                f"https://gitcode.com/octo/review-pages/pulls/{suffix}"
                if provider == "gitcode"
                else f"https://github.com/octo/review-pages/pull/{suffix}"
            ),
            status=status,
            base_sha="a" * 40,
            head_sha="b" * 40,
            source_chat_id=f"secret-chat-{suffix}",
            source_message_id=f"review-page-message-{suffix}",
            source_sender_open_id=f"secret-sender-{suffix}",
            review_key=f"secret-review-key-{suffix}",
            claim_token=f"secret-claim-{suffix}",
            structured_result=structured_result,
            failure_code=failure_code,
            failure_summary=failure_summary,
            comment_url=(
                (
                    f"https://gitcode.com/octo/review-pages/pulls/{suffix}"
                    "#note_9"
                    if provider == "gitcode"
                    else f"https://github.com/octo/review-pages/pull/{suffix}#issuecomment-9"
                )
                if status == "completed"
                else None
            ),
            started_at=datetime(2026, 7, 17, 8, 0, tzinfo=UTC),
            finished_at=(
                datetime(2026, 7, 17, 8, 5, tzinfo=UTC)
                if status in {"completed", "failed"}
                else None
            ),
        )
        session.add(task)
        session.commit()
        return task.id


def test_task_tabs_link_issue_and_code_review_pages(client: TestClient) -> None:
    login(client)

    tasks = client.get("/tasks")
    reviews = client.get("/reviews")

    assert tasks.status_code == 200
    assert reviews.status_code == 200
    assert '<a class="active" aria-current="page" href="/tasks">Issue 处理</a>' in tasks.text
    assert '<a href="/reviews">代码检视</a>' in tasks.text
    assert '<a href="/tasks">Issue 处理</a>' in reviews.text
    assert '<a class="active" aria-current="page" href="/reviews">代码检视</a>' in reviews.text
    assert '<a class="active" href="/tasks">任务</a>' in reviews.text


def test_review_pages_filter_paginate_and_hide_internal_fields(
    client: TestClient,
) -> None:
    login(client)
    statuses = ["queued", "preparing", "reviewing", "commenting", "completed", "failed"]
    for index in range(1, 23):
        add_pr_review_page_task(
            client,
            status=statuses[(index - 1) % len(statuses)],
            suffix=str(index),
        )

    first_page = client.get("/reviews")
    second_page = client.get("/reviews?page=2")
    running = client.get("/reviews?status=running")

    assert "RV-22" in first_page.text
    assert 'href="/reviews/1"><strong>RV-1</strong>' not in first_page.text
    assert "第 1 / 2 页" in first_page.text
    assert "/reviews?status=all&page=2" in first_page.text
    assert 'href="/reviews/1"><strong>RV-1</strong>' in second_page.text
    assert "准备代码" in running.text
    assert "代码检视中" in running.text
    assert "提交评论中" in running.text
    assert "https://github.com/octo/review-pages/pull/5" not in running.text
    assert "secret-chat" not in first_page.text
    assert "secret-sender" not in first_page.text
    assert "secret-review-key" not in first_page.text
    assert "secret-claim" not in first_page.text


def test_review_pages_render_structured_result_and_failure(client: TestClient) -> None:
    login(client)
    completed_id = add_pr_review_page_task(
        client,
        status="completed",
        suffix="31",
        structured_result={
            "findings": [
                {
                    "priority": "P1",
                    "title": "空值会导致请求失败",
                    "file_path": "coderus/web/app.py",
                    "line_side": "RIGHT",
                    "line_start": 120,
                    "line_end": 124,
                    "problem": "这里没有检查空值。",
                    "impact": "请求会返回服务器错误。",
                    "suggestion": "增加空值分支和回归测试。",
                }
            ]
        },
    )
    failed_id = add_pr_review_page_task(
        client,
        status="failed",
        suffix="32",
        failure_code="runner_failed",
        failure_summary="Codex 检视进程异常退出",
    )

    completed = client.get(f"/reviews/{completed_id}")
    failed = client.get(f"/reviews/{failed_id}")

    assert completed.status_code == 200
    assert f"RV-{completed_id}" in completed.text
    assert "coderus/web/app.py:120-124" in completed.text
    assert "空值会导致请求失败" in completed.text
    assert "请求会返回服务器错误" in completed.text
    assert "增加空值分支和回归测试" in completed.text
    assert "查看 PR 评论" in completed.text
    assert failed.status_code == 200
    assert "runner_failed" in failed.text
    assert "Codex 检视进程异常退出" in failed.text
    assert "未发现需要处理的代码问题" not in failed.text
    assert client.get("/reviews/99999").status_code == 404


def test_review_pages_show_provider_and_neutral_comment_link(client: TestClient) -> None:
    login(client)
    review_id = add_pr_review_page_task(
        client, provider="gitcode", status="completed", suffix="33"
    )

    listing = client.get("/reviews")
    detail = client.get(f"/reviews/{review_id}")

    assert "GitCode" in listing.text
    assert "查看 PR 评论" in listing.text
    assert "GitCode 评论" not in listing.text
    assert "GitCode" in detail.text
    assert "查看 PR 评论" in detail.text
    assert "查看 GitHub 评论" not in detail.text


def test_review_pages_are_visible_to_regular_users(client: TestClient) -> None:
    with client.app.state.sessions() as session:
        create_user(session, "developer", "developer-password")
    review_id = add_pr_review_page_task(client, status="completed", suffix="41")
    login_as(client, "developer", "developer-password")

    listing = client.get("/reviews")
    detail = client.get(f"/reviews/{review_id}")

    assert listing.status_code == 200
    assert f"RV-{review_id}" in listing.text
    assert detail.status_code == 200


@pytest.mark.parametrize(
    ("provider", "pr_url"),
    [
        ("github", "https://github.com/octo/web-review/pull/7"),
        ("gitcode", "https://gitcode.com/octo/web-review/pulls/7"),
    ],
)
def test_regular_user_can_create_review_for_admin_registered_repository(
    app_settings: Settings, provider: str, pr_url: str
) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        publisher=FakeReviewPublisher(),
        start_scheduler=False,
    )
    if provider == "gitcode":
        app.state.forges.install("gitcode", FakeReviewPublisher())
    enable_agent_execution(app)
    with TestClient(app) as test_client:
        login(test_client)
        with app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            developer = create_user(session, "reviewer", "developer-password")
            session.add(
                DbRepository(
                    provider=provider,
                    owner="octo",
                    name="web-review",
                    canonical_url=f"https://{provider}.com/octo/web-review",
                    default_branch="main",
                    created_by_user=admin,
                )
            )
            session.commit()
            developer_id = developer.id
        login_as(test_client, "reviewer", "developer-password")
        page = test_client.get("/reviews")

        assert 'name="pr_url"' in page.text
        response = test_client.post(
            "/reviews",
            data={"pr_url": pr_url, "csrf_token": csrf_from(page)},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/reviews/1"
        with app.state.sessions() as session:
            task = session.query(PRReviewTask).one()
            assert task.status == "queued"
            assert task.repository.provider == provider
            assert task.source_chat_id == ""
            assert task.source_message_id.startswith("web-review:")
            assert task.source_sender_open_id == f"web-user:{developer_id}"


def test_unavailable_codex_auth_blocks_pr_review(
    app_settings: Settings,
) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        publisher=FakeReviewPublisher(),
        start_scheduler=False,
    )
    with TestClient(app) as test_client:
        login(test_client)
        with app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            session.add(
                DbRepository(
                    provider="github",
                    owner="octo",
                    name="web-review",
                    canonical_url="https://github.com/octo/web-review",
                    default_branch="main",
                    created_by_user=admin,
                )
            )
            session.commit()
        page = test_client.get("/reviews")

        response = test_client.post(
            "/reviews",
            data={
                "pr_url": "https://github.com/octo/web-review/pull/7",
                "csrf_token": csrf_from(page),
            },
        )

        assert "Agent 执行已阻止" in response.text
        with app.state.sessions() as session:
            assert session.scalar(select(func.count()).select_from(PRReviewTask)) == 0


@pytest.mark.parametrize(
    "pr_url",
    [
        "https://github.com/octo/not-registered/pull/7",
        "https://gitcode.com/octo/web-review/pull/7",
        "https://gitcode.com/octo/web-review/pulls/7",
    ],
)
def test_web_review_rejects_unregistered_invalid_or_unconfigured_pull_request(
    app_settings: Settings, pr_url: str
) -> None:
    app = create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        publisher=FakeReviewPublisher(),
        start_scheduler=False,
    )
    with TestClient(app) as test_client:
        login(test_client)
        with app.state.sessions() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            session.add(
                DbRepository(
                    provider="gitcode",
                    owner="octo",
                    name="web-review",
                    canonical_url="https://gitcode.com/octo/web-review",
                    default_branch="main",
                    created_by_user=admin,
                )
            )
            session.commit()
        page = test_client.get("/reviews")

        response = test_client.post(
            "/reviews",
            data={"pr_url": pr_url, "csrf_token": csrf_from(page)},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/reviews"
        with app.state.sessions() as session:
            assert session.query(PRReviewTask).count() == 0


def test_authenticated_page_renders_coderus_shell(client: TestClient) -> None:
    login(client)
    page = client.get("/")
    assert '<body class="app-shell">' in page.text
    assert 'class="brand-mark"' in page.text
    assert 'class="brand-signal"' in page.text


def test_login_page_renders_coderus_shell(client: TestClient) -> None:
    page = client.get("/login")
    assert '<body class="login-shell">' in page.text
    assert '<div class="product-mark" aria-hidden="true">C</div>' in page.text
