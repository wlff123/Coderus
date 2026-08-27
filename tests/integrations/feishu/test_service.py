from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import coderus.application.issues as application_issues_module
import coderus.application.reviews as application_reviews_module
from coderus.application import IssueCommands, ReviewCommands
from coderus.db import create_session_factory
from coderus.forge import ForgeRegistry
from coderus.integrations.feishu.commands import IncomingFeishuMessage
from coderus.integrations.feishu.service import FeishuCommandService
from coderus.issues.service import add_and_dispatch_issue
from coderus.models import FeishuEvent, Issue, PRReviewTask, Repository, Task, User
from coderus.providers.models import Issue as ProviderIssue
from coderus.providers.models import Repository as ProviderRepository


class FakeReviewForge:
    async def get_pull_request(self, owner: str, name: str, pr_number: int):
        raise AssertionError("review should not run while enqueueing")

    async def publish_pr_comment(
        self, owner: str, name: str, pr_number: int, body: str, marker: str
    ):
        raise AssertionError("review should not run while enqueueing")


def message(
    text: str,
    *,
    message_id: str = "om_1",
    chat_type: str = "group",
    mentioned_bot: bool = True,
    sender_open_id: str | None = "ou_1",
) -> IncomingFeishuMessage:
    return IncomingFeishuMessage(
        message_id=message_id,
        event_id="evt_1",
        chat_id="oc_1",
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        text=text,
        mentioned_bot=mentioned_bot,
    )


def test_missing_sender_is_rejected_and_audited(engine) -> None:
    reply = service(engine).handle(
        message("状态", message_id="missing-sender", sender_open_id=None)
    )

    assert reply == "无法确认发送者身份，已拒绝处理"
    with create_session_factory(engine)() as session:
        event = session.query(FeishuEvent).one()
        assert event.chat_id == "oc_1"
        assert event.sender_open_id == "<missing>"
        assert event.status == "failed"
        assert event.error_summary == "missing sender_open_id"
        assert event.reply_text == reply
        assert event.reply_status == "pending"


def service(
    engine,
    providers=None,
    *,
    forges: ForgeRegistry | None = None,
    assistant=None,
) -> FeishuCommandService:
    sessions = create_session_factory(engine)
    return FeishuCommandService(
        session_factory=sessions,
        issues=IssueCommands(session_factory=sessions, providers=providers or {}),
        reviews=ReviewCommands(
            session_factory=sessions,
            forges=forges or ForgeRegistry({"github": FakeReviewForge()}),
        ),
        assistant=assistant,
    )


def add_task(
    engine,
    number: int,
    *,
    status: str = "queued",
    failure_summary: str | None = None,
    pr_url: str | None = None,
) -> int:
    with create_session_factory(engine)() as session:
        user = session.query(User).filter_by(username="admin").one_or_none()
        if user is None:
            user = User(username="admin", password_hash="hash", role="admin")
            session.add(user)
        repository = session.query(Repository).filter_by(name="demo").one_or_none()
        if repository is None:
            repository = Repository(
                provider="github",
                owner="octo",
                name="demo",
                canonical_url="https://github.com/octo/demo",
                default_branch="main",
                created_by_user=user,
            )
            session.add(repository)
        issue = Issue(
            repository=repository,
            external_id=str(number),
            number=number,
            title=f"Issue {number}",
            body="details",
            state="open",
            source_url=f"https://github.com/octo/demo/issues/{number}",
        )
        task = Task(
            issue=issue,
            creator=user,
            status=status,
            failure_summary=failure_summary,
            pr_url=pr_url,
        )
        session.add(task)
        session.commit()
        return task.id


class FakeProvider:
    def __init__(self, *, issue_state: str = "open") -> None:
        self.issue_state = issue_state
        self.calls = 0

    def get_issue(
        self, repository: ProviderRepository, number: int
    ) -> ProviderIssue:
        self.calls += 1
        return ProviderIssue(
            repository=repository,
            external_id=str(number),
            number=number,
            title=f"Issue {number}",
            body="details",
            state=self.issue_state,
            labels=("bug",),
            canonical_url=f"{repository.canonical_url}/issues/{number}",
            created_at=None,
            updated_at=datetime(2026, 7, 16, tzinfo=UTC),
        )


def add_authorized_repository(
    engine, *, enabled: bool = True, provider: str = "github"
) -> None:
    with create_session_factory(engine)() as session:
        admin = User(username="admin", password_hash="hash", role="admin")
        session.add(
            Repository(
                provider=provider,
                owner="octo",
                name="demo",
                canonical_url=f"https://{provider}.com/octo/demo",
                default_branch="main",
                is_enabled=enabled,
                created_by_user=admin,
            )
        )
        session.commit()


