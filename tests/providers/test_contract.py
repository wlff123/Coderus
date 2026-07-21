from dataclasses import FrozenInstanceError

import pytest

from coderus.providers import (
    Issue,
    Repository,
)


def test_provider_models_are_immutable_value_objects() -> None:
    repository = Repository(
        provider="github",
        owner="octocat",
        name="Hello-World",
        canonical_url="https://github.com/octocat/Hello-World",
    )
    issue = Issue(
        repository=repository,
        external_id="1",
        number=7,
        title="Example",
        body=None,
        state="open",
        labels=("bug",),
        canonical_url="https://github.com/octocat/Hello-World/issues/7",
        created_at=None,
        updated_at=None,
    )
    assert issue.labels == ("bug",)
    with pytest.raises(FrozenInstanceError):
        repository.owner = "changed"  # type: ignore[misc]
