from typing import Literal

FailureKind = Literal["http", "api", "transport", "invalid_response"]


class FeishuRequestError(RuntimeError):
    def __init__(
        self,
        operation: str,
        *,
        kind: FailureKind,
        retryable: bool,
        status_code: int | None = None,
        api_code: int | None = None,
    ) -> None:
        self.operation = operation
        self.retryable = retryable
        self.status_code = status_code
        self.api_code = api_code
        retry_text = str(retryable).lower()
        if kind == "http":
            message = f"feishu {operation} failed with HTTP {status_code}; retryable={retry_text}"
        elif kind == "api":
            message = (
                f"feishu {operation} failed with API code {api_code} "
                f"(HTTP {status_code}); retryable={retry_text}"
            )
        elif kind == "transport":
            message = f"feishu {operation} request failed; retryable={retry_text}"
        else:
            message = f"feishu {operation} returned an invalid response; retryable={retry_text}"
        super().__init__(message)
