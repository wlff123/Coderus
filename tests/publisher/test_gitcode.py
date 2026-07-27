from collections import deque
from pathlib import Path
from typing import Any

import pytest

from coderus.publisher import (
    ForkNotReady,
    ForkResult,
    GitCodePublisher,
    InvalidPublisherInput,
    PRCommentResult,
    PRFeedbackItem,
    PublisherRemoteError,
    PublishResult,
    PullRequestDetails,
    RegisteredForkMismatch,
    UnsupportedPublisher,
)

_MISSING = object()


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttpClient:
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
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return self._next()

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
    ) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self._next()

    def patch(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
    ) -> FakeResponse:
        self.calls.append({"method": "PATCH", "url": url, "headers": headers, "json": json})
        return self._next()

    def _next(self) -> FakeResponse:
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


class RecordingPusher:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str]] = []

    def push(self, workspace: Path, remote_url: str, branch: str) -> None:
        self.calls.append((workspace, remote_url, branch))


def account(login: object = "coderus-bot") -> FakeResponse:
    return FakeResponse(200, {"login": login})


def fork_payload(
    *,
    owner: str = "coderus-bot",
    parent: str = "acme/widgets",
    full_name: str = "coderus-bot/widgets",
    fork: bool = True,
    public: bool = True,
) -> dict[str, Any]:
    return {
        "fork": fork,
        "full_name": full_name,
        "owner": {"login": owner},
        "parent": {"full_name": parent},
        "private": not public,
        "public": public,
    }


def pr_payload(
    *,
    number: int = 17,
    state: str = "open",
    head_owner: str = "coderus-bot",
    head_ref: str = "coderus/issue-7-11",
    base_ref: str = "main",
    merged: object = False,
    merged_at: object = _MISSING,
) -> dict[str, Any]:
    payload = {
        "number": number,
        "html_url": f"https://gitcode.com/acme/widgets/pulls/{number}",
        "state": state,
        "head": {
            "ref": head_ref,
            "sha": "b" * 40,
            "repo": {
                "path": "widgets",
                "name": "widgets",
                "namespace": {"path": head_owner},
            },
        },
        "base": {
            "ref": base_ref,
            "sha": "a" * 40,
            "repo": {
                "path": "widgets",
                "name": "widgets",
                "namespace": {"path": "acme"},
            },
        },
    }
    if merged is not _MISSING:
        payload["merged"] = merged
    if merged_at is not _MISSING:
        payload["merged_at"] = merged_at
    return payload


def mutate_pr_payload(path: tuple[str, ...], value: object) -> dict[str, Any]:
    payload = pr_payload()
    target: dict[str, Any] = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return payload


def publisher(
    client: FakeHttpClient,
    *,
    registered: bool = False,
    pusher: RecordingPusher | None = None,
    **kwargs: Any,
) -> GitCodePublisher:
    forks = (
        {("acme", "widgets"): "https://gitcode.com/coderus-bot/widgets.git"}
        if registered
        else {}
    )
    return GitCodePublisher(
        "gitcode-secret",
        "coderus-bot",
        registered_forks=forks,
        http_client=client,
        git_pusher=pusher or RecordingPusher(),
        **kwargs,
    )


def assert_bearer_only(client: FakeHttpClient) -> None:
    assert client.calls
    for call in client.calls:
        assert call["headers"]["Authorization"] == "Bearer gitcode-secret"
        assert "gitcode-secret" not in call["url"]
        assert "access_token" not in call["url"]
        assert "?" not in call["url"]


def test_ensure_fork_returns_existing_expected_public_fork_and_normalizes_url() -> None:
    client = FakeHttpClient(account(), FakeResponse(200, fork_payload()))

    result = publisher(client).ensure_fork("acme", "widgets")

    assert result == ForkResult(
        url="https://gitcode.com/coderus-bot/widgets.git",
        owner="coderus-bot",
        created=False,
    )
    assert [call["url"] for call in client.calls] == [
        "https://api.gitcode.com/api/v5/user",
        "https://api.gitcode.com/api/v5/repos/coderus-bot/widgets",
    ]
    assert_bearer_only(client)


def test_ensure_fork_creates_and_polls_with_finite_attempts() -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(404, {"message": "missing"}),
        FakeResponse(202, {"id": 1}),
        FakeResponse(404, {"message": "building"}),
        FakeResponse(200, fork_payload()),
    )
    sleeps: list[float] = []

    result = publisher(
        client,
        sleep=sleeps.append,
        fork_poll_attempts=3,
        fork_poll_interval=0.25,
    ).ensure_fork("acme", "widgets")

    assert result.created is True
    assert [call["method"] for call in client.calls] == ["GET", "GET", "POST", "GET", "GET"]
    assert client.calls[2]["url"].endswith("/repos/acme/widgets/forks")
    assert client.calls[2]["json"] == {"name": "widgets", "path": "widgets"}
    assert sleeps == [0.25, 0.25]
    assert_bearer_only(client)


