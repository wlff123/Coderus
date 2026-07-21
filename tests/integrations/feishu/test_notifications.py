from datetime import UTC, datetime

import pytest

from coderus.db import create_session_factory
from coderus.models import FeishuEvent, Issue, Repository, Task, User
from coderus.workflow.notifications import FeishuTaskNotifier


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_task_completed(
        self,
        message,
        *,
        receive_id: str,
        receive_id_type: str,
    ) -> None:
        self.calls.append(
            {
                "message": message,
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
            }
        )


def create_task(session) -> Task:
    user = User(username="feishu-bot", password_hash="hash")
    repository = Repository(
        provider="github",
        owner="octo",
        name="demo",
        canonical_url="https://github.com/octo/demo",
        created_by_user=user,
    )
    issue = Issue(
        repository=repository,
        external_id="1",
        number=1,
        title="Fix it",
        state="open",
        source_url="https://github.com/octo/demo/issues/1",
    )
    task = Task(issue=issue, creator=user)
    session.add(task)
    session.flush()
    return task


@pytest.mark.asyncio
async def test_feishu_task_completion_returns_to_originating_chat(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task = create_task(session)
        session.add(
            FeishuEvent(
                message_id="om-1",
                event_id="evt-1",
                chat_id="oc-origin",
                chat_type="group",
                sender_open_id="ou-user",
                command="派发 https://github.com/octo/demo/issues/1",
                status="processed",
                task_id=task.id,
                processed_at=datetime.now(UTC),
            )
        )
        session.commit()
        task_id = task.id
    client = RecordingClient()
    notifier = FeishuTaskNotifier(client, session_factory=sessions, default_chat_id="oc-default")

    await notifier.notify(
        database_task_id=task_id,
        task_id=f"RE-{task_id}",
        repository="octo/demo",
        issue="#1 Fix it",
        creator="feishu-bot",
        pr_url="https://github.com/octo/demo/pull/2",
    )

    assert client.calls[0]["receive_id"] == "oc-origin"
    assert client.calls[0]["receive_id_type"] == "chat_id"


@pytest.mark.asyncio
async def test_web_task_completion_uses_default_chat(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task = create_task(session)
        session.commit()
        task_id = task.id
    client = RecordingClient()
    notifier = FeishuTaskNotifier(client, session_factory=sessions, default_chat_id="oc-default")

    await notifier.notify(
        database_task_id=task_id,
        task_id=f"RE-{task_id}",
        repository="octo/demo",
        issue="#1 Fix it",
        creator="admin",
        pr_url="https://github.com/octo/demo/pull/2",
    )

    assert client.calls[0]["receive_id"] == "oc-default"


@pytest.mark.asyncio
async def test_web_task_without_default_chat_skips_notification(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task = create_task(session)
        session.commit()
        task_id = task.id
    client = RecordingClient()
    notifier = FeishuTaskNotifier(client, session_factory=sessions, default_chat_id=None)

    await notifier.notify(
        database_task_id=task_id,
        task_id=f"RE-{task_id}",
        repository="octo/demo",
        issue="#1 Fix it",
        creator="admin",
        pr_url="https://github.com/octo/demo/pull/2",
    )

    assert client.calls == []