def test_group_message_without_bot_mention_is_ignored(engine) -> None:
    command_service = service(engine)

    reply = command_service.handle(message("帮助", mentioned_bot=False))

    assert reply is None
    with create_session_factory(engine)() as session:
        assert session.query(FeishuEvent).count() == 0


def test_direct_message_does_not_require_bot_mention(engine) -> None:
    reply = service(engine).handle(
        message("帮助", chat_type="p2p", mentioned_bot=False)
    )

    assert reply is not None
    assert "派发 <Issue URL>" in reply


def test_help_lists_only_supported_commands(engine) -> None:
    reply = service(engine).handle(message("帮助"))

    assert reply is not None
    assert "状态" in reply
    assert "任务 RE-N" in reply
    assert "派发 <Issue URL>" in reply
    assert "取消" not in reply
    with create_session_factory(engine)() as session:
        event = session.query(FeishuEvent).one()
        assert event.reply_text == reply
        assert event.reply_status == "pending"


def test_failed_reply_uses_persisted_backoff_before_retry(engine) -> None:
    command_service = service(engine)
    command_service.handle(message("帮助", message_id="retry-me"))

    command_service.mark_reply_result("retry-me", "temporary")

    assert command_service.pending_replies() == []
    with create_session_factory(engine)() as session:
        event = session.query(FeishuEvent).filter_by(message_id="retry-me").one()
        assert event.reply_attempts == 1
        assert event.reply_next_attempt_at is not None
        event.reply_next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert command_service.pending_replies()[0][0] == "retry-me"


def test_unknown_command_returns_strict_usage_help(engine) -> None:
    reply = service(engine).handle(message("帮我看看状态"))

    assert reply is not None
    assert reply.startswith("无法识别命令")
    assert "帮助" in reply


def test_capability_question_returns_coderus_introduction(engine) -> None:
    reply = service(engine).handle(message("你会干什么？"))

    assert reply.startswith("我是 Coderus")
    assert "您的代码仓管家" in reply
    assert "创建 PR 和检视 PR" in reply
    assert "WIP" not in reply
    assert "派发 <Issue URL>" in reply
    assert "检视 <GitHub 或 GitCode PR URL>" in reply


def test_duplicate_message_id_is_processed_once(engine) -> None:
    command_service = service(engine)

    first_reply = command_service.handle(message("帮助"))
    duplicate_reply = command_service.handle(message("状态"))

    assert first_reply is not None
    assert duplicate_reply is None
    with create_session_factory(engine)() as session:
        events = session.query(FeishuEvent).all()
        assert len(events) == 1
        assert events[0].message_id == "om_1"
        assert events[0].status == "processed"


def test_status_counts_each_operational_task_group(engine) -> None:
    for number, status in enumerate(
        (
            "queued",
            "developer_working",
            "reviewing",
            "awaiting_human_review",
            "manual_intervention",
            "failed",
        ),
        start=1,
    ):
        add_task(engine, number, status=status)

    reply = service(engine).handle(message("状态"))

    assert reply is not None
    assert "排队中：1" in reply
    assert "执行中：2" in reply
    assert "等待人工审核：1" in reply
    assert "需要人工处理：1" in reply
    assert "失败：1" in reply


def test_tasks_returns_only_ten_most_recent_tasks(engine) -> None:
    for number in range(1, 13):
        add_task(engine, number)

    reply = service(engine).handle(message("任务"))

    assert reply is not None
    task_lines = [line for line in reply.splitlines() if line.startswith("RE-")]
    assert len(task_lines) == 10
    assert task_lines[0].startswith("RE-12 ")
    assert task_lines[-1].startswith("RE-3 ")
    assert all(not line.startswith("RE-2 ") for line in task_lines)


def test_task_detail_includes_stage_repository_issue_failure_and_pr(engine) -> None:
    task_id = add_task(
        engine,
        7,
        status="manual_intervention",
        failure_summary="Tests failed",
        pr_url="https://github.com/octo/demo/pull/9",
    )

    reply = service(engine).handle(message(f"任务 RE-{task_id}"))

    assert reply is not None
    assert f"RE-{task_id}" in reply
    assert "阶段：需要人工处理（manual_intervention）" in reply
    assert "仓库：github/octo/demo" in reply
    assert "Issue：https://github.com/octo/demo/issues/7" in reply
    assert "失败摘要：Tests failed" in reply
    assert "PR：https://github.com/octo/demo/pull/9" in reply