@pytest.mark.parametrize(
    "payload",
    [
        fork_payload(fork=False),
        fork_payload(parent="other/widgets"),
        fork_payload(owner="other"),
        fork_payload(full_name="coderus-bot/other"),
        fork_payload(public=False),
    ],
)
def test_ensure_fork_rejects_same_name_repository_that_is_not_expected_public_fork(
    payload: dict[str, Any],
) -> None:
    client = FakeHttpClient(account(), FakeResponse(200, payload))

    with pytest.raises(RegisteredForkMismatch):
        publisher(client).ensure_fork("acme", "widgets")


def test_ensure_fork_rejects_invalid_account_response() -> None:
    client = FakeHttpClient(account("bad/name"))

    with pytest.raises(PublisherRemoteError, match="invalid response"):
        publisher(client).ensure_fork("acme", "widgets")


def test_ensure_fork_times_out_after_configured_poll_attempts() -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(404, {}),
        FakeResponse(202, {}),
        FakeResponse(404, {}),
        FakeResponse(404, {}),
    )

    with pytest.raises(ForkNotReady, match="did not become available"):
        publisher(
            client,
            sleep=lambda _: None,
            fork_poll_attempts=2,
        ).ensure_fork("acme", "widgets")

    assert len(client.calls) == 5


@pytest.mark.parametrize(
    "url",
    [
        "http://gitcode.com/coderus-bot/widgets.git",
        "https://token@gitcode.com/coderus-bot/widgets.git",
        "https://gitcode.com/coderus-bot/widgets.git?access_token=secret",
        "https://github.com/coderus-bot/widgets.git",
    ],
)
def test_constructor_rejects_unsafe_or_non_gitcode_registered_url(url: str) -> None:
    error = UnsupportedPublisher if "github.com" in url else InvalidPublisherInput
    with pytest.raises(error):
        GitCodePublisher(
            "secret",
            "coderus-bot",
            registered_forks={("acme", "widgets"): url},
            http_client=FakeHttpClient(),
            git_pusher=RecordingPusher(),
        )


def test_publish_pushes_only_registered_fork_and_creates_cross_repository_pr(
    tmp_path: Path,
) -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(200, fork_payload()),
        FakeResponse(200, []),
        FakeResponse(201, pr_payload()),
    )
    pusher = RecordingPusher()

    result = publisher(client, registered=True, pusher=pusher).publish(
        tmp_path,
        "acme",
        "widgets",
        "main",
        "coderus/issue-7-11",
        "Repair widgets",
        "Fixes #7",
    )

    assert pusher.calls == [
        (tmp_path, "https://gitcode.com/coderus-bot/widgets.git", "coderus/issue-7-11")
    ]
    assert result == PublishResult(
        url="https://gitcode.com/acme/widgets/pulls/17",
        number=17,
        state="open",
        fork_url="https://gitcode.com/coderus-bot/widgets.git",
        branch="coderus/issue-7-11",
        pr_created=True,
    )
    request = client.calls[-1]
    assert request["method"] == "POST"
    assert request["url"] == "https://api.gitcode.com/api/v5/repos/acme/widgets/pulls"
    assert request["json"] == {
        "title": "Repair widgets",
        "head": "coderus-bot:coderus/issue-7-11",
        "fork_path": "coderus-bot/widgets",
        "base": "main",
        "body": "Fixes #7",
        "draft": False,
    }


def test_publish_accepts_gitcode_create_response_web_url(tmp_path: Path) -> None:
    created = pr_payload(state="opened")
    created["web_url"] = created.pop("html_url").replace("/pulls/", "/merge_requests/")
    client = FakeHttpClient(
        account(),
        FakeResponse(200, fork_payload()),
        FakeResponse(200, []),
        FakeResponse(200, created),
    )

    result = publisher(client, registered=True).publish(
        tmp_path, "acme", "widgets", "main", "coderus/issue-7-11", "Title", "Body"
    )

    assert result.url == "https://gitcode.com/acme/widgets/merge_requests/17"
    assert result.number == 17
    assert result.state == "open"


