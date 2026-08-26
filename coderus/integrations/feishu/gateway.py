from __future__ import annotations

import json
import logging
import multiprocessing
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .commands import IncomingFeishuMessage

QUEUE_CAPACITY = 256
BOT_DISPLAY_NAME = "Coderus"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayFailure:
    error_type: str
    generation: int = 0


@dataclass(frozen=True)
class GatewayReconnecting:
    generation: int = 0


@dataclass(frozen=True)
class GatewayConnected:
    generation: int = 0


def _put_gateway_item(output: Any, item: object) -> bool:
    try:
        output.put_nowait(item)
    except queue.Full:
        logger.warning(
            "feishu_gateway_queue_full",
            extra={"event": "feishu_gateway_queue_full", "item_type": type(item).__name__},
        )
        return False
    return True


def _run_websocket(
    app_id: str, app_secret: str, output: Any, generation: int = 0
) -> None:
    try:
        import lark_oapi as lark

        def receive(data: Any) -> None:
            message = normalize_message_event(data)
            if message is not None:
                _put_gateway_item(output, message)

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
        # The SDK blocks inside start(), so report successful initial and retry
        # connections from its connection coroutine to let the parent supervise it.
        original_connect = client._connect

        async def connect_and_report() -> None:
            await original_connect()
            _put_gateway_item(output, GatewayConnected(generation))

        client._connect = connect_and_report
        client.on_reconnecting = lambda: _put_gateway_item(
            output, GatewayReconnecting(generation)
        )
        client.start()
        _put_gateway_item(output, GatewayFailure("ConnectionClosed", generation))
    except Exception as exc:
        _put_gateway_item(output, GatewayFailure(type(exc).__name__, generation))


