import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

import coderus.integrations.feishu.gateway as gateway_module
from coderus.integrations.feishu.commands import IncomingFeishuMessage
from coderus.integrations.feishu.gateway import (
    FeishuGateway,
    GatewayConnected,
    GatewayFailure,
    GatewayReconnecting,
    _put_gateway_item,
    normalize_message_event,
)


def event(
    *,
    text: str,
    chat_type: str = "group",
    message_type: str = "text",
    mentions: list[object] | None = None,
):
    return SimpleNamespace(
        header=SimpleNamespace(event_id="evt-1"),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou-sender")),
            message=SimpleNamespace(
                message_id="om-1",
                chat_id="oc-chat",
                chat_type=chat_type,
                message_type=message_type,
                content=json.dumps({"text": text}),
                mentions=mentions,
            ),
        ),
    )


def test_normalize_group_message_removes_bot_mention() -> None:
    data = event(
        text="@_user_1 任务 RE-5",
        mentions=[SimpleNamespace(key="@_user_1", name="Coderus")],
    )

    message = normalize_message_event(data)

    assert message is not None
    assert message.message_id == "om-1"
    assert message.event_id == "evt-1"
    assert message.chat_id == "oc-chat"
    assert message.chat_type == "group"
    assert message.sender_open_id == "ou-sender"
    assert message.text == "任务 RE-5"
    assert message.mentioned_bot is True


def test_normalize_removes_mention_key_with_or_without_at_prefix() -> None:
    data = event(
        text="@_user_1 状态",
        mentions=[SimpleNamespace(key="_user_1", name="Coderus")],
    )

    message = normalize_message_event(data)

    assert message is not None
    assert message.text == "状态"


def test_normalize_group_message_without_mention_is_retained_for_filtering() -> None:
    message = normalize_message_event(event(text="状态", mentions=[]))

    assert message is not None
    assert message.text == "状态"
    assert message.mentioned_bot is False


def test_normalize_group_message_mentioning_another_bot_is_not_for_coderus() -> None:
    message = normalize_message_event(
        event(
            text="@_user_2 帮助",
            mentions=[SimpleNamespace(key="@_user_2", name="Other Bot")],
        )
    )

    assert message is not None
    assert message.text == "帮助"
    assert message.mentioned_bot is False


def test_normalize_personal_message_does_not_require_mention() -> None:
    message = normalize_message_event(event(text="状态", chat_type="p2p"))

    assert message is not None
    assert message.chat_type == "p2p"
    assert message.mentioned_bot is False


def test_normalize_ignores_non_text_and_invalid_json() -> None:
    assert normalize_message_event(event(text="x", message_type="image")) is None
    malformed = event(text="x")
    malformed.event.message.content = "not-json"
    assert normalize_message_event(malformed) is None


def test_normalize_ignores_events_without_required_message_identifiers() -> None:
    missing_message_id = event(text="状态")
    missing_message_id.event.message.message_id = None
    missing_chat_id = event(text="状态")
    missing_chat_id.event.message.chat_id = None

    assert normalize_message_event(missing_message_id) is None
    assert normalize_message_event(missing_chat_id) is None


def test_normalize_preserves_event_without_sender_for_service_audit() -> None:
    missing_sender = event(text="状态")
    missing_sender.event.sender.sender_id.open_id = None

    normalized = normalize_message_event(missing_sender)
    assert normalized is not None
    assert normalized.sender_open_id is None


def test_queue_full_is_reported_with_structured_warning(caplog) -> None:
    output: queue.Queue[object] = queue.Queue(maxsize=1)
    output.put(object())

    with caplog.at_level("WARNING"):
        accepted = _put_gateway_item(output, incoming())

    assert accepted is False
    assert "feishu_gateway_queue_full" in caplog.text


class FakeProcess:
    def __init__(self) -> None:
        self.started = False
        self.terminated = False
        self.joined = False

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return self.started and not self.terminated

    def terminate(self) -> None:
        self.terminated = True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


class FakeContext:
    def __init__(self) -> None:
        self.queue: queue.Queue[object] = queue.Queue()
        self.processes: list[FakeProcess] = []
        self.process_target = None
        self.process_args = None

    @property
    def process(self) -> FakeProcess:
        return self.processes[-1]

    def Queue(self, *, maxsize: int):
        assert maxsize == 256
        return self.queue

    def Process(self, *, target, args, daemon):
        self.process_target = target
        self.process_args = args
        assert daemon is True
        process = FakeProcess()
        self.processes.append(process)
        return process