def test_publish_reuses_only_exact_existing_pr(tmp_path: Path) -> None:
    wrong = pr_payload(number=16, head_ref="other", state="closed")
    exact = pr_payload(number=17, state="closed")
    client = FakeHttpClient(
        account(),
        FakeResponse(200, fork_payload()),
        FakeResponse(200, [wrong, exact]),
    )

    result = publisher(client, registered=True).publish(
        tmp_path, "acme", "widgets", "main", "coderus/issue-7-11", "Title", "Body"
    )

    assert result.number == 17
    assert result.state == "closed"
    assert result.pr_created is False
    assert [call["method"] for call in client.calls].count("POST") == 0
    list_call = client.calls[-1]
    assert list_call["params"] == {
        "state": "all",
        "base": "main",
        "per_page": 100,
        "page": 1,
    }


def test_publish_reuses_pr_with_gitcode_owner_repository_shape(tmp_path: Path) -> None:
    existing = pr_payload()
    for owner, ref in (("coderus-bot", "head"), ("acme", "base")):
        existing[ref]["repo"] = {
            "full_path": f"{owner}/widgets",
            "full_name": f"{owner}/widgets",
            "name": "widgets",
            "path": "widgets",
            "owner": {"login": owner if ref == "head" else "project-owner"},
            "html_url": f"https://gitcode.com/{owner}/widgets.git",
        }
    client = FakeHttpClient(
        account(),
        FakeResponse(200, fork_payload()),
        FakeResponse(200, [existing]),
    )

    result = publisher(client, registered=True).publish(
        tmp_path, "acme", "widgets", "main", "coderus/issue-7-11", "Title", "Body"
    )

    assert result.number == 17
    assert result.pr_created is False
    assert [call["method"] for call in client.calls].count("POST") == 0


def test_publish_paginates_and_reconciles_head_locally(tmp_path: Path) -> None:
    wrong_page = [pr_payload(number=index, head_ref="other") for index in range(1, 101)]
    client = FakeHttpClient(
        account(),
        FakeResponse(200, fork_payload()),
        FakeResponse(200, wrong_page),
        FakeResponse(200, [pr_payload()]),
    )

    result = publisher(client, registered=True).publish(
        tmp_path, "acme", "widgets", "main", "coderus/issue-7-11", "Title", "Body"
    )

    assert result.number == 17
    list_calls = [call for call in client.calls if call["url"].endswith("/pulls")]
    assert [call["params"]["page"] for call in list_calls] == [1, 2]
    assert all("head" not in call["params"] for call in list_calls)


@pytest.mark.parametrize(
    "failure",
    [
        OSError("connection lost after send"),
        FakeResponse(500, {"secret": "response-body"}),
    ],
)
def test_publish_reconciles_after_single_uncertain_create_attempt(
    tmp_path: Path, failure: FakeResponse | Exception
) -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(200, fork_payload()),
        FakeResponse(200, []),
        failure,
        FakeResponse(200, [pr_payload()]),
    )

    result = publisher(client, registered=True).publish(
        tmp_path, "acme", "widgets", "main", "coderus/issue-7-11", "Title", "Body"
    )

    assert result.number == 17
    assert result.pr_created is True
    assert [call["method"] for call in client.calls][-2:] == ["POST", "GET"]
    assert [call["method"] for call in client.calls].count("POST") == 1


@pytest.mark.parametrize(
    "failure",
    [
        OSError("connection lost after fork send"),
        FakeResponse(500, {"secret": "response-body"}),
    ],
)
def test_ensure_fork_polls_after_single_uncertain_create_attempt(
    failure: FakeResponse | Exception,
) -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(404, {}),
        failure,
        FakeResponse(200, fork_payload()),
    )

    result = publisher(client, sleep=lambda _: None).ensure_fork("acme", "widgets")

    assert result.created is True
    assert [call["method"] for call in client.calls] == ["GET", "GET", "POST", "GET"]


def test_ensure_fork_reconciles_409_without_reposting() -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(404, {}),
        FakeResponse(409, {"message": "already exists"}),
        FakeResponse(200, fork_payload()),
    )

    result = publisher(client, sleep=lambda _: None).ensure_fork("acme", "widgets")

    assert result.created is True
    assert [call["method"] for call in client.calls].count("POST") == 1


def test_ensure_fork_409_reconciliation_verifies_parent() -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(404, {}),
        FakeResponse(409, {}),
        FakeResponse(200, fork_payload(parent="other/widgets")),
    )

    with pytest.raises(RegisteredForkMismatch):
        publisher(client, sleep=lambda _: None).ensure_fork("acme", "widgets")

    assert [call["method"] for call in client.calls].count("POST") == 1


