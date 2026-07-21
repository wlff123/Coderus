import re

import pytest

from coderus.db import create_session_factory
from coderus.integrations.feishu.commands import IncomingFeishuMessage
from coderus.models import PRReviewTask, Repository, User
from coderus.pr_review.service import enqueue_pr_review

PR_URL = "https://github.com/octo/demo/pull/7"


def message(url: str = PR_URL) -> IncomingFeishuMessage:
    return IncomingFeishuMessage(
        message_id="om_1",
        event_id="evt_1",
        chat_id="oc_1",
        chat_type="group",
        sender_open_id="ou_1",
        text=f"检视 {url}",
        mentioned_bot=True,
    )


def add_authorized_repository(engine, *, enabled: bool = True) -> None:
    with create_session_factory(engine)() as session:
        admin = User(username="admin", password_hash="hash", role="admin")
        session.add(
            Repository(
                provider="github",
                owner="octo",
                name="demo",
                canonical_url="https://github.com/octo/demo",
                default_branch="main",
                is_enabled=enabled,
                created_by_user=admin,
            )
        )
        session.commit()


def test_enqueue_review_requires_enabled_repository(engine) -> None:
    with create_session_factory(engine)() as session:
        with pytest.raises(ValueError, match="仓库未由管理员授权"):
            enqueue_pr_review(session, PR_URL, "oc_1", "om_1", "ou_1")


def test_enqueue_review_rejects_disabled_repository(engine) -> None:
    add_authorized_repository(engine, enabled=False)

    with create_session_factory(engine)() as session:
        with pytest.raises(ValueError, match="仓库未由管理员授权"):
            enqueue_pr_review(session, PR_URL, "oc_1", "om_1", "ou_1")


def test_enqueue_review_persists_queued_task_and_message_source(engine) -> None:
    add_authorized_repository(engine)

    with create_session_factory(engine)() as session:
        task = enqueue_pr_review(session, PR_URL, "oc_1", "om_1", "ou_1")

        assert task.id == 1
        assert task.status == "queued"
        assert task.review_key is not None
        assert len(task.review_key) >= 32
        assert re.fullmatch(r"[A-Za-z0-9_-]+", task.review_key)
        assert task.repository.provider == "github"
        assert task.repository.owner == "octo"
        assert task.repository.name == "demo"
        assert task.pr_number == 7
        assert task.pr_url == PR_URL
        assert task.source_chat_id == "oc_1"
        assert task.source_message_id == "om_1"
        assert task.source_sender_open_id == "ou_1"
        assert session.query(PRReviewTask).one() is task


def test_enqueue_gitcode_review_uses_platform_repository_and_explicit_source(engine) -> None:
    with create_session_factory(engine)() as session:
        admin = User(username="admin", password_hash="hash", role="admin")
        session.add(
            Repository(
                provider="gitcode",
                owner="octo",
                name="demo",
                canonical_url="https://gitcode.com/octo/demo",
                default_branch="main",
                created_by_user=admin,
            )
        )
        session.commit()

    with create_session_factory(engine)() as session:
        task = enqueue_pr_review(
            session,
            "https://gitcode.com/octo/demo/pulls/7",
            source_chat_id="",
            source_message_id="web-review:unique",
            source_sender_id="web-user:2",
        )

        assert task.repository.provider == "gitcode"
        assert task.source_chat_id == ""
        assert task.source_message_id == "web-review:unique"
        assert task.source_sender_open_id == "web-user:2"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Octo/demo/pull/7",
        "https://github.com/octo/Demo/pull/7",
    ],
)
def test_enqueue_review_rejects_repository_name_case_variants(engine, url: str) -> None:
    add_authorized_repository(engine)

    with create_session_factory(engine)() as session:
        with pytest.raises(ValueError, match="仓库未由管理员授权"):
            enqueue_pr_review(session, url, "oc_1", "om_1", "ou_1")
        assert session.query(PRReviewTask).count() == 0
