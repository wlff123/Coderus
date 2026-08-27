"""阶段 1 架构收敛的外部行为契约：路由集合与关键响应保持不变。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coderus.auth.service import create_user
from coderus.config import DatabaseSettings, Settings
from coderus.providers.models import Issue, Repository
from coderus.web.app import create_app

EXPECTED_ROUTES = {
    ("GET", "/healthz", "healthz"),
    ("GET", "/readyz", "readyz"),
    ("GET", "/login", "login_page"),
    ("POST", "/login", "login"),
    ("POST", "/logout", "logout"),
    ("GET", "/account", "account_page"),
    ("POST", "/account/password", "change_own_password"),
    ("GET", "/", "dashboard"),
    ("GET", "/users", "users_page"),
    ("POST", "/users", "add_user"),
    ("POST", "/users/{user_id}/toggle", "toggle_user"),
    ("POST", "/users/{user_id}/reset-password", "reset_user_password"),
    ("GET", "/system", "system_page"),
    ("POST", "/system/github-credential", "save_github_credential"),
    ("POST", "/system/gitcode-credential", "save_gitcode_credential"),
    ("POST", "/system/feishu-bot", "save_feishu_bot_settings"),
    ("POST", "/system/feishu-bot/test", "test_feishu_bot"),
    ("GET", "/repositories", "repositories_page"),
    ("POST", "/repositories", "add_repository"),
    ("POST", "/repositories/{repository_id}/sync", "force_sync"),
    ("POST", "/repositories/{repository_id}/toggle", "toggle_repository"),
    ("POST", "/repositories/sync-all", "force_sync_all"),
    ("GET", "/issues", "issues_page"),
    ("POST", "/issues/manual", "add_issue_manually"),
    ("POST", "/issues/{issue_id}/dispatch", "dispatch"),
    ("POST", "/issues/{issue_id}/ignore", "ignore_issue"),
    ("POST", "/issues/{issue_id}/restore", "restore_issue"),
    ("GET", "/tasks", "tasks_page"),
    ("GET", "/tasks/{task_id}", "task_detail"),
    ("POST", "/tasks/{task_id}/cancel", "cancel_task"),
    ("POST", "/tasks/{task_id}/close", "close_task"),
    ("POST", "/tasks/{task_id}/feedback/sync", "sync_task_feedback"),
    ("POST", "/tasks/{task_id}/publish-wip", "publish_existing_wip"),
    ("POST", "/tasks/{task_id}/feedback/handle", "handle_task_feedback"),
    ("GET", "/reviews", "reviews_page"),
    ("GET", "/reviews/{review_id}", "review_detail"),
    ("POST", "/reviews", "create_review"),
}

_AUTO_ROUTE_NAMES = {"openapi", "swagger_ui_html", "swagger_ui_redirect", "redoc_html"}


class FakeGitHubProvider:
    name = "github"

    def get_repository(self, url: str) -> Repository:
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
        return []

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


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return Settings(
        database=DatabaseSettings(path=tmp_path / "contract.db"),
        workspace={"root": tmp_path / "workspaces"},
        session_secret=SecretStr("test-session-secret-that-is-long-enough"),
        bootstrap_admin_password=SecretStr("initial-password"),
    )


@pytest.fixture
def app(app_settings: Settings):
    return create_app(
        app_settings,
        providers={"github": FakeGitHubProvider()},
        start_scheduler=False,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def csrf_from(response) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def login_as(client: TestClient, username: str, password: str) -> str:
    csrf_token = csrf_from(client.get("/login"))
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf_token


def test_active_route_snapshot_is_stable(app) -> None:
    actual = {
        (method, route.path, route.name)
        for route in app.routes
        if isinstance(route, APIRoute) and route.name not in _AUTO_ROUTE_NAMES
        for method in route.methods - {"HEAD", "OPTIONS"}
    }
    assert actual == EXPECTED_ROUTES


def test_unauthenticated_dashboard_redirects_to_login(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_regular_user_cannot_open_admin_page(client: TestClient) -> None:
    with client.app.state.sessions() as session:
        create_user(session, "developer", "developer-password")
    login_as(client, "developer", "developer-password")

    response = client.get("/users")
    assert response.status_code == 403


def test_invalid_csrf_token_is_rejected(client: TestClient) -> None:
    login_as(client, "admin", "initial-password")

    response = client.post("/login", data={"username": "x", "password": "y", "csrf_token": "bad"})
    assert response.status_code == 400


def test_release_drain_rejects_mutations(
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
        gate.touch()
        response = drain_client.post("/login", data={})

    assert response.status_code == 503
    assert response.json()["error_code"] == "release_draining"


def test_unknown_task_detail_returns_404(client: TestClient) -> None:
    login_as(client, "admin", "initial-password")

    response = client.get("/tasks/999999")
    assert response.status_code == 404


def test_successful_form_post_redirects(client: TestClient) -> None:
    csrf_token = login_as(client, "admin", "initial-password")

    response = client.post(
        "/logout", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
