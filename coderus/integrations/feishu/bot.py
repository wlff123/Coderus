from __future__ import annotations

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
    ) -> None:
        self._service = service
        self._client = client
        self._gateway = gateway_factory(self._handle)
        self.last_error: str | None = None

    def start(self) -> None:
        self._gateway.start()

    def stop(self) -> None:
        self._gateway.stop()

    def is_running(self) -> bool:
        check = getattr(self._gateway, "is_running", None)
        return True if check is None else bool(check())

    def _handle(self, message: IncomingFeishuMessage) -> None:
        try:
            response = self._service.handle(message)
        except Exception as exc:
            self.last_error = type(exc).__name__
            self._send(
                message.chat_id,
                "命令处理失败，请稍后重试或在网页查看任务状态。",
            )
            return
        if response is not None:
            self._send(message.chat_id, response)

    def _send(self, chat_id: str, text: str) -> None:
        try:
            self._client.send_text(chat_id, "chat_id", text)
        except Exception as exc:
            self.last_error = type(exc).__name__