def test_missing_task_detail_returns_not_found(engine) -> None:
    reply = service(engine).handle(message("任务 RE-404"))

    assert reply == "未找到任务 RE-404"


def test_add_and_dispatch_issue_reuses_authorized_repository_and_dispatch(engine) -> None:
    add_authorized_repository(engine)
    provider = FakeProvider()
    sessions = create_session_factory(engine)
    with sessions() as session:
        creator = session.scalar(select(User).where(User.username == "admin"))

        task = add_and_dispatch_issue(
            session,
            {"github": provider},
            "https://github.com/octo/demo/issues/7",
            creator,
        )

        assert task.issue.number == 7
        assert task.issue.triage_state == "dispatched"
        assert task.status == "queued"
    assert provider.calls == 1


def test_dispatch_uses_ensured_feishu_bot_user_and_records_task(engine) -> None:
    add_authorized_repository(engine)
    sessions = create_session_factory(engine)
    with sessions() as session:
        session.add(
            User(
                username="feishu-bot",
                password_hash="hash",
                role="admin",
                is_active=False,
            )
        )
        session.commit()
    provider = FakeProvider()

    reply = service(engine, {"github": provider}).handle(
        message("派发 https://github.com/octo/demo/issues/7")
    )

    assert reply is not None
    assert reply.startswith("已派发为 RE-")
    assert "github/octo/demo#7 Issue 7" in reply
    with sessions() as session:
        task = session.query(Task).one()
        bot_user = session.query(User).filter_by(username="feishu-bot").one()
        event = session.query(FeishuEvent).one()
        assert task.created_by == bot_user.id
        assert bot_user.role == "user"
        assert bot_user.is_active is True
        assert event.task_id == task.id
        assert event.status == "processed"


def test_duplicate_dispatch_does_not_fetch_or_create_a_second_task(engine) -> None:
    add_authorized_repository(engine)
    provider = FakeProvider()
    command_service = service(engine, {"github": provider})
    incoming = message("派发 https://github.com/octo/demo/issues/7")

    first_reply = command_service.handle(incoming)
    duplicate_reply = command_service.handle(incoming)

    assert first_reply is not None
    assert duplicate_reply is None
    assert provider.calls == 1
    with create_session_factory(engine)() as session:
        assert session.query(Task).count() == 1
        assert session.query(FeishuEvent).count() == 1


def test_dispatch_crash_rolls_back_event_and_task_and_allows_retry(
    engine, monkeypatch
) -> None:
    add_authorized_repository(engine)
    provider = FakeProvider()
    command_service = service(engine, {"github": provider})
    incoming = message("派发 https://github.com/octo/demo/issues/7")
    original = application_issues_module.add_and_dispatch_issue

    def crash_after_dispatch(*args, **kwargs):
        task = original(*args, **kwargs)
        assert task.id is not None
        raise SystemExit("simulated crash")

    monkeypatch.setattr(
        application_issues_module, "add_and_dispatch_issue", crash_after_dispatch
    )
    with pytest.raises(SystemExit, match="simulated crash"):
        command_service.handle(incoming)

    with create_session_factory(engine)() as session:
        assert session.query(FeishuEvent).count() == 0
        assert session.query(Issue).count() == 0
        assert session.query(Task).count() == 0

    monkeypatch.setattr(application_issues_module, "add_and_dispatch_issue", original)
    assert command_service.handle(incoming).startswith("已派发为 RE-")


def test_dispatch_rejects_repository_that_is_not_enabled(engine) -> None:
    add_authorized_repository(engine, enabled=False)
    provider = FakeProvider()

    reply = service(engine, {"github": provider}).handle(
        message("派发 https://github.com/octo/demo/issues/7")
    )

    assert reply == "派发失败：该 Issue 所属仓库未由管理员授权"
    assert provider.calls == 0
    with create_session_factory(engine)() as session:
        event = session.query(FeishuEvent).one()
        assert event.status == "failed"
        assert session.query(Task).count() == 0


def test_dispatch_rejects_closed_issue(engine) -> None:
    add_authorized_repository(engine)
    provider = FakeProvider(issue_state="closed")

    reply = service(engine, {"github": provider}).handle(
        message("派发 https://github.com/octo/demo/issues/7")
    )

    assert reply == "派发失败：只有待处理的开放 Issue 可以派发"
    with create_session_factory(engine)() as session:
        assert session.query(Task).count() == 0