def incoming() -> IncomingFeishuMessage:
    return IncomingFeishuMessage(
        message_id="om-1",
        event_id="evt-1",
        chat_id="oc-chat",
        chat_type="group",
        sender_open_id="ou-sender",
        text="状态",
        mentioned_bot=True,
    )


def test_gateway_relays_messages_and_owns_child_process_lifecycle() -> None:
    context = FakeContext()
    received: list[IncomingFeishuMessage] = []
    delivered = threading.Event()

    def on_message(message: IncomingFeishuMessage) -> None:
        received.append(message)
        delivered.set()

    gateway = FeishuGateway(
        "cli-test",
        "secret",
        on_message=on_message,
        process_context=context,
    )

    gateway.start()
    context.queue.put(incoming())

    assert delivered.wait(1)
    assert received == [incoming()]
    assert context.process.started is True
    gateway.stop()
    assert context.process.terminated is True
    assert context.process.joined is True


def test_gateway_start_is_idempotent() -> None:
    context = FakeContext()
    gateway = FeishuGateway(
        "cli-test",
        "secret",
        on_message=lambda _message: None,
        process_context=context,
    )

    gateway.start()
    gateway.start()
    gateway.stop()

    assert context.process.started is True


def test_gateway_rolls_back_started_process_when_relay_thread_start_fails(
    monkeypatch,
) -> None:
    context = FakeContext()

    class FailingThread:
        def __init__(self, *, target, daemon) -> None:
            assert target is not None
            assert daemon is True

        def start(self) -> None:
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(gateway_module.threading, "Thread", FailingThread)
    gateway = FeishuGateway(
        "cli-test",
        "secret",
        on_message=lambda _message: None,
        process_context=context,
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        gateway.start()

    assert context.process.terminated is True
    assert context.process.joined is True
    assert gateway._process is None
    assert gateway._relay_thread is None
    assert gateway._output is None
    gateway.stop()


def test_gateway_reports_sanitized_child_failure() -> None:
    context = FakeContext()
    errors: list[str] = []
    delivered = threading.Event()

    def on_error(error: str) -> None:
        errors.append(error)
        delivered.set()

    gateway = FeishuGateway(
        "cli-test",
        "secret",
        on_message=lambda _message: None,
        on_error=on_error,
        process_context=context,
    )

    gateway.start()
    context.queue.put(GatewayFailure("RuntimeError"))

    assert delivered.wait(1)
    assert errors == ["RuntimeError"]
    gateway.stop()


def test_gateway_recycles_child_after_prolonged_reconnect() -> None:
    context = FakeContext()
    errors: list[str] = []
    recovered = threading.Event()
    gateway = FeishuGateway(
        "cli-test",
        "secret",
        on_message=lambda _message: None,
        on_error=errors.append,
        on_recovered=recovered.set,
        process_context=context,
        reconnect_timeout_seconds=0.02,
        restart_backoff_seconds=0,
        max_restart_backoff_seconds=0,
    )

    gateway.start()
    first_process = context.process
    context.queue.put(GatewayReconnecting())

    deadline = time.monotonic() + 1
    while len(context.processes) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert errors == ["ReconnectTimeout"]
    assert first_process.terminated is True
    assert first_process.joined is True
    assert len(context.processes) == 2
    assert context.process.started is True

    context.queue.put(GatewayConnected())
    assert recovered.wait(1)
    gateway.stop()


def test_gateway_restarts_failed_child_with_backoff() -> None:
    context = FakeContext()
    errors: list[str] = []
    gateway = FeishuGateway(
        "cli-test",
        "secret",
        on_message=lambda _message: None,
        on_error=errors.append,
        process_context=context,
        restart_backoff_seconds=0.02,
        max_restart_backoff_seconds=0.02,
    )

    gateway.start()
    first_process = context.process
    started_at = time.monotonic()
    context.queue.put(GatewayFailure("RuntimeError"))

    deadline = time.monotonic() + 1
    while len(context.processes) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert time.monotonic() - started_at >= 0.02
    assert errors == ["RuntimeError"]
    assert first_process.terminated is True
    assert len(context.processes) == 2
    gateway.stop()


def test_gateway_stop_during_restart_backoff_prevents_new_child() -> None:
    context = FakeContext()
    failed = threading.Event()
    gateway = FeishuGateway(
        "cli-test",
        "secret",
        on_message=lambda _message: None,
        on_error=lambda _error: failed.set(),
        process_context=context,
        restart_backoff_seconds=0.5,
        max_restart_backoff_seconds=0.5,
    )

    gateway.start()
    context.queue.put(GatewayFailure("RuntimeError"))
    assert failed.wait(1)

    gateway.stop()

    assert len(context.processes) == 1
