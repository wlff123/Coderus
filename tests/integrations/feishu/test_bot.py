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

    def handle(self, incoming):
        self.messages.append(incoming)
        return self.response


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