def test_dispatch_hides_unexpected_provider_errors(engine) -> None:
    add_authorized_repository(engine)

    class FailingProvider(FakeProvider):
        def get_issue(self, repository, number):
            raise RuntimeError("secret token and C:/internal/workspace")

    reply = service(engine, {"github": FailingProvider()}).handle(
        message("派发 https://github.com/octo/demo/issues/7")
    )

    assert reply == "派发失败：内部错误，请稍后重试"
    with create_session_factory(engine)() as session:
        event = session.query(FeishuEvent).one()
        assert event.status == "failed"
        assert event.error_summary == "RuntimeError"


def test_review_command_creates_rv_task(engine) -> None:
    add_authorized_repository(engine)

    reply = service(engine).handle(
        message("检视 https://github.com/octo/demo/pull/7")
    )

    assert reply == "已创建检视任务 RV-1，正在排队"
    with create_session_factory(engine)() as session:
        task = session.query(PRReviewTask).one()
        assert task.status == "queued"
        assert task.pr_number == 7
        assert task.pr_url == "https://github.com/octo/demo/pull/7"
        assert task.source_chat_id == "oc_1"
        assert task.source_message_id == "om_1"
        assert task.source_sender_open_id == "ou_1"
        assert session.query(FeishuEvent).one().status == "processed"


def test_gitcode_review_command_creates_rv_task(engine) -> None:
    add_authorized_repository(engine, provider="gitcode")
    forges = ForgeRegistry({"gitcode": FakeReviewForge()})

    reply = service(engine, forges=forges).handle(
        message("检视 https://gitcode.com/octo/demo/pull/7")
    )

    assert reply == "已创建检视任务 RV-1，正在排队"
    with create_session_factory(engine)() as session:
        task = session.query(PRReviewTask).one()
        assert task.repository.provider == "gitcode"
        assert task.pr_url == "https://gitcode.com/octo/demo/pull/7"


def test_review_without_github_forge_returns_platform_configuration_error(engine) -> None:
    add_authorized_repository(engine)

    reply = service(engine, forges=ForgeRegistry()).handle(
        message("检视 https://github.com/octo/demo/pull/7")
    )

    assert reply == "检视失败：GitHub 平台尚未配置"
    with create_session_factory(engine)() as session:
        assert session.query(PRReviewTask).count() == 0
        assert session.query(FeishuEvent).one().status == "failed"


def test_review_registry_is_evaluated_for_each_command(engine) -> None:
    add_authorized_repository(engine)
    forges = ForgeRegistry()
    sessions = create_session_factory(engine)
    command_service = FeishuCommandService(
        session_factory=sessions,
        issues=IssueCommands(session_factory=sessions, providers={}),
        reviews=ReviewCommands(session_factory=sessions, forges=forges),
    )

    unavailable_reply = command_service.handle(
        message(
            "检视 https://github.com/octo/demo/pull/7",
            message_id="before-install",
        )
    )
    forges.install("github", FakeReviewForge())
    available_reply = command_service.handle(
        message(
            "检视 https://github.com/octo/demo/pull/7",
            message_id="after-install",
        )
    )

    assert unavailable_reply == "检视失败：GitHub 平台尚未配置"
    assert available_reply == "已创建检视任务 RV-1，正在排队"


def test_missing_review_publisher_does_not_affect_status_or_dispatch(engine) -> None:
    add_authorized_repository(engine)
    provider = FakeProvider()
    command_service = service(
        engine,
        {"github": provider},
        forges=ForgeRegistry(),
    )

    status_reply = command_service.handle(message("状态", message_id="om_status"))
    dispatch_reply = command_service.handle(
        message(
            "派发 https://github.com/octo/demo/issues/7",
            message_id="om_dispatch",
        )
    )

    assert status_reply is not None
    assert status_reply.startswith("任务状态：")
    assert dispatch_reply is not None
    assert dispatch_reply.startswith("已派发为 RE-")
    with create_session_factory(engine)() as session:
        assert session.query(Task).count() == 1
        assert session.query(PRReviewTask).count() == 0


