from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coderus.application import IssueCommands, ReviewCommands, ReviewSource
from coderus.integrations.feishu.settings import ensure_feishu_bot_user
from coderus.models import FeishuEvent, Task
from coderus.providers.errors import InvalidProviderUrl, ProviderError
from coderus.tasks.statuses import RUNNING_TASK_STATES

from .commands import IncomingFeishuMessage, parse_command

HELP_TEXT = """我是 Coderus，您的代码仓管家。
我可以帮您处理代码仓库 Issue、跟踪任务、创建 PR 和检视 PR。

当前支持的命令：
帮助
状态
任务
任务 RE-N
派发 <Issue URL>
检视 <GitHub 或 GitCode PR URL>"""

ASSISTANT_HELP_TEXT = HELP_TEXT + "\n\n除命令外，也可以直接用自然语言向我提问。"

TASK_STATUS_LABELS = {
    "queued": "排队中",
    "preparing": "准备工作区",
    "developer_working": "开发处理中",
    "reviewing": "审核中",
    "developer_revising": "修改中",
    "sealing": "确认提交",
    "publishing": "发布 PR",
    "awaiting_human_review": "等待人工审核",
    "completed": "已完成",
    "closed": "PR 已关闭",
    "dismissed": "已关闭",
    "failed": "失败",
    "cancelled": "已取消",
    "cancelling": "正在取消",
    "manual_intervention": "需要人工处理",
}


class Assistant(Protocol):
    def answer(self, question: str, session: Session) -> str: ...


def _operation_label(kind: str) -> str:
    return {"review": "检视", "dispatch": "派发"}.get(kind, "处理")


