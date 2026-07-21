import json
from collections import deque
from typing import Any

import pytest

from coderus.integrations.feishu import (
    FeishuClient,
    FeishuConfig,
    FeishuRequestError,
    SendResult,
    TaskCompletedMessage,
)

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttpClient:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        params: dict[str, str] | None = None,
    ) -> FakeResponse:
        self.calls.append(
            {"method": "POST", "url": url, "headers": headers, "json": json, "params": params}
        )
        return self.responses.popleft()


def make_config(**overrides: str) -> FeishuConfig:
    values = {
        "app_id": "cli_test",
        "app_secret": "app-secret-value",
    }
    values.update(overrides)
    return FeishuConfig.model_validate(values)


def make_message() -> TaskCompletedMessage:
    return TaskCompletedMessage(
        task_id="task-17",
        repository="acme/widgets",
        issue="#42 Repair the widget",
        creator="Alice",
        pr_url="https://github.com/acme/widgets/pull/17",
    )


def test_config_repr_does_not_expose_secret() -> None:
    secret = "never-show-this-app-secret"
    config = make_config(app_secret=secret)

    assert secret not in repr(config)


def test_validate_credentials_fetches_tenant_access_token() -> None:
    client = FakeHttpClient(
        FakeResponse(200, {"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
    )
    feishu = FeishuClient(make_config(), http_client=client)

    feishu.validate_credentials()

    assert client.calls == [
        {
            "method": "POST",
            "url": TOKEN_URL,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "json": {"app_id": "cli_test", "app_secret": "app-secret-value"},
            "params": None,
        }
    ]


def test_send_text_routes_message_to_requested_recipient() -> None:
    client = FakeHttpClient(
        FakeResponse(200, {"code": 0, "tenant_access_token": "tenant-token"}),
        FakeResponse(200, {"code": 0, "data": {"message_id": "om_test"}}),
    )
    feishu = FeishuClient(make_config(), http_client=client)

    result = feishu.send_text("oc_default", "chat_id", "Coderus 飞书机器人测试消息")

    assert result == SendResult(message_id="om_test")
    send_call = client.calls[1]
    assert send_call["url"] == MESSAGE_URL
    assert send_call["params"] == {"receive_id_type": "chat_id"}
    assert send_call["headers"] == {
        "Authorization": "Bearer tenant-token",
        "Content-Type": "application/json; charset=utf-8",
    }
    assert send_call["json"] == {
        "receive_id": "oc_default",
        "msg_type": "text",
        "content": json.dumps(
            {"text": "Coderus 飞书机器人测试消息"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def test_send_fetches_token_and_sends_task_completed_card() -> None:
    client = FakeHttpClient(
        FakeResponse(200, {"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}),
        FakeResponse(200, {"code": 0, "data": {"message_id": "om_123"}}),
    )
    feishu = FeishuClient(make_config(), http_client=client)

    result = feishu.send_task_completed(
        make_message(),
        receive_id="oc_group",
        receive_id_type="chat_id",
    )

    assert result == SendResult(message_id="om_123")
    assert client.calls[0] == {
        "method": "POST",
        "url": TOKEN_URL,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "json": {"app_id": "cli_test", "app_secret": "app-secret-value"},
        "params": None,
    }
    send_call = client.calls[1]
    assert send_call["url"] == MESSAGE_URL
    assert send_call["params"] == {"receive_id_type": "chat_id"}
    assert send_call["headers"] == {
        "Authorization": "Bearer tenant-token",
        "Content-Type": "application/json; charset=utf-8",
    }
    assert send_call["json"]["receive_id"] == "oc_group"
    assert send_call["json"]["msg_type"] == "interactive"
    card = json.loads(send_call["json"]["content"])
    assert card["header"]["title"]["content"] == "Coderus task completed"
    markdown = card["elements"][0]["content"]
    assert "task-17" in markdown
    assert "acme/widgets" in markdown
    assert "#42 Repair the widget" in markdown
    assert "Alice" in markdown
    assert "https://github.com/acme/widgets/pull/17" in markdown


def test_task_completed_message_can_override_configured_recipient() -> None:
    client = FakeHttpClient(
        FakeResponse(200, {"code": 0, "tenant_access_token": "tenant-token"}),
        FakeResponse(200, {"code": 0, "data": {"message_id": "om_dynamic"}}),
    )
    feishu = FeishuClient(make_config(), http_client=client)

    result = feishu.send_task_completed(
        make_message(),
        receive_id="ou_dynamic",
        receive_id_type="open_id",
    )

    assert result == SendResult(message_id="om_dynamic")
    send_call = client.calls[1]
    assert send_call["params"] == {"receive_id_type": "open_id"}
    assert send_call["json"]["receive_id"] == "ou_dynamic"


def test_send_supports_user_target_and_text_message() -> None:
    client = FakeHttpClient(
        FakeResponse(200, {"code": 0, "tenant_access_token": "tenant-token"}),
        FakeResponse(200, {"code": 0, "data": {"message_id": "om_text"}}),
    )
    feishu = FeishuClient(
        make_config(),
        http_client=client,
    )

    result = feishu.send_task_completed(
        make_message(),
        receive_id="ou_user",
        receive_id_type="open_id",
        message_type="text",
    )

    assert result.message_id == "om_text"
    send_call = client.calls[1]
    assert send_call["params"] == {"receive_id_type": "open_id"}
    assert send_call["json"]["receive_id"] == "ou_user"
    assert send_call["json"]["msg_type"] == "text"
    text = json.loads(send_call["json"]["content"])["text"]
    for value in (
        "task-17",
        "acme/widgets",
        "#42 Repair the widget",
        "Alice",
        "https://github.com/acme/widgets/pull/17",
    ):
        assert value in text


def test_invalid_message_type_is_rejected_before_http() -> None:
    client = FakeHttpClient()
    feishu = FeishuClient(make_config(), http_client=client)

    with pytest.raises(ValueError, match="message_type"):
        feishu.send_task_completed(
            make_message(),
            receive_id="oc_group",
            receive_id_type="chat_id",
            message_type="post",  # type: ignore[arg-type]
        )

    assert client.calls == []


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(400, False), (429, True), (503, True)],
)
def test_token_http_failure_reports_retryability(status_code: int, retryable: bool) -> None:
    client = FakeHttpClient(FakeResponse(status_code, {"msg": "failed"}))
    feishu = FeishuClient(make_config(), http_client=client)

    with pytest.raises(FeishuRequestError) as error:
        feishu.send_task_completed(
            make_message(), receive_id="oc_group", receive_id_type="chat_id"
        )

    assert error.value.operation == "tenant_access_token"
    assert error.value.status_code == status_code
    assert error.value.api_code is None
    assert error.value.retryable is retryable
    assert str(error.value) == (
        f"feishu tenant_access_token failed with HTTP {status_code}; "
        f"retryable={str(retryable).lower()}"
    )


@pytest.mark.parametrize(
    ("status_code", "api_code", "retryable"),
    [
        (400, 230034, False),
        (400, 230049, True),
        (400, 99991400, True),
        (429, 1000004, True),
    ],
)
def test_message_api_failure_reports_retryability(
    status_code: int, api_code: int, retryable: bool
) -> None:
    secret = "app-secret-value"
    token = "tenant-token-value"
    client = FakeHttpClient(
        FakeResponse(200, {"code": 0, "tenant_access_token": token}),
        FakeResponse(status_code, {"code": api_code, "msg": f"failed {secret} {token}"}),
    )
    feishu = FeishuClient(make_config(app_secret=secret), http_client=client)

    with pytest.raises(FeishuRequestError) as error:
        feishu.send_task_completed(
            make_message(), receive_id="oc_group", receive_id_type="chat_id"
        )

    assert error.value.operation == "send_message"
    assert error.value.status_code == status_code
    assert error.value.api_code == api_code
    assert error.value.retryable is retryable
    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    assert token not in str(error.value)
    assert token not in repr(error.value)


def test_transport_exception_and_repr_do_not_expose_credentials() -> None:
    secret = "transport-app-secret"
    token = "transport-tenant-token"

    class ExplodingHttpClient:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *args: object, **kwargs: object) -> FakeResponse:
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(200, {"code": 0, "tenant_access_token": token})
            raise OSError(f"request contained {secret} and {token}")

    config = make_config(app_secret=secret)
    feishu = FeishuClient(config, http_client=ExplodingHttpClient())

    with pytest.raises(FeishuRequestError) as error:
        feishu.send_task_completed(
            make_message(), receive_id="oc_group", receive_id_type="chat_id"
        )

    exposed = (repr(config), repr(feishu), str(error.value), repr(error.value))
    assert all(secret not in value for value in exposed)
    assert all(token not in value for value in exposed)
    assert error.value.operation == "send_message"
    assert error.value.retryable is True
    assert error.value.__cause__ is None


def test_invalid_success_payload_is_non_retryable_and_sanitized() -> None:
    token = "invalid-json-token"
    client = FakeHttpClient(
        FakeResponse(200, {"code": 0, "tenant_access_token": token}),
        FakeResponse(200, ValueError(f"invalid response containing {token}")),
    )
    feishu = FeishuClient(make_config(), http_client=client)

    with pytest.raises(FeishuRequestError) as error:
        feishu.send_task_completed(
            make_message(), receive_id="oc_group", receive_id_type="chat_id"
        )

    assert error.value.operation == "send_message"
    assert error.value.retryable is False
    assert token not in str(error.value)
    assert token not in repr(error.value)
    assert error.value.__cause__ is None
