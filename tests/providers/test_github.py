from collections import deque
from typing import Any

import pytest

from coderus.providers import (
    GitHubProvider,
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
        if isinstance(self._payload, Exception):
            raise self._payload
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


def github_repository() -> Repository:
    return Repository(
        provider="github",
        owner="octocat",
        name="Hello-World",
        canonical_url="https://github.com/octocat/Hello-World",
    )


def issue_payload(number: int, *, pull_request: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 1000 + number,
        "number": number,
        "title": f"Issue {number}",
        "body": None,
        "state": "open",
        "labels": [{"name": "bug"}, {"name": "help wanted"}],
        "created_at": "2026-07-01T01:02:03Z",
        "updated_at": "2026-07-02T02:03:04Z",
    }
    if pull_request:
        payload["pull_request"] = {"url": "https://api.github.com/pulls/1"}
    return payload


def test_list_open_issues_paginates_and_excludes_pull_requests() -> None:
    first_page = [issue_payload(number) for number in range(1, 100)]
    first_page.append(issue_payload(100, pull_request=True))
    client = FakeClient(
        FakeResponse(
            200,
            first_page,
            headers={
                "Link": (
                    '<https://api.github.com/repositories/1/issues?state=open&per_page=100'
                    '&page=2>; rel="next"'
                )
            },
        ),
        FakeResponse(200, [issue_payload(101)]),
    )

    issues = GitHubProvider(client=client).list_open_issues(github_repository())

    assert [issue.number for issue in issues] == [*range(1, 100), 101]
    assert issues[0].labels == ("bug", "help wanted")
    assert issues[0].canonical_url == "https://github.com/octocat/Hello-World/issues/1"
    assert issues[0].created_at is not None
    assert [call["params"]["page"] for call in client.calls] == [1, 2]
    assert all("after" not in call["params"] for call in client.calls)
    assert all(call["params"]["state"] == "open" for call in client.calls)
    assert all(call["params"]["per_page"] == 100 for call in client.calls)
    assert all("Authorization" not in call["headers"] for call in client.calls)


def test_list_open_issues_forwards_github_after_cursor() -> None:
    cursor = "Y3Vyc29yOnYyOpLPAAABn1E3rJjPAAAAASHQ1Ls="
    client = FakeClient(
        FakeResponse(
            200,
            [issue_payload(1)],
            headers={
                "Link": (
                    "<https://api.github.com/repositories/1128123594/issues"
                    "?state=open&per_page=100&page=2"
                    "&after=Y3Vyc29yOnYyOpLPAAABn1E3rJjPAAAAASHQ1Ls%3D>; rel=\"next\""
                )
            },
        ),
        FakeResponse(200, [issue_payload(2)]),
    )

    issues = GitHubProvider(client=client).list_open_issues(github_repository())

    assert [issue.number for issue in issues] == [1, 2]
    assert client.calls[1]["params"] == {
        "state": "open",
        "per_page": 100,
        "page": 2,
        "after": cursor,
    }


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/repositories/1/issues?state=open&per_page=100&page=2",
        "https://api.github.com/users/octocat/issues?state=open&per_page=100&page=2",
        "https://api.github.com/repositories/1/issues?state=open&per_page=100&page=0",
        "https://api.github.com/repositories/1/issues?state=open&per_page=100&page=2&after=",
        "https://api.github.com/repositories/1/issues?state=closed&per_page=100&page=2",
    ],
)
def test_list_issues_rejects_untrusted_next_links(next_url: str) -> None:
    client = FakeClient(
        FakeResponse(
            200,
            [issue_payload(1)],
            headers={"Link": f'<{next_url}>; rel="next"'},
        )
    )

    with pytest.raises(ProviderRemoteError, match="pagination"):
        GitHubProvider(client=client).list_open_issues(github_repository())

    assert len(client.calls) == 1


