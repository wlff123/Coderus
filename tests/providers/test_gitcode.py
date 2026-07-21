from collections import deque
from typing import Any

import pytest

from coderus.providers import (
    GitCodeProvider,
    ProviderRemoteError,
    Repository,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


class FakeClient:
    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, object] | None = None,
    ) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "params": params})
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def gitcode_repository() -> Repository:
    return Repository(
        provider="gitcode",
        owner="example",
        name="project",
        canonical_url="https://gitcode.com/example/project",
    )


def issue_payload(number: int) -> dict[str, Any]:
    return {
        "id": 5000 + number,
        "number": str(number),
        "title": f"Issue {number}",
        "body": "Details",
        "state": "opened",
        "labels": [{"name": "bug"}],
        "created_at": "2026-07-01T01:02:03.000+08:00",
        "updated_at": "2026-07-02T02:03:04.000+08:00",
    }


def test_public_repository_and_issues_work_without_token() -> None:
    client = FakeClient(
        FakeResponse(
            200,
            {"public": True, "private": False, "default_branch": "main", "has_issue": True},
        ),
        FakeResponse(200, [issue_payload(1)]),
        FakeResponse(200, issue_payload(1)),
    )
    provider = GitCodeProvider(client=client)

    repository = provider.get_repository("https://gitcode.com/example/project")
    assert provider.list_open_issues(repository)[0].number == 1
    assert provider.get_issue(repository, 1).number == 1
    assert all("Authorization" not in call["headers"] for call in client.calls)
    assert all("access_token" not in (call["params"] or {}) for call in client.calls)


def test_get_repository_maps_public_metadata() -> None:
    client = FakeClient(
        FakeResponse(
            200,
            {"public": True, "private": False, "default_branch": "main", "has_issue": True},
        )
    )

    repository = GitCodeProvider(client=client, token="token").get_repository(
        "https://gitcode.com/example/project"
    )

    assert repository.default_branch == "main"
    assert repository.is_private is False
    assert repository.issues_enabled is True
    assert client.calls[0]["headers"]["Authorization"] == "Bearer token"
    assert client.calls[0]["params"] is None


def test_get_repository_accepts_official_payload_without_issue_capability() -> None:
    client = FakeClient(
        FakeResponse(
            200,
            {
                "public": True,
                "private": False,
                "default_branch": "master",
                "open_issues_count": 54,
            },
        )
    )

    repository = GitCodeProvider(client=client, token="token").get_repository(
        "https://gitcode.com/opengauss/oGMemory"
    )

    assert repository.default_branch == "master"
    assert repository.issues_enabled is None


def test_get_repository_rejects_private_repository() -> None:
    client = FakeClient(
        FakeResponse(
            200,
            {"public": False, "private": True, "default_branch": "main", "has_issue": True},
        )
    )
    with pytest.raises(ProviderRemoteError, match="not public"):
        GitCodeProvider(client=client, token="token").get_repository(
            "https://gitcode.com/example/project"
        )


def test_list_open_issues_uses_documented_endpoint_and_paginates() -> None:
    first_page = [issue_payload(number) for number in range(1, 101)]
    client = FakeClient(
        FakeResponse(200, first_page),
        FakeResponse(200, [issue_payload(101)]),
    )

    issues = GitCodeProvider(client=client, token="gitcode-token").list_open_issues(
        gitcode_repository()
    )

    assert len(issues) == 101
    assert issues[0].state == "open"
    assert issues[0].labels == ("bug",)
    assert issues[0].canonical_url == "https://gitcode.com/example/project/issues/1"
    assert [call["params"]["page"] for call in client.calls] == [1, 2]
    assert all(call["params"]["state"] == "open" for call in client.calls)
    assert all(call["params"]["per_page"] == 100 for call in client.calls)
    assert all(call["headers"]["Authorization"] == "Bearer gitcode-token" for call in client.calls)
    assert all("access_token" not in call["params"] for call in client.calls)


def test_get_issue_maps_documented_response() -> None:
    client = FakeClient(FakeResponse(200, issue_payload(7)))

    issue = GitCodeProvider(client=client, token="token").get_issue(gitcode_repository(), 7)

    assert issue.external_id == "5007"
    assert issue.number == 7
    assert client.calls[0]["url"] == (
        "https://api.gitcode.com/api/v5/repos/example/project/issues/7"
    )


def test_remote_error_is_clear_and_preserves_retry_metadata() -> None:
    client = FakeClient(FakeResponse(429, {"message": "limited"}, headers={"Retry-After": "30"}))

    with pytest.raises(ProviderRemoteError) as error:
        GitCodeProvider(client=client, token="token").get_issue(gitcode_repository(), 1)

    assert error.value.provider == "gitcode"
    assert error.value.status_code == 429
    assert error.value.retry_after == "30"
    assert "limited" not in str(error.value)


def test_invalid_remote_payload_is_wrapped() -> None:
    client = FakeClient(FakeResponse(200, {"number": "not-a-number"}))

    with pytest.raises(ProviderRemoteError, match="invalid response"):
        GitCodeProvider(client=client, token="token").get_issue(gitcode_repository(), 1)