def test_publish_reconciles_409_and_verifies_head_and_base(tmp_path: Path) -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(200, fork_payload()),
        FakeResponse(200, []),
        FakeResponse(409, {"message": "already exists"}),
        FakeResponse(200, [pr_payload()]),
    )

    result = publisher(client, registered=True).publish(
        tmp_path, "acme", "widgets", "main", "coderus/issue-7-11", "Title", "Body"
    )

    assert result.number == 17
    assert [call["method"] for call in client.calls].count("POST") == 1


def test_publish_409_reconciliation_rejects_mismatched_pr(tmp_path: Path) -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(200, fork_payload()),
        FakeResponse(200, []),
        FakeResponse(409, {}),
        FakeResponse(200, [pr_payload(head_ref="other")]),
    )

    with pytest.raises(PublisherRemoteError, match="status 409"):
        publisher(client, registered=True).publish(
            tmp_path,
            "acme",
            "widgets",
            "main",
            "coderus/issue-7-11",
            "Title",
            "Body",
        )

    assert [call["method"] for call in client.calls].count("POST") == 1


def test_publish_reconciles_after_success_with_invalid_json(tmp_path: Path) -> None:
    client = FakeHttpClient(
        account(),
        FakeResponse(200, fork_payload()),
        FakeResponse(200, []),
        FakeResponse(201, ValueError("invalid json")),
        FakeResponse(200, [pr_payload()]),
    )

    result = publisher(client, registered=True).publish(
        tmp_path, "acme", "widgets", "main", "coderus/issue-7-11", "Title", "Body"
    )

    assert result.number == 17
    assert [call["method"] for call in client.calls].count("POST") == 1


def test_publish_rejects_unregistered_fork_before_network_or_push(tmp_path: Path) -> None:
    client = FakeHttpClient()
    pusher = RecordingPusher()

    with pytest.raises(RegisteredForkMismatch, match="no fork is registered"):
        publisher(client, pusher=pusher).publish(
            tmp_path, "acme", "widgets", "main", "coderus/task-1", "Title", "Body"
        )

    assert client.calls == []
    assert pusher.calls == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"state": "open"}, "open"),
        ({"state": "opened"}, "open"),
        ({"state": "closed"}, "closed"),
        ({"state": "closed", "merged": True}, "merged"),
        ({"state": "closed", "merged_at": "2026-07-17T10:00:00Z"}, "merged"),
        ({"state": "merged"}, "merged"),
    ],
)
def test_get_pr_status_maps_gitcode_states(payload: dict[str, Any], expected: str) -> None:
    assert publisher(FakeHttpClient(FakeResponse(200, payload))).get_pr_status(
        "acme", "widgets", 17
    ) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "open", "merged": True},
        {"state": "open", "merged_at": "2026-07-17T10:00:00Z"},
        {"state": "closed", "merged": False, "merged_at": "2026-07-17T10:00:00Z"},
        {"state": "merged", "merged": False},
        {"state": "merged", "merged_at": None},
    ],
)
def test_get_pr_status_rejects_inconsistent_merge_fields(payload: dict[str, Any]) -> None:
    with pytest.raises(PublisherRemoteError, match="status"):
        publisher(FakeHttpClient(FakeResponse(200, payload))).get_pr_status(
            "acme", "widgets", 17
        )


def test_get_pull_request_validates_and_maps_required_fields() -> None:
    details = publisher(FakeHttpClient(FakeResponse(200, pr_payload()))).get_pull_request(
        "acme", "widgets", 17
    )

    assert details == PullRequestDetails(
        number=17,
        url="https://gitcode.com/acme/widgets/pulls/17",
        state="open",
        merged=False,
        base_sha="a" * 40,
        head_sha="b" * 40,
        base_ref="main",
        head_ref="coderus/issue-7-11",
        head_repository_url="https://gitcode.com/coderus-bot/widgets.git",
    )


def test_get_pull_request_accepts_gitcode_merge_request_url() -> None:
    payload = pr_payload(merged=None, merged_at="")
    payload["html_url"] = "https://gitcode.com/acme/widgets/merge_requests/17"

    details = publisher(FakeHttpClient(FakeResponse(200, payload))).get_pull_request(
        "acme", "widgets", 17
    )

    assert details.url == "https://gitcode.com/acme/widgets/merge_requests/17"


def test_get_pull_request_does_not_depend_on_unofficial_clone_url() -> None:
    payload = pr_payload()
    payload["head"]["repo"]["clone_url"] = "https://token@evil.example/widgets.git"

    details = publisher(FakeHttpClient(FakeResponse(200, payload))).get_pull_request(
        "acme", "widgets", 17
    )

    assert details.head_repository_url == "https://gitcode.com/coderus-bot/widgets.git"


