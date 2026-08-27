from __future__ import annotations

import pytest

from coderus.application.reviews import ReviewCommands, ReviewSource
from coderus.forge import ForgeNotConfigured
from coderus.models import PRReviewTask

from .conftest import seed_issue

SOURCE = ReviewSource(chat_id="oc_1", message_id="om_1", sender_id="ou_1")


class FakeForges:
    def __init__(self, *, configured: bool = True) -> None:
        self._configured = configured

    def supports(self, provider: str, *capabilities) -> bool:
        return self._configured


def test_enqueue_persists_review_task_with_source(session_factory) -> None:
    with session_factory() as session:
        seed_issue(session)

    commands = ReviewCommands(session_factory=session_factory, forges=FakeForges())
    review_id = commands.enqueue("https://github.com/octo/demo/pull/5", SOURCE)

    with session_factory() as session:
        review = session.get(PRReviewTask, review_id)
        assert review is not None
        assert review.pr_number == 5
        assert review.status == "queued"
        assert review.source_chat_id == "oc_1"
        assert review.source_message_id == "om_1"
        assert review.source_sender_open_id == "ou_1"


def test_enqueue_rejects_unconfigured_forge(session_factory) -> None:
    with session_factory() as session:
        seed_issue(session)

    commands = ReviewCommands(
        session_factory=session_factory, forges=FakeForges(configured=False)
    )
    with pytest.raises(ForgeNotConfigured):
        commands.enqueue("https://github.com/octo/demo/pull/5", SOURCE)


def test_enqueue_rejects_unauthorized_repository(session_factory) -> None:
    commands = ReviewCommands(session_factory=session_factory, forges=FakeForges())

    with pytest.raises(ValueError, match="未由管理员授权"):
        commands.enqueue("https://github.com/other/repo/pull/5", SOURCE)


def test_enqueue_in_session_leaves_commit_to_caller(session_factory) -> None:
    with session_factory() as session:
        seed_issue(session)

    commands = ReviewCommands(session_factory=session_factory, forges=FakeForges())
    with session_factory() as session:
        commands.enqueue_in_session(
            session, "https://github.com/octo/demo/pull/5", SOURCE
        )
        session.rollback()

    with session_factory() as session:
        assert session.query(PRReviewTask).count() == 0
