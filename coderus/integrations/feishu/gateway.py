from __future__ import annotations

import json
import multiprocessing
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .commands import IncomingFeishuMessage

QUEUE_CAPACITY = 256


@dataclass(frozen=True)
class GatewayFailure:
    error_type: str


def _run_websocket(app_id: str, app_secret: str, output: Any) -> None:
    try:
        import lark_oapi as lark

        def receive(data: Any) -> None:
            message = normalize_message_event(data)
            if message is not None:
                try:
                    output.put_nowait(message)
                except queue.Full:
                    pass

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(receive)
            .build()
        )
        client = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        client.start()
        output.put_nowait(GatewayFailure("ConnectionClosed"))
    except Exception as exc:
        try:
            output.put_nowait(GatewayFailure(type(exc).__name__))
        except queue.Full:
            pass


class FeishuGateway:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        on_message: Callable[[IncomingFeishuMessage], None],
        on_error: Callable[[str], None] | None = None,
        process_context: Any | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._on_message = on_message
        self._on_error = on_error or (lambda _error: None)
        self._context = process_context or multiprocessing.get_context("spawn")
        self._output: Any | None = None
        self._process: Any | None = None
        self._relay_thread: threading.Thread | None = None
        self._relay_stop = threading.Event()

    def start(self) -> None:
        if self._process is not None:
            return
        self._relay_stop.clear()
        self._output = self._context.Queue(maxsize=QUEUE_CAPACITY)
        process = self._context.Process(
            target=_run_websocket,
            args=(self._app_id, self._app_secret, self._output),
            daemon=True,
        )
        try:
            process.start()
        except BaseException:
            self._output = None
            raise
        self._process = process
        relay_thread = threading.Thread(target=self._relay, daemon=True)
        try:
            relay_thread.start()
        except BaseException:
            self.stop()
            raise
        self._relay_thread = relay_thread

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        self._relay_stop.set()
        if self._relay_thread is not None:
            self._relay_thread.join(timeout=2)
        self._process = None
        self._relay_thread = None
        self._output = None

    def is_running(self) -> bool:
        return bool(
            self._process is not None
            and self._process.is_alive()
            and self._relay_thread is not None
            and self._relay_thread.is_alive()
        )

    def _relay(self) -> None:
        while not self._relay_stop.is_set():
            try:
                item = self._output.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(item, GatewayFailure):
                self._on_error(item.error_type)
                return
            if isinstance(item, IncomingFeishuMessage):
                self._on_message(item)


def normalize_message_event(data: Any) -> IncomingFeishuMessage | None:
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    if message is None or getattr(message, "message_type", None) != "text":
        return None
    try:
        content = json.loads(message.content)
    except (AttributeError, TypeError, json.JSONDecodeError):
        return None
    text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(text, str):
        return None

    message_id = getattr(message, "message_id", None)
    chat_id = getattr(message, "chat_id", None)
    chat_type = getattr(message, "chat_type", None)
    if not all(isinstance(value, str) and value for value in (message_id, chat_id, chat_type)):
        return None

    mentions = list(getattr(message, "mentions", None) or [])
    for mention in mentions:
        key = getattr(mention, "key", None)
        if isinstance(key, str) and key:
            placeholder = key if key.startswith("@") else f"@{key}"
            text = text.replace(placeholder, "")
            text = text.replace(key, "")

    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    header = getattr(data, "header", None)
    return IncomingFeishuMessage(
        message_id=message_id,
        event_id=getattr(header, "event_id", None),
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=getattr(sender_id, "open_id", None),
        text=" ".join(text.split()),
        mentioned_bot=bool(mentions),
    )