def test_get_pull_request_supports_strict_legacy_repo_coordinates() -> None:
    payload = pr_payload()
    payload["head"]["repo"] = {
        "full_name": "coderus-bot/widgets",
        "html_url": "https://gitcode.com/coderus-bot/widgets",
    }
    payload["base"]["repo"] = {"full_name": "acme/widgets"}

    details = publisher(FakeHttpClient(FakeResponse(200, payload))).get_pull_request(
        "acme", "widgets", 17
    )

    assert details.head_repository_url == "https://gitcode.com/coderus-bot/widgets.git"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(("number",), True, id="number-type"),
        pytest.param(("number",), 18, id="number-mismatch"),
        pytest.param(
            ("html_url",),
            "https://gitcode.com/acme/widgets/pull-requests/17",
            id="pr-url",
        ),
        pytest.param(
            ("html_url",),
            "https://token@gitcode.com/acme/widgets/pulls/17",
            id="pr-url-credentials",
        ),
        pytest.param(
            ("html_url",), "https://github.com/acme/widgets/pulls/17", id="pr-url-host"
        ),
        pytest.param(("state",), "draft", id="state"),
        pytest.param(("merged",), "false", id="merged"),
        pytest.param(("base", "sha"), "a" * 39, id="base-sha"),
        pytest.param(("head", "sha"), "not-a-sha", id="head-sha"),
        pytest.param(("base", "ref"), "--upload-pack=evil", id="base-ref"),
        pytest.param(("head", "ref"), "feature..review", id="head-ref"),
        pytest.param(("head", "repo", "path"), None, id="head-path-missing"),
        pytest.param(
            ("head", "repo", "path"), "../widgets", id="head-path-invalid"
        ),
        pytest.param(
            ("head", "repo", "name"), "other", id="head-name-mismatch"
        ),
        pytest.param(
            ("head", "repo", "namespace"),
            {"path": "../other"},
            id="head-namespace-invalid",
        ),
        pytest.param(
            ("base", "repo", "path"), "other", id="base-path-mismatch"
        ),
        pytest.param(
            ("head", "repo", "html_url"),
            "http://gitcode.com/coderus-bot/widgets",
            id="html-url-http",
        ),
        pytest.param(
            ("head", "repo", "html_url"),
            "https://token@gitcode.com/coderus-bot/widgets",
            id="html-url-credentials",
        ),
        pytest.param(
            ("head", "repo", "html_url"),
            "https://github.com/coderus-bot/widgets",
            id="html-url-host",
        ),
        pytest.param(
            ("head", "repo", "html_url"),
            "https://gitcode.com/coderus-bot/other",
            id="html-url-mismatch",
        ),
        pytest.param(
            ("head", "repo", "html_url"),
            "https://gitcode.com/coderus-bot/widgets?access_token=secret",
            id="html-url-query",
        ),
    ],
)
def test_get_pull_request_rejects_single_invalid_metadata_field(
    path: tuple[str, ...], value: object
) -> None:
    payload = mutate_pr_payload(path, value)

    with pytest.raises(PublisherRemoteError):
        publisher(FakeHttpClient(FakeResponse(200, payload))).get_pull_request(
            "acme", "widgets", 17
        )


def feedback_item(
    identifier: object,
    author: str,
    *,
    path: str | None = None,
    position: int | None = None,
    include_url: bool = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": identifier,
        "body": f"feedback {identifier}",
        "user": {"login": author},
    }
    if include_url:
        item["html_url"] = f"https://gitcode.com/acme/widgets/pulls/7#note_{identifier}"
    if path is not None:
        item["path"] = path
    if position is not None:
        item["position"] = position
    return item


def test_list_pr_feedback_maps_permissions_and_diff_position_with_cache() -> None:
    client = FakeHttpClient(
        FakeResponse(
            200,
            [
                feedback_item(1, "acme"),
                feedback_item(2, "admin-user"),
                feedback_item(3, "admin-user", path="src/a.py", position=12),
                feedback_item(4, "push-user"),
                feedback_item(5, "pull-user"),
                feedback_item(6, "outsider"),
            ],
        ),
        FakeResponse(200, {"permission": "admin"}),
        FakeResponse(200, {"permission": "push"}),
        FakeResponse(200, {"permission": "pull"}),
        FakeResponse(404, {"message": "not a collaborator"}),
    )

    result = publisher(client).list_pr_feedback("acme", "widgets", 7)

    assert [item.author_association for item in result] == [
        "OWNER",
        "MEMBER",
        "MEMBER",
        "COLLABORATOR",
        "CONTRIBUTOR",
        "NONE",
    ]
    assert result[2] == PRFeedbackItem(
        provider_id="comment:3",
        kind="review_comment",
        author="admin-user",
        author_association="MEMBER",
        body="feedback 3",
        url="https://gitcode.com/acme/widgets/merge_requests/7#note_3",
        path="src/a.py",
        line=12,
    )
    permission_urls = [call["url"] for call in client.calls if "/collaborators/" in call["url"]]
    assert permission_urls.count(
        "https://api.gitcode.com/api/v5/repos/acme/widgets/collaborators/admin-user/permission"
    ) == 1


