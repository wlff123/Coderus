from collections.abc import Mapping
from typing import Any, Protocol

from .errors import ProviderRemoteError


class HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...


class HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, object] | None = None,
    ) -> HttpResponse: ...


def default_http_client() -> HttpClient:
    import httpx

    return httpx.Client(timeout=10.0, follow_redirects=False)


def get_json(
    client: HttpClient,
    provider: str,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, object] | None = None,
) -> Any:
    payload, _ = get_json_response(
        client,
        provider,
        url,
        headers=headers,
        params=params,
    )
    return payload


def get_json_response(
    client: HttpClient,
    provider: str,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, object] | None = None,
) -> tuple[Any, Mapping[str, str]]:
    try:
        response = client.get(url, headers=headers, params=params)
    except Exception as exc:
        raise ProviderRemoteError(provider, f"{provider} request failed") from exc

    if not 200 <= response.status_code < 300:
        retry_after = next(
            (value for key, value in response.headers.items() if key.lower() == "retry-after"),
            None,
        )
        raise ProviderRemoteError(
            provider,
            f"{provider} request failed with status {response.status_code}",
            status_code=response.status_code,
            retry_after=retry_after,
        )

    try:
        return response.json(), response.headers
    except Exception as exc:
        raise ProviderRemoteError(provider, f"{provider} returned an invalid response") from exc