def test_agent_authentication_gate_returns_configured_reason(engine) -> None:
    sessions = create_session_factory(engine)
    command_service = FeishuCommandService(
        session_factory=sessions,
        issues=IssueCommands(session_factory=sessions, providers={}),
        reviews=ReviewCommands(session_factory=sessions, forges=ForgeRegistry()),
        can_mutate=lambda: False,
        mutation_block_reason=lambda: "Codex 认证未就绪，Agent 执行已阻止",
    )

    reply = command_service.handle(
        message(
            "派发 https://github.com/octo/demo/issues/7",
            message_id="auth-unavailable",
        )
    )

    assert reply == "Codex 认证未就绪，Agent 执行已阻止"
    with create_session_factory(engine)() as session:
        assert session.query(Task).count() == 0


def test_duplicate_review_message_does_not_enqueue_a_second_task(engine) -> None:
    add_authorized_repository(engine)
    command_service = service(engine)
    incoming = message("检视 https://github.com/octo/demo/pull/7")

    first_reply = command_service.handle(incoming)
    duplicate_reply = command_service.handle(incoming)

    assert first_reply == "已创建检视任务 RV-1，正在排队"
    assert duplicate_reply is None
    with create_session_factory(engine)() as session:
        assert session.query(PRReviewTask).count() == 1
        assert session.query(FeishuEvent).count() == 1


def test_review_crash_rolls_back_event_and_task_and_allows_retry(
    engine, monkeypatch
) -> None:
    add_authorized_repository(engine)
    command_service = service(engine)
    incoming = message("检视 https://github.com/octo/demo/pull/7")
    original = application_reviews_module.enqueue_pr_review

    def crash_after_enqueue(*args, **kwargs):
        task = original(*args, **kwargs)
        assert task.id is not None
        raise SystemExit("simulated crash")

    monkeypatch.setattr(
        application_reviews_module, "enqueue_pr_review", crash_after_enqueue
    )
    with pytest.raises(SystemExit, match="simulated crash"):
        command_service.handle(incoming)

    with create_session_factory(engine)() as session:
        assert session.query(FeishuEvent).count() == 0
        assert session.query(PRReviewTask).count() == 0

    monkeypatch.setattr(application_reviews_module, "enqueue_pr_review", original)
    assert command_service.handle(incoming) == "已创建检视任务 RV-1，正在排队"


def test_review_rejects_disabled_repository(engine) -> None:
    add_authorized_repository(engine, enabled=False)

    reply = service(engine).handle(
        message("检视 https://github.com/octo/demo/pull/7")
    )

    assert reply == "检视失败：该 PR 所属仓库未由管理员授权"
    with create_session_factory(engine)() as session:
        assert session.query(PRReviewTask).count() == 0
        assert session.query(FeishuEvent).one().status == "failed"


def test_review_hides_invalid_provider_url_error(engine) -> None:
    reply = service(engine).handle(
        message("检视 https://gitcode.com/acme/widgets/pull/0")
    )

    assert reply == "检视失败：PR URL 无效，请使用完整的 GitHub 或 GitCode PR URL"
    with create_session_factory(engine)() as session:
        event = session.query(FeishuEvent).one()
        assert event.status == "failed"
        assert event.error_summary == "InvalidProviderUrl"


class FakeAssistant:
    def __init__(self, reply: str = "智能回答内容") -> None:
        self.reply = reply
        self.questions: list[str] = []

    def answer(self, question: str, session) -> str:
        self.questions.append(question)
        return self.reply


def test_free_form_question_routes_to_assistant(engine) -> None:
    assistant = FakeAssistant()

    reply = service(engine, assistant=assistant).handle(message("如何写好单元测试？"))

    assert reply == "智能回答内容"
    assert assistant.questions == ["如何写好单元测试？"]
    with create_session_factory(engine)() as session:
        event = session.query(FeishuEvent).one()
        assert event.status == "processed"
        assert event.reply_text == reply


def test_fixed_commands_bypass_assistant(engine) -> None:
    assistant = FakeAssistant()

    reply = service(engine, assistant=assistant).handle(message("状态"))

    assert reply is not None
    assert reply.startswith("任务状态：")
    assert assistant.questions == []


def test_help_mentions_natural_language_when_assistant_enabled(engine) -> None:
    reply = service(engine, assistant=FakeAssistant()).handle(message("帮助"))

    assert reply is not None
    assert "自然语言" in reply


def test_assistant_internal_error_reports_generic_failure(engine) -> None:
    class BrokenAssistant:
        def answer(self, question: str, session) -> str:
            raise RuntimeError("boom")

    reply = service(engine, assistant=BrokenAssistant()).handle(message("随便问问"))

    assert reply == "处理失败：内部错误，请稍后重试"
    with create_session_factory(engine)() as session:
        assert session.query(FeishuEvent).one().status == "failed"