@pytest.mark.parametrize(
    "permission_response",
    [
        FakeResponse(500, {"message": "temporary failure"}),
        FakeResponse(200, {"permission": "unexpected"}),
        OSError("network failure"),
    ],
)
def test_list_pr_feedback_permission_failures_degrade_to_untrusted(
    permission_response: FakeResponse | Exception,
) -> None:
    client = FakeHttpClient(
        FakeResponse(200, [feedback_item(1, "unknown-user")]),
        permission_response,
    )

    result = publisher(client, request_attempts=1).list_pr_feedback(
        "acme", "widgets", 7
    )

    assert result[0].author_association == "NONE"


def test_list_pr_feedback_reads_all_pages() -> None:
    first_page = [feedback_item(number, "acme") for number in range(1, 101)]
    client = FakeHttpClient(
        FakeResponse(200, first_page),
        FakeResponse(200, [feedback_item(101, "acme")]),
    )

    result = publisher(client).list_pr_feedback("acme", "widgets", 7)

    assert len(result) == 101
    assert [call["params"]["page"] for call in client.calls] == [1, 2]


@pytest.mark.parametrize(("identifier", "expected"), [(7, "7"), ("7", "7"), ("hash-7", "hash-7")])
def test_list_pr_feedback_normalizes_string_and_integer_ids(
    identifier: object, expected: str
) -> None:
    client = FakeHttpClient(FakeResponse(200, [feedback_item(identifier, "acme")]))

    result = publisher(client).list_pr_feedback("acme", "widgets", 7)

    assert result[0].provider_id == f"comment:{expected}"


@pytest.mark.parametrize("identifier", ["", True, None, 1.5, []])
def test_list_pr_feedback_rejects_invalid_comment_ids(identifier: object) -> None:
    client = FakeHttpClient(FakeResponse(200, [feedback_item(identifier, "acme")]))

    with pytest.raises(PublisherRemoteError, match="feedback"):
        publisher(client).list_pr_feedback("acme", "widgets", 7)


def test_list_pr_feedback_without_html_url_uses_canonical_pr_url() -> None:
    client = FakeHttpClient(
        FakeResponse(200, [feedback_item("hash-7", "acme", include_url=False)])
    )

    result = publisher(client).list_pr_feedback("acme", "widgets", 7)

    assert result[0].url == "https://gitcode.com/acme/widgets/merge_requests/7"


def test_publish_pr_comment_reuses_marker_only_for_authenticated_author() -> None:
    marker = "<!-- coderus-pr-review:RV-7:abc -->"
    body = f"review complete\n{marker}"
    client = FakeHttpClient(
        FakeResponse(
            200,
            [
                {
                    "id": 8,
                    "body": body,
                    "html_url": "https://gitcode.com/acme/widgets/pulls/7#note_8",
                    "user": {"login": "coderus-bot"},
                }
            ],
        )
    )

    result = publisher(client).publish_pr_comment("acme", "widgets", 7, body, marker)

    assert result == PRCommentResult(
        url="https://gitcode.com/acme/widgets/merge_requests/7#note_8", created=False
    )
    assert [call["method"] for call in client.calls] == ["GET"]


def test_publish_pr_comment_updates_stale_body_with_same_marker() -> None:
    marker = "<!-- coderus-pr-review:RV-7:abc -->"
    old_body = f"old review\n{marker}"
    new_body = f"correct review\n{marker}"
    client = FakeHttpClient(
        FakeResponse(
            200,
            [
                {
                    "id": 8,
                    "body": old_body,
                    "html_url": "https://gitcode.com/acme/widgets/pulls/7#note_8",
                    "user": {"login": "coderus-bot"},
                }
            ],
        ),
        FakeResponse(200, {}),
    )

    result = publisher(client).publish_pr_comment("acme", "widgets", 7, new_body, marker)

    assert result == PRCommentResult(
        url="https://gitcode.com/acme/widgets/merge_requests/7#note_8", created=False
    )
    assert client.calls[-1]["method"] == "PATCH"
    assert client.calls[-1]["url"] == (
        "https://api.gitcode.com/api/v5/repos/acme/widgets/pulls/comments/8"
    )
    assert client.calls[-1]["json"] == {"body": new_body}