class FeishuGateway:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        on_message: Callable[[IncomingFeishuMessage], None],
        on_error: Callable[[str], None] | None = None,
        on_recovered: Callable[[], None] | None = None,
        process_context: Any | None = None,
        reconnect_timeout_seconds: float = 15 * 60,
        restart_backoff_seconds: float = 5.0,
        max_restart_backoff_seconds: float = 5 * 60,
    ) -> None:
        if reconnect_timeout_seconds <= 0:
            raise ValueError("reconnect_timeout_seconds must be positive")
        if restart_backoff_seconds < 0:
            raise ValueError("restart_backoff_seconds must not be negative")
        if max_restart_backoff_seconds < restart_backoff_seconds:
            raise ValueError(
                "max_restart_backoff_seconds must not be less than restart_backoff_seconds"
            )
        self._app_id = app_id
        self._app_secret = app_secret
        self._on_message = on_message
        self._on_error = on_error or (lambda _error: None)
        self._on_recovered = on_recovered or (lambda: None)
        self._context = process_context or multiprocessing.get_context("spawn")
        self._reconnect_timeout_seconds = reconnect_timeout_seconds
        self._restart_backoff_seconds = restart_backoff_seconds
        self._max_restart_backoff_seconds = max_restart_backoff_seconds
        self._output: Any | None = None
        self._process: Any | None = None
        self._generation = 0
        self._process_lock = threading.Lock()
        self._relay_thread: threading.Thread | None = None
        self._relay_stop = threading.Event()

    def start(self) -> None:
        if self._process is not None:
            return
        self._relay_stop.clear()
        self._output = self._context.Queue(maxsize=QUEUE_CAPACITY)
        try:
            self._start_process()
        except BaseException:
            self._output = None
            raise
        relay_thread = threading.Thread(target=self._relay, daemon=True)
        try:
            relay_thread.start()
        except BaseException:
            self.stop()
            raise
        self._relay_thread = relay_thread

    def stop(self) -> None:
        if self._process is None and self._relay_thread is None and self._output is None:
            return
        self._relay_stop.set()
        self._stop_process()
        if self._relay_thread is not None:
            self._relay_thread.join(timeout=2)
        output = self._output
        close = getattr(output, "close", None)
        if close is not None:
            close()
        join_thread = getattr(output, "join_thread", None)
        if join_thread is not None:
            join_thread()
        self._process = None
        self._relay_thread = None
        self._output = None

    def is_running(self) -> bool:
        with self._process_lock:
            process_running = self._process is not None and self._process.is_alive()
        return bool(
            process_running
            and self._relay_thread is not None
            and self._relay_thread.is_alive()
        )

    def _relay(self) -> None:
        reconnect_started_at: float | None = None
        restart_attempts = 0
        while not self._relay_stop.is_set():
            try:
                item = self._output.get(timeout=0.2)
            except queue.Empty:
                if (
                    reconnect_started_at is not None
                    and time.monotonic() - reconnect_started_at
                    >= self._reconnect_timeout_seconds
                ):
                    self._on_error("ReconnectTimeout")
                    restart_attempts += 1
                    if not self._restart_process(restart_attempts):
                        return
                    reconnect_started_at = None
                elif not self._process_is_alive():
                    self._on_error("ConnectionClosed")
                    restart_attempts += 1
                    if not self._restart_process(restart_attempts):
                        return
                continue
            if isinstance(item, GatewayFailure):
                if not self._is_current_generation(item.generation):
                    continue
                self._on_error(item.error_type)
                restart_attempts += 1
                if not self._restart_process(restart_attempts):
                    return
                reconnect_started_at = None
                continue
            if isinstance(item, GatewayReconnecting):
                if not self._is_current_generation(item.generation):
                    continue
                if reconnect_started_at is None:
                    reconnect_started_at = time.monotonic()
                continue
            if isinstance(item, GatewayConnected):
                if not self._is_current_generation(item.generation):
                    continue
                reconnect_started_at = None
                restart_attempts = 0
                self._on_recovered()
                continue
            if isinstance(item, IncomingFeishuMessage):
                self._on_message(item)

    def _start_process(self) -> None:
        with self._process_lock:
            if self._relay_stop.is_set():
                return
            process = self._context.Process(
                target=_run_websocket,
                args=(
                    self._app_id,
                    self._app_secret,
                    self._output,
                    self._generation + 1,
                ),
                daemon=True,
            )
            process.start()
            self._generation += 1
            self._process = process

    def _stop_process(self) -> None:
        with self._process_lock:
            process = self._process
            self._process = None
            if process is None:
                return
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    def _process_is_alive(self) -> bool:
        with self._process_lock:
            return self._process is not None and self._process.is_alive()

    def _is_current_generation(self, generation: int) -> bool:
        with self._process_lock:
            return generation in {0, self._generation}

    def _restart_process(self, attempt: int) -> bool:
        self._stop_process()
        delay = min(
            self._restart_backoff_seconds * (2 ** min(attempt - 1, 20)),
            self._max_restart_backoff_seconds,
        )
        if self._relay_stop.wait(delay):
            return False
        try:
            self._start_process()
        except Exception as exc:
            self._on_error(type(exc).__name__)
        return not self._relay_stop.is_set()


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
    mentioned_bot = False
    for mention in mentions:
        name = getattr(mention, "name", None)
        if isinstance(name, str) and name.strip().casefold() == BOT_DISPLAY_NAME.casefold():
            mentioned_bot = True
        key = getattr(mention, "key", None)
        if isinstance(key, str) and key:
            placeholder = key if key.startswith("@") else f"@{key}"
            text = text.replace(placeholder, "")
            text = text.replace(key, "")

    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    sender_open_id = getattr(sender_id, "open_id", None)
    if not isinstance(sender_open_id, str) or not sender_open_id:
        sender_open_id = None
    header = getattr(data, "header", None)
    return IncomingFeishuMessage(
        message_id=message_id,
        event_id=getattr(header, "event_id", None),
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        text=" ".join(text.split()),
        mentioned_bot=mentioned_bot,
    )
