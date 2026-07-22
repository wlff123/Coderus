from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .errors import FeishuRequestError
from .models import FeishuConfig, MessageType, ReceiveIdType, SendResult, TaskCompletedMessage

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
RETRYABLE_API_CODES = {230049, 1000004, 1000005, 99991400}
INVALID_TOKEN_API_CODES = {99991663, 99991664}
TOKEN_REFRESH_SKEW_SECONDS = 60


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class HttpClient(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        params: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...


def default_http_client() -> HttpClient:
    import httpx

    return httpx.Client(timeout=10.0, follow_redirects=False)


class FeishuClient:
    def __init__(
        self,
        config: FeishuConfig,
        *,
        http_client: HttpClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._http_client = http_client if http_client is not None else default_http_client()
        self._clock = clock
        self._cached_token: str | None = None
        self._token_refresh_at = 0.0

    def __repr__(self) -> str:
        return "FeishuClient()"

    def validate_credentials(self) -> None:
        self._tenant_access_token()

    def send_text(
        self,
        receive_id: str,
        receive_id_type: ReceiveIdType,
        text: str,
    ) -> SendResult:
        payload, status_code = self._send_message(
            payload={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps(
                    {"text": text},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
            params={"receive_id_type": receive_id_type},
        )
        data = payload.get("data")
        message_id = data.get("message_id") if isinstance(data, Mapping) else None
        if not isinstance(message_id, str) or not message_id:
            raise self._invalid_response("send_message", status_code)
        return SendResult(message_id=message_id)

    def send_task_completed(
        self,
        message: TaskCompletedMessage,
        *,
        receive_id: str,
        receive_id_type: ReceiveIdType,
        message_type: MessageType = "interactive",
    ) -> SendResult:
        if message_type not in {"interactive", "text"}:
            raise ValueError("message_type must be 'interactive' or 'text'")

        content = self._content(message, message_type)
        payload, status_code = self._send_message(
            payload={
                "receive_id": receive_id,
                "msg_type": message_type,
                "content": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
            },
            params={"receive_id_type": receive_id_type},
        )
        data = payload.get("data")
        message_id = data.get("message_id") if isinstance(data, Mapping) else None
        if not isinstance(message_id, str) or not message_id:
            raise self._invalid_response("send_message", status_code)
        return SendResult(message_id=message_id)

    def _tenant_access_token(self) -> str:
        if self._cached_token is not None and self._clock() < self._token_refresh_at:
            return self._cached_token
        response = self._post(
            "tenant_access_token",
            TOKEN_URL,
            headers=JSON_HEADERS,
            payload={
                "app_id": self._config.app_id,
                "app_secret": self._config.app_secret.get_secret_value(),
            },
        )
        payload = self._successful_payload("tenant_access_token", response)
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise self._invalid_response("tenant_access_token", response.status_code)
        expires_in = payload.get("expires_in", payload.get("expire", 7200))
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
        ):
            raise self._invalid_response("tenant_access_token", response.status_code)
        self._cached_token = token
        self._token_refresh_at = self._clock() + max(
            0.0, float(expires_in) - TOKEN_REFRESH_SKEW_SECONDS
        )
        return token

    def _send_message(
        self,
        *,
        payload: Mapping[str, object],
        params: Mapping[str, str],
    ) -> tuple[Mapping[str, object], int]:
        for attempt in range(2):
            token = self._tenant_access_token()
            response = self._post(
                "send_message",
                MESSAGE_URL,
                headers={"Authorization": f"Bearer {token}", **JSON_HEADERS},
                payload=payload,
                params=params,
            )
            try:
                return self._successful_payload("send_message", response), response.status_code
            except FeishuRequestError as exc:
                invalid_token = (
                    exc.status_code == 401 or exc.api_code in INVALID_TOKEN_API_CODES
                )
                if attempt == 0 and invalid_token:
                    self._cached_token = None
                    self._token_refresh_at = 0.0
                    continue
                raise
        raise AssertionError("unreachable")

    def _post(
        self,
        operation: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        params: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        try:
            return self._http_client.post(
                url,
                headers=headers,
                json=payload,
                params=params,
            )
        except Exception:
            raise FeishuRequestError(
                operation,
                kind="transport",
                retryable=True,
            ) from None

    def _successful_payload(self, operation: str, response: HttpResponse) -> Mapping[str, object]:
        try:
            payload = response.json()
        except Exception:
            if not 200 <= response.status_code < 300:
                raise self._http_error(operation, response.status_code) from None
            raise self._invalid_response(operation, response.status_code) from None
        if not isinstance(payload, Mapping):
            if not 200 <= response.status_code < 300:
                raise self._http_error(operation, response.status_code)
            raise self._invalid_response(operation, response.status_code)
        api_code = payload.get("code")
        if isinstance(api_code, int) and not isinstance(api_code, bool) and api_code != 0:
            raise FeishuRequestError(
                operation,
                kind="api",
                status_code=response.status_code,
                api_code=api_code,
                retryable=(
                    self._retryable_http_status(response.status_code)
                    or api_code in RETRYABLE_API_CODES
                ),
            )
        if not 200 <= response.status_code < 300:
            raise self._http_error(operation, response.status_code)
        if isinstance(api_code, bool) or not isinstance(api_code, int):
            raise self._invalid_response(operation, response.status_code)
        return payload

    @staticmethod
    def _retryable_http_status(status_code: int) -> bool:
        return status_code in {408, 425, 429} or status_code >= 500

    @staticmethod
    def _invalid_response(operation: str, status_code: int) -> FeishuRequestError:
        return FeishuRequestError(
            operation,
            kind="invalid_response",
            status_code=status_code,
            retryable=False,
        )

    @classmethod
    def _http_error(cls, operation: str, status_code: int) -> FeishuRequestError:
        return FeishuRequestError(
            operation,
            kind="http",
            status_code=status_code,
            retryable=cls._retryable_http_status(status_code),
        )

    @staticmethod
    def _content(
        message: TaskCompletedMessage,
        message_type: MessageType,
    ) -> dict[str, object]:
        lines = (
            f"Task: {message.task_id}",
            f"Repository: {message.repository}",
            f"Issue: {message.issue}",
            f"Creator: {message.creator}",
            f"PR: {message.pr_url}",
        )
        if message_type == "text":
            return {"text": "Coderus task completed\n" + "\n".join(lines)}
        markdown = "\n".join(
            f"**{line.split(':', 1)[0]}:**{line.split(':', 1)[1]}" for line in lines
        )
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "green",
                "title": {"tag": "plain_text", "content": "Coderus task completed"},
            },
            "elements": [{"tag": "markdown", "content": markdown}],
        }