def test_publish_pr_comment_reuses_marker_without_html_url_using_canonical_url() -> None:
    marker = "<!-- coderus-pr-review:RV-7:abc -->"
    body = f"review complete\n{marker}"
    client = FakeHttpClient(
        FakeResponse(
            200,
            [{"id": "hash-8", "body": body, "user": {"login": "coderus-bot"}}],
        )
    )

    result = publisher(client).publish_pr_comment("acme", "widgets", 7, body, marker)

    assert result == PRCommentResult(
        url="https://gitcode.com/acme/widgets/merge_requests/7", created=False
    )


def test_publish_pr_comment_ignores_forged_marker_and_creates_new_comment() -> None:
    marker = "<!-- coderus-pr-review:RV-7:abc -->"
    body = f"review complete\n{marker}"
    client = FakeHttpClient(
        FakeResponse(
            200,
            [{"id": 8, "body": body, "user": {"login": "other-user"}}],
        ),
        FakeResponse(
            201,
            {
                "id": 9,
                "body": body,
                "html_url": "https://gitcode.com/acme/widgets/pulls/7#note_9",
            },
        ),
    )

    result = publisher(client).publish_pr_comment("acme", "widgets", 7, body, marker)

    assert result.created is True
    assert client.calls[-1]["json"] == {"body": body}


def test_publish_pr_comment_accepts_diff_comment_fields() -> None:
    marker = "<!-- coderus-pr-review:RV-7:abc -->"
    body = f"review\n{marker}"
    client = FakeHttpClient(
        FakeResponse(200, []),
        FakeResponse(
            201,
            {"id": "hash-9", "body": body},
        ),
    )

    result = publisher(client).publish_pr_comment(
        "acme",
        "widgets",
        7,
        body,
        marker,
        path="src/widget.py",
        position=19,
    )

    assert result.created is True
    assert result.url == "https://gitcode.com/acme/widgets/merge_requests/7"
    assert client.calls[-1]["json"] == {
        "body": body,
        "path": "src/widget.py",
        "position": 19,
    }


@pytest.mark.parametrize(
    "failure",
    [
        OSError("connection lost after comment send"),
        FakeResponse(500, {"secret": "response-body"}),
    ],
)
def test_publish_pr_comment_reconciles_after_single_uncertain_create_attempt(
    failure: FakeResponse | Exception,
) -> None:
    marker = "<!-- coderus-pr-review:RV-7:abc -->"
    body = f"review complete\n{marker}"
    client = FakeHttpClient(
        FakeResponse(200, []),
        failure,
        FakeResponse(
            200,
            [{"id": "hash-9", "body": body, "user": {"login": "coderus-bot"}}],
        ),
    )

    result = publisher(client).publish_pr_comment("acme", "widgets", 7, body, marker)

    assert result == PRCommentResult(
        url="https://gitcode.com/acme/widgets/merge_requests/7", created=True
    )
    assert [call["method"] for call in client.calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize(
    "malformation",
    [
        "missing_id",
        "empty_id",
        "invalid_id_type",
        "missing_body",
        "invalid_body_type",
        "body_mismatch",
    ],
)
@pytest.mark.parametrize("reconciled", [True, False], ids=["found", "not-found"])
def test_publish_pr_comment_reconciles_malformed_success_without_reposting(
    malformation: str,
    reconciled: bool,
) -> None:
    marker = "<!-- coderus-pr-review:RV-7:abc -->"
    body = f"review complete\n{marker}"
    response_payload: dict[str, object] = {"id": "hash-9", "body": body}
    if malformation == "missing_id":
        response_payload.pop("id")
    elif malformation == "empty_id":
        response_payload["id"] = ""
    elif malformation == "invalid_id_type":
        response_payload["id"] = True
    elif malformation == "missing_body":
        response_payload.pop("body")
    elif malformation == "invalid_body_type":
        response_payload["body"] = 7
    else:
        response_payload["body"] = f"server changed body\n{marker}"

    reconciled_comments = (
        [{"id": "hash-9", "body": body, "user": {"login": "coderus-bot"}}]
        if reconciled
        else []
    )
    client = FakeHttpClient(
        FakeResponse(200, []),
        FakeResponse(201, response_payload),
        FakeResponse(200, reconciled_comments),
    )

    if reconciled:
        result = publisher(client).publish_pr_comment(
            "acme", "widgets", 7, body, marker
        )
        assert result == PRCommentResult(
            url="https://gitcode.com/acme/widgets/merge_requests/7", created=True
        )
    else:
        with pytest.raises(PublisherRemoteError) as error:
            publisher(client).publish_pr_comment(
                "acme", "widgets", 7, body, marker
            )
        assert str(error.value) == "gitcode returned an invalid comment response"
        assert error.value.__cause__ is None

    methods = [call["method"] for call in client.calls]
    assert methods == ["GET", "POST", "GET"]
    assert methods.count("POST") == 1


def test_publish_pr_comment_reconciles_429_without_reposting() -> None:
    marker = "<!-- coderus-pr-review:RV-7:abc -->"
    body = f"review complete\n{marker}"
    client = FakeHttpClient(
        FakeResponse(200, []),
        FakeResponse(429, {}, {"Retry-After": "0"}),
        FakeResponse(
            200,
            [{"id": "hash-9", "body": body, "user": {"login": "coderus-bot"}}],
        ),
    )

    result = publisher(client, sleep=lambda _: None).publish_pr_comment(
        "acme", "widgets", 7, body, marker
    )

    assert result.created is True
    assert [call["method"] for call in client.calls] == ["GET", "POST", "GET"]
    assert [call["method"] for call in client.calls].count("POST") == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://gitcode.com/acme/widgets/pulls/7",
        "https://evil.invalid/acme/widgets/pulls/7",
        "https://token@gitcode.com/acme/widgets/pulls/7",
        "https://gitcode.com:443/acme/widgets/pulls/7",
        "https://gitcode.com/acme/widgets/pulls/7?token=secret",
        "https://gitcode.com/other/widgets/pulls/7",
        "https://gitcode.com/acme/widgets/pulls/8",
    ],
)
def test_publish_pr_comment_rejects_malicious_or_mismatched_html_url(url: str) -> None:
    marker = "<!-- coderus-pr-review:RV-7:abc -->"
    body = f"review complete\n{marker}"
    client = FakeHttpClient(
        FakeResponse(
            200,
            [{"id": 8, "body": body, "html_url": url, "user": {"login": "coderus-bot"}}],
        )
    )

    with pytest.raises(PublisherRemoteError, match="comment"):
        publisher(client).publish_pr_comment("acme", "widgets", 7, body, marker)