def test_list_issues_rejects_repeated_page() -> None:
    client = FakeClient(
        FakeResponse(
            200,
            [issue_payload(1)],
            headers={
                "Link": (
                    '<https://api.github.com/repositories/1/issues?state=open'
                    '&per_page=100&page=1>; rel="next"'
                )
            },
        )
    )

    with pytest.raises(ProviderRemoteError, match="pagination"):
        GitHubProvider(client=client).list_open_issues(github_repository())


def test_list_issues_caps_page_count(monkeypatch) -> None:
    monkeypatch.setattr(GitHubProvider, "_MAX_ISSUE_PAGES", 2)
    first_link = (
        '<https://api.github.com/repositories/1/issues?state=open&per_page=100&page=2>'
        '; rel="next"'
    )
    second_link = (
        '<https://api.github.com/repositories/1/issues?state=open&per_page=100&page=3>'
        '; rel="next"'
    )
    client = FakeClient(
        FakeResponse(200, [issue_payload(1)], headers={"Link": first_link}),
        FakeResponse(200, [issue_payload(2)], headers={"Link": second_link}),
    )

    with pytest.raises(ProviderRemoteError, match="pagination"):
        GitHubProvider(client=client).list_open_issues(github_repository())

    assert len(client.calls) == 2


def test_list_all_issues_requests_all_states() -> None:
    client = FakeClient(FakeResponse(200, [issue_payload(1)]))

    issues = GitHubProvider(client=client).list_issues(github_repository(), state="all")

    assert [issue.number for issue in issues] == [1]
    assert client.calls[0]["params"]["state"] == "all"


def test_get_issue_uses_optional_token_and_maps_response() -> None:
    client = FakeClient(FakeResponse(200, issue_payload(42)))

    issue = GitHubProvider(client=client, token="secret-token").get_issue(github_repository(), 42)

    assert issue.external_id == "1042"
    assert issue.number == 42
    assert client.calls == [
        {
            "url": "https://api.github.com/repos/octocat/Hello-World/issues/42",
            "headers": {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer secret-token",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            "params": None,
        }
    ]


def test_get_repository_maps_public_metadata() -> None:
    client = FakeClient(
        FakeResponse(
            200,
            {
                "private": False,
                "default_branch": "main",
                "has_issues": True,
            },
        )
    )

    repository = GitHubProvider(client=client).get_repository(
        "https://github.com/octocat/Hello-World"
    )

    assert repository.default_branch == "main"
    assert repository.is_private is False
    assert repository.issues_enabled is True


def test_get_repository_rejects_private_repository() -> None:
    client = FakeClient(
        FakeResponse(
            200,
            {"private": True, "default_branch": "main", "has_issues": True},
        )
    )

    with pytest.raises(ProviderRemoteError, match="not public"):
        GitHubProvider(client=client, token="can-see-private").get_repository(
            "https://github.com/octocat/private-repo"
        )


def test_remote_status_exposes_retry_metadata_without_response_body() -> None:
    client = FakeClient(
        FakeResponse(403, {"message": "token leaked?"}, headers={"Retry-After": "60"})
    )

    with pytest.raises(ProviderRemoteError) as error:
        GitHubProvider(client=client).get_issue(github_repository(), 1)

    assert error.value.provider == "github"
    assert error.value.status_code == 403
    assert error.value.retry_after == "60"
    assert "token leaked" not in str(error.value)


def test_network_and_invalid_payload_errors_are_wrapped() -> None:
    network_client = FakeClient(OSError("connection failed"))
    with pytest.raises(ProviderRemoteError, match="request failed"):
        GitHubProvider(client=network_client).get_issue(github_repository(), 1)

    invalid_client = FakeClient(FakeResponse(200, {"number": 1}))
    with pytest.raises(ProviderRemoteError, match="invalid response"):
        GitHubProvider(client=invalid_client).get_issue(github_repository(), 1)


def test_repository_object_is_revalidated_before_building_api_url() -> None:
    client = FakeClient()
    malicious = Repository(
        provider="github",
        owner="octocat/../../users",
        name="repo",
        canonical_url="https://github.com/octocat/../../users/repo",
    )

    with pytest.raises(ValueError):
        GitHubProvider(client=client).list_open_issues(malicious)

    assert client.calls == []