class FeishuCommandService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        issues: IssueCommands,
        reviews: ReviewCommands,
        assistant: Assistant | None = None,
        can_mutate: Callable[[], bool] | None = None,
        mutation_block_reason: Callable[[], str] | None = None,
    ) -> None:
        self.sessions = session_factory
        self.issues = issues
        self.reviews = reviews
        self.assistant = assistant
        self.can_mutate = can_mutate or (lambda: True)
        self.mutation_block_reason = mutation_block_reason or (
            lambda: "系统正在发布新版本，暂不接收新任务，请稍后重试"
        )

    def handle(self, message: IncomingFeishuMessage) -> str | None:
        if message.chat_type == "group" and not message.mentioned_bot:
            return None

        with self.sessions() as session:
            if not message.sender_open_id:
                event = self._event(message, status="failed")
                event.error_summary = "missing sender_open_id"
                event.processed_at = datetime.now(UTC)
                reply = "无法确认发送者身份，已拒绝处理"
                self._queue_reply(event, reply)
                session.add(event)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    return None
                return reply
            event = self._event(message, status="processing")
            session.add(event)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None

            command = parse_command(message.text)
            try:
                if command.kind in {"dispatch", "review"} and not self.can_mutate():
                    reply = self.mutation_block_reason()
                elif command.kind == "help":
                    reply = HELP_TEXT if self.assistant is None else ASSISTANT_HELP_TEXT
                elif command.kind == "status":
                    reply = self._status(session)
                elif command.kind == "tasks":
                    reply = self._tasks(session)
                elif command.kind == "task" and command.argument is not None:
                    reply = self._task(session, command.argument)
                elif command.kind == "review" and command.argument is not None:
                    review_id = self.reviews.enqueue_in_session(
                        session,
                        command.argument,
                        ReviewSource(
                            chat_id=message.chat_id,
                            message_id=message.message_id,
                            sender_id=message.sender_open_id or "",
                        ),
                    )
                    reply = f"已创建检视任务 RV-{review_id}，正在排队"
                elif command.kind == "dispatch" and command.argument is not None:
                    creator = ensure_feishu_bot_user(session)
                    task_id = self.issues.add_and_dispatch_in_session(
                        session, command.argument, creator.id
                    )
                    event.task_id = task_id
                    task = session.get(Task, task_id)
                    assert task is not None
                    issue = task.issue
                    repository = issue.repository
                    reply = (
                        f"已派发为 RE-{task.id}：{repository.provider}/"
                        f"{repository.owner}/{repository.name}#{issue.number} {issue.title}"
                    )
                elif self.assistant is not None:
                    reply = self.assistant.answer(message.text, session)
                else:
                    reply = f"无法识别命令。\n{HELP_TEXT}"
            except (ProviderError, ValueError) as exc:
                session.rollback()
                reason = str(exc)
                if command.kind == "review" and isinstance(exc, InvalidProviderUrl):
                    reason = "PR URL 无效，请使用完整的 GitHub 或 GitCode PR URL"
                    error_summary = type(exc).__name__
                else:
                    error_summary = reason[:1000]
                event = session.get(FeishuEvent, event.id)
                assert event is not None
                event.status = "failed"
                event.error_summary = error_summary
                event.processed_at = datetime.now(UTC)
                reply = f"{_operation_label(command.kind)}失败：{reason}"
                self._queue_reply(event, reply)
                session.commit()
                return reply
            except Exception as exc:
                session.rollback()
                event = session.get(FeishuEvent, event.id)
                assert event is not None
                event.status = "failed"
                event.error_summary = type(exc).__name__
                event.processed_at = datetime.now(UTC)
                reply = f"{_operation_label(command.kind)}失败：内部错误，请稍后重试"
                self._queue_reply(event, reply)
                session.commit()
                return reply
            except BaseException:
                session.rollback()
                event = session.get(FeishuEvent, event.id)
                if event is not None:
                    session.delete(event)
                    session.commit()
                raise

            event.status = "processed"
            event.processed_at = datetime.now(UTC)
            self._queue_reply(event, reply)
            session.commit()
            return reply

    def pending_replies(self, *, limit: int = 100) -> list[tuple[str, str, str]]:
        now = datetime.now(UTC)
        with self.sessions() as session:
            rows = session.execute(
                select(FeishuEvent.message_id, FeishuEvent.chat_id, FeishuEvent.reply_text)
                .where(
                    FeishuEvent.reply_status == "pending",
                    FeishuEvent.reply_text.is_not(None),
                    or_(
                        FeishuEvent.reply_next_attempt_at.is_(None),
                        FeishuEvent.reply_next_attempt_at <= now,
                    ),
                )
                .order_by(FeishuEvent.id)
                .limit(limit)
            ).all()
            return [(message_id, chat_id, text) for message_id, chat_id, text in rows]

    def mark_reply_result(self, message_id: str, error: str | None = None) -> None:
        with self.sessions() as session:
            event = session.scalar(
                select(FeishuEvent).where(FeishuEvent.message_id == message_id)
            )
            if event is None or event.reply_status != "pending":
                return
            event.reply_attempts += 1
            event.reply_error = error[:1000] if error else None
            if error is None:
                event.reply_status = "sent"
                event.reply_sent_at = datetime.now(UTC)
                event.reply_next_attempt_at = None
            else:
                delay_seconds = min(300, 5 * (2 ** min(event.reply_attempts - 1, 6)))
                event.reply_next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=delay_seconds
                )
            session.commit()

    @staticmethod
    def _queue_reply(event: FeishuEvent, reply: str) -> None:
        event.reply_text = reply
        event.reply_status = "pending"
        event.reply_error = None
        event.reply_next_attempt_at = None

    @staticmethod
    def _event(message: IncomingFeishuMessage, *, status: str) -> FeishuEvent:
        return FeishuEvent(
            message_id=message.message_id,
            event_id=message.event_id or "",
            chat_id=message.chat_id,
            chat_type=message.chat_type,
            sender_open_id=message.sender_open_id or "<missing>",
            command=message.text,
            status=status,
        )

    @staticmethod
    def _status(session: Session) -> str:
        counts = dict(
            session.execute(
                select(Task.status, func.count()).group_by(Task.status)
            ).all()
        )
        running = sum(counts.get(status, 0) for status in RUNNING_TASK_STATES)
        return "\n".join(
            (
                "任务状态：",
                f"排队中：{counts.get('queued', 0)}",
                f"执行中：{running}",
                f"等待人工审核：{counts.get('awaiting_human_review', 0)}",
                f"需要人工处理：{counts.get('manual_intervention', 0)}",
                f"失败：{counts.get('failed', 0)}",
            )
        )

    @staticmethod
    def _tasks(session: Session) -> str:
        tasks = session.scalars(select(Task).order_by(Task.id.desc()).limit(10)).all()
        if not tasks:
            return "暂无任务"
        lines = ["最近 10 条任务："]
        for task in tasks:
            issue = task.issue
            lines.append(
                f"RE-{task.id} {TASK_STATUS_LABELS.get(task.status, task.status)}"
                f"（{task.status}） {issue.repository.owner}/{issue.repository.name}"
                f"#{issue.number} {issue.title}"
            )
        return "\n".join(lines)

    @staticmethod
    def _task(session: Session, reference: str) -> str:
        task = session.get(Task, int(reference.removeprefix("RE-")))
        if task is None:
            return f"未找到任务 {reference}"
        issue = task.issue
        repository = issue.repository
        return "\n".join(
            (
                f"RE-{task.id}",
                f"阶段：{TASK_STATUS_LABELS.get(task.status, task.status)}（{task.status}）",
                f"仓库：{repository.provider}/{repository.owner}/{repository.name}",
                f"Issue：{issue.source_url or '-'}",
                f"失败摘要：{task.failure_summary or '-'}",
                f"PR：{task.pr_url or '-'}",
            )
        )