def test_retry_429_honors_parseable_retry_after() -> None:
    client = FakeHttpClient(
        FakeResponse(429, {"secret": "body"}, {"Retry-After": "2.5"}),
        FakeResponse(200, {"state": "open", "merged": False}),
    )
    sleeps: list[float] = []

    result = publisher(client, sleep=sleeps.append).get_pr_status("acme", "widgets", 7)

    assert result == "open"
    assert sleeps == [2.5]
    assert len(client.calls) == 2


def test_retry_delay_and_total_wall_time_are_capped() -> None:
    class Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def __call__(self) -> float:
            return self.now

        def sleep(self, delay: float) -> None:
            self.sleeps.append(delay)
            self.now += delay

    clock = Clock()
    client = FakeHttpClient(
        FakeResponse(429, {}, {"Retry-After": "999999"}),
        FakeResponse(429, {}, {"Retry-After": "999999"}),
        FakeResponse(200, {"state": "open", "merged": False}),
    )

    with pytest.raises(PublisherRemoteError, match="retry deadline"):
        publisher(
            client,
            request_attempts=5,
            max_retry_delay=2,
            max_retry_elapsed_seconds=3,
            clock=clock,
            sleep=clock.sleep,
        ).get_pr_status("acme", "widgets", 7)

    assert clock.sleeps == [2, 1]
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "responses",
    [
        [OSError("network secret") for _ in range(3)],
        [FakeResponse(500, {"secret": "response-body"}) for _ in range(3)],
    ],
)
def test_retryable_failure_stops_after_configured_attempts_without_body(
    responses: list[FakeResponse | Exception],
) -> None:
    token = "gitcode-secret"
    client = FakeHttpClient(*responses)

    with pytest.raises(PublisherRemoteError) as error:
        publisher(
            client,
            request_attempts=3,
            retry_interval=0,
            sleep=lambda _: None,
        ).get_pr_status("acme", "widgets", 7)

    assert len(client.calls) == 3
    assert "response-body" not in str(error.value)
    assert "network secret" not in str(error.value)
    assert token not in repr(error.value)
    assert error.value.__cause__ is None


def test_deterministic_4xx_is_not_retried_and_body_is_not_exposed() -> None:
    client = FakeHttpClient(FakeResponse(422, {"token": "response-body-secret"}))

    with pytest.raises(PublisherRemoteError) as error:
        publisher(client, request_attempts=3).get_pr_status("acme", "widgets", 7)

    assert len(client.calls) == 1
    assert str(error.value) == "gitcode request failed with status 422"
    assert "response-body-secret" not in repr(error.value)
