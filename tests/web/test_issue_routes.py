"""Issue 路由必须把派发委托给应用服务，而不是内联业务逻辑。"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

import coderus
from coderus.auth.service import create_user
from coderus.models import Issue, Repository, User
from coderus.web.routes.auth import build_auth_router
from coderus.web.routes.issues import build_issue_router
from coderus.web.ui import WebUI


class RecordingIssueCommands:
    def __init__(self) -> None:
        self.dispatch_calls: list[tuple[int, int, str]] = []
        self.added_urls: list[str] = []

    def dispatch_in_session(
        self, session, issue_id: int, actor_id: int, instructions: str = ""
    ) -> int:
        self.dispatch_calls.append((issue_id, actor_id, instructions))
        return 42

    def add_issue(self, issue_url: str) -> int:
        self.added_urls.append(issue_url)
        return 7


def make_app(engine, issues: RecordingIssueCommands) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret-key-that-is-long-enough"
    )
    static_root = Path(coderus.__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_root), name="static")
    ui = WebUI()

    def sessions() -> Session:
        return Session(engine)

    app.include_router(build_auth_router(ui=ui, session_factory=sessions))
    app.include_router(
        build_issue_router(
            ui=ui,
            session_factory=sessions,
            issues=issues,
            forge_status=lambda: {},
            codex_auth=lambda: SimpleNamespace(ready=True, detail=""),
        )
    )
    return app


def seed_issue(engine) -> tuple[int, int]:
    with Session(engine) as session:
        user = create_user(session, "operator", "operator-password")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            created_by_user=session.get(User, user.id),
        )
        issue = Issue(
            repository=repository,
            external_id="1",
            number=1,
            title="crash on start",
            body="details",
            state="open",
            source_url="https://github.com/octo/demo/issues/1",
        )
        session.add(issue)
        session.commit()
        return issue.id, user.id


def csrf_from(text: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', text).group(1)


def login(client: TestClient) -> str:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": "operator",
            "password": "operator-password",
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf_from(client.get("/account").text)


def test_dispatch_delegates_to_issue_commands_once(engine) -> None:
    issue_id, actor_id = seed_issue(engine)
    issues = RecordingIssueCommands()
    client = TestClient(make_app(engine, issues))
    csrf_token = login(client)

    response = client.post(
        f"/issues/{issue_id}/dispatch",
        data={"csrf_token": csrf_token, "instructions": "优先验证回归"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert issues.dispatch_calls == [(issue_id, actor_id, "优先验证回归")]


def test_manual_add_delegates_to_issue_commands(engine) -> None:
    seed_issue(engine)
    issues = RecordingIssueCommands()
    client = TestClient(make_app(engine, issues))
    csrf_token = login(client)

    response = client.post(
        "/issues/manual",
        data={
            "csrf_token": csrf_token,
            "url": "https://github.com/octo/demo/issues/7",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert issues.added_urls == ["https://github.com/octo/demo/issues/7"]
