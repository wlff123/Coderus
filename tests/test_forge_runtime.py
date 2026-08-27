from types import SimpleNamespace

import httpx

from coderus.forge import (
    ForgeCapability,
    ForgeRegistration,
    ForgeRegistry,
    GitCodeForge,
    GitHubForge,
)
from coderus.application import IssueCommands, ReviewCommands
from coderus.integrations.feishu.commands import IncomingFeishuMessage
from coderus.integrations.feishu.service import FeishuCommandService
from coderus.models import PRReviewTask, Repository, User
from coderus.providers import GitCodeProvider, GitHubProvider
from coderus.web.forge_runtime import (
    ForgeRuntime,
    build_gitcode_runtime,
    build_github_runtime,
    install_forge_runtime,
)


def fake_app() -> SimpleNamespace:
    github_provider = object()
    gitcode_provider = object()
    github_forge = object()
    gitcode_forge = object()
    return SimpleNamespace(
        state=SimpleNamespace(
            providers={"github": github_provider, "gitcode": gitcode_provider},
            issue_poller=SimpleNamespace(
                providers={"github": github_provider, "gitcode": gitcode_provider}
            ),
            forges=ForgeRegistry({"github": github_forge, "gitcode": gitcode_forge}),
        )
    )


def test_build_github_runtime_creates_provider_and_forge(session) -> None:
    runtime = build_github_runtime(
        "github-token",
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        session_factory=lambda: session,
    )

    assert isinstance(runtime, ForgeRuntime)
    assert isinstance(runtime.provider_client, GitHubProvider)
    assert runtime.provider_client.token == "github-token"
    assert isinstance(runtime.registration.forge, GitHubForge)
    assert runtime.registration.supports(ForgeCapability.PUBLISH)


def test_build_gitcode_runtime_without_account_is_issue_only(session) -> None:
    runtime = build_gitcode_runtime(
        "gitcode-token",
        account_name=None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        session_factory=lambda: session,
    )

    assert isinstance(runtime, ForgeRuntime)
    assert isinstance(runtime.provider_client, GitCodeProvider)
    assert runtime.provider_client.token == "gitcode-token"
    assert runtime.registration.forge is None
    assert runtime.registration.supports(ForgeCapability.PUBLISH) is False
    assert runtime.registration.supports(ForgeCapability.LIST_PR_FEEDBACK) is False


def test_build_gitcode_runtime_with_account_creates_real_forge(session) -> None:
    runtime = build_gitcode_runtime(
        "gitcode-token",
        account_name="coderus-bot",
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        session_factory=lambda: session,
    )

    assert isinstance(runtime.provider_client, GitCodeProvider)
    assert isinstance(runtime.registration.forge, GitCodeForge)
    assert runtime.registration.supports(ForgeCapability.PUBLISH) is True
    assert runtime.registration.supports(ForgeCapability.LIST_PR_FEEDBACK) is True


def test_installing_gitcode_runtime_keeps_github_objects() -> None:
    app = fake_app()
    github_provider = app.state.providers["github"]
    github_poller = app.state.issue_poller.providers["github"]
    github_forge = app.state.forges.require("github")
    forge = object()
    runtime = ForgeRuntime(
        provider_client=object(), registration=ForgeRegistration.full(forge)
    )

    install_forge_runtime(app, "gitcode", runtime)

    assert app.state.providers["gitcode"] is runtime.provider_client
    assert app.state.issue_poller.providers["gitcode"] is runtime.provider_client
    assert app.state.forges.require("gitcode") is forge
    assert app.state.providers["github"] is github_provider
    assert app.state.issue_poller.providers["github"] is github_poller
    assert app.state.forges.require("github") is github_forge


def test_installing_github_runtime_keeps_gitcode_objects() -> None:
    app = fake_app()
    gitcode_provider = app.state.providers["gitcode"]
    gitcode_poller = app.state.issue_poller.providers["gitcode"]
    gitcode_forge = app.state.forges.require("gitcode")
    forge = object()
    runtime = ForgeRuntime(
        provider_client=object(), registration=ForgeRegistration.full(forge)
    )

    install_forge_runtime(app, "github", runtime)

    assert app.state.providers["github"] is runtime.provider_client
    assert app.state.issue_poller.providers["github"] is runtime.provider_client
    assert app.state.forges.require("github") is forge
    assert app.state.providers["gitcode"] is gitcode_provider
    assert app.state.issue_poller.providers["gitcode"] is gitcode_poller
    assert app.state.forges.require("gitcode") is gitcode_forge


class IncompleteReviewForge:
    async def get_pull_request(self, owner: str, name: str, pr_number: int):
        return None


class CompleteReviewForge(IncompleteReviewForge):
    async def publish_pr_comment(
        self, owner: str, name: str, pr_number: int, body: str, marker: str
    ):
        return None


def _review_message(message_id: str) -> IncomingFeishuMessage:
    return IncomingFeishuMessage(
        message_id=message_id,
        event_id=f"event-{message_id}",
        chat_id="chat-1",
        chat_type="p2p",
        sender_open_id="sender-1",
        text="检视 https://github.com/octo/demo/pull/7",
        mentioned_bot=False,
    )


def _add_review_repository(session) -> None:
    user = User(username="admin", password_hash="hash", role="admin")
    session.add(
        Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            default_branch="main",
            is_enabled=True,
            created_by_user=user,
        )
    )
    session.commit()


def test_feishu_review_gate_requires_both_review_methods(engine) -> None:
    from coderus.db import create_session_factory

    sessions = create_session_factory(engine)
    with sessions() as session:
        _add_review_repository(session)
    forges = ForgeRegistry(
        {
            "github": ForgeRegistration(
                IncompleteReviewForge(),
                frozenset({ForgeCapability.GET_PULL_REQUEST}),
            )
        }
    )
    service = FeishuCommandService(
        session_factory=sessions,
        issues=IssueCommands(session_factory=sessions, providers={}),
        reviews=ReviewCommands(session_factory=sessions, forges=forges),
    )

    unavailable = service.handle(_review_message("incomplete"))
    forges.install(
        "github",
        CompleteReviewForge(),
        capabilities=frozenset(
            {
                ForgeCapability.GET_PULL_REQUEST,
                ForgeCapability.PUBLISH_PR_COMMENT,
            }
        ),
    )
    available = service.handle(_review_message("complete"))

    assert unavailable == "检视失败：GitHub 平台尚未配置"
    assert available == "已创建检视任务 RV-1，正在排队"
    with sessions() as session:
        assert session.query(PRReviewTask).count() == 1
