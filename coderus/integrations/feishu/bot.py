from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from .commands import IncomingFeishuMessage


class CommandService(Protocol):
    def handle(self, message: IncomingFeishuMessage) -> str | None: ...


class MessageClient(Protocol):
    def send_text(self, receive_id: str, receive_id_type: str, text: str) -> object: ...


class Gateway(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class FeishuBot:
    def __init__(
        self,
        *,
        service: CommandService,
        client: MessageClient,
        gateway_factory: Callable[[Callable[[IncomingFeishuMessage], None]], Gateway],
        retry_interval_seconds: float = 5.0,
    ) -> None:
        if retry_interval_seconds <= 0:
            raise ValueError("retry_interval_seconds must be positive")
        self._service = service
        self._client = client
        self._gateway = gateway_factory(self._handle)
        self._retry_interval_seconds = retry_interval_seconds
        self._retry_stop = threading.Event()
        self._delivery_lock = threading.Lock()
        self._retry_thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self._drain_pending()
        self._gateway.start()
        self._retry_stop.clear()
        self._retry_thread = threading.Thread(
            target=self._retry_loop,
            name="coderus-feishu-outbox",
            daemon=True,
        )
        self._retry_thread.start()

    def stop(self) -> None:
        self._retry_stop.set()
        self._gateway.stop()
        if self._retry_thread is not None:
            self._retry_thread.join(timeout=max(1.0, self._retry_interval_seconds + 1.0))
            self._retry_thread = None

    def is_running(self) -> bool:
        check = getattr(self._gateway, "is_running", None)
        return True if check is None else bool(check())

    def _handle(self, message: IncomingFeishuMessage) -> None:
        try:
            response = self._service.handle(message)
        except Exception as exc:
            self.last_error = type(exc).__name__
            self._send(
                message.message_id,
                message.chat_id,
                "命令处理失败，请稍后重试或在网页查看任务状态。",
            )
            return
        if response is not None:
            self._send(message.message_id, message.chat_id, response)

    def _send(self, message_id: str, chat_id: str, text: str) -> None:
        with self._delivery_lock:
            self._send_unlocked(message_id, chat_id, text)

    def _send_unlocked(self, message_id: str, chat_id: str, text: str) -> None:
        error = None
        try:
            self._client.send_text(chat_id, "chat_id", text)
        except Exception as exc:
            self.last_error = type(exc).__name__
            error = type(exc).__name__
        mark_result = getattr(self._service, "mark_reply_result", None)
        if mark_result is not None:
            mark_result(message_id, error)

    def _retry_loop(self) -> None:
        while not self._retry_stop.wait(self._retry_interval_seconds):
            self._drain_pending()

    def _drain_pending(self) -> None:
        pending = getattr(self._service, "pending_replies", None)
        if pending is None:
            return
        with self._delivery_lock:
            for message_id, chat_id, text in pending():
                self._send_unlocked(message_id, chat_id, text)
