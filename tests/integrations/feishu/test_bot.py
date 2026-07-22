import time

from coderus.integrations.feishu.bot import FeishuBot
from coderus.integrations.feishu.commands import IncomingFeishuMessage


def message() -> IncomingFeishuMessage:
    return IncomingFeishuMessage(
        message_id="om-1",
        event_id="evt-1",
        chat_id="oc-chat",
        chat_type="group",
        sender_open_id="ou-user",
        text="状态",
        mentioned_bot=True,
    )


class FakeService:
    def __init__(self, response: str | None = "reply") -> None:
        self.response = response
        self.messages = []
        self.marked = []
        self.pending = []

    def handle(self, incoming):
        self.messages.append(incoming)
        return self.response

    def pending_replies(self):
        return self.pending

    def mark_reply_result(self, message_id, error=None):
        self.marked.append((message_id, error))
        if error is None:
            self.pending = [item for item in self.pending if item[0] != message_id]


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def send_text(self, receive_id, receive_id_type, text):
        self.calls.append((receive_id, receive_id_type, text))


class FakeGateway:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_bot_processes_message_and_replies_to_same_chat() -> None:
    service = FakeService("任务 RE-5 已排队")
    client = FakeClient()
    gateway_holder = {}

    def gateway_factory(callback):
        gateway_holder["gateway"] = FakeGateway(callback)
        return gateway_holder["gateway"]

    bot = FeishuBot(service=service, client=client, gateway_factory=gateway_factory)

    bot.start()
    gateway_holder["gateway"].callback(message())
    bot.stop()

    assert service.messages == [message()]
    assert service.marked == [("om-1", None)]
    assert client.calls == [("oc-chat", "chat_id", "任务 RE-5 已排队")]
    assert gateway_holder["gateway"].started is True
    assert gateway_holder["gateway"].stopped is True


def test_bot_does_not_reply_when_service_ignores_message() -> None:
    service = FakeService(None)
    client = FakeClient()
    gateway_holder = {}

    def gateway_factory(callback):
        gateway_holder["gateway"] = FakeGateway(callback)
        return gateway_holder["gateway"]

    bot = FeishuBot(service=service, client=client, gateway_factory=gateway_factory)
    bot.start()
    gateway_holder["gateway"].callback(message())

    assert client.calls == []
    bot.stop()


def test_bot_isolates_command_errors() -> None:
    class FailingService:
        def handle(self, _incoming):
            raise RuntimeError("secret detail")

    client = FakeClient()
    gateway_holder = {}

    def gateway_factory(callback):
        gateway_holder["gateway"] = FakeGateway(callback)
        return gateway_holder["gateway"]

    bot = FeishuBot(service=FailingService(), client=client, gateway_factory=gateway_factory)
    bot.start()
    gateway_holder["gateway"].callback(message())

    assert client.calls == [
        ("oc-chat", "chat_id", "命令处理失败，请稍后重试或在网页查看任务状态。")
    ]
    assert bot.last_error == "RuntimeError"
    bot.stop()


def test_bot_retries_persisted_replies_on_start() -> None:
    service = FakeService(None)
    service.pending = [("old-message", "oc-old", "persisted reply")]
    client = FakeClient()
    gateway_holder = {}

    def gateway_factory(callback):
        gateway_holder["gateway"] = FakeGateway(callback)
        return gateway_holder["gateway"]

    bot = FeishuBot(service=service, client=client, gateway_factory=gateway_factory)
    bot.start()

    assert client.calls == [("oc-old", "chat_id", "persisted reply")]
    assert service.marked == [("old-message", None)]
    bot.stop()


def test_bot_retries_failed_reply_while_running() -> None:
    service = FakeService(None)
    service.pending = [("old-message", "oc-old", "persisted reply")]

    class FailOnceClient(FakeClient):
        def send_text(self, receive_id, receive_id_type, text):
            if not self.calls:
                self.calls.append((receive_id, receive_id_type, "failed"))
                raise RuntimeError("temporary")
            super().send_text(receive_id, receive_id_type, text)

    client = FailOnceClient()
    bot = FeishuBot(
        service=service,
        client=client,
        gateway_factory=FakeGateway,
        retry_interval_seconds=0.01,
    )

    bot.start()
    deadline = time.monotonic() + 1
    while service.pending and time.monotonic() < deadline:
        time.sleep(0.01)
    bot.stop()

    assert service.pending == []
    assert service.marked == [("old-message", "RuntimeError"), ("old-message", None)]
