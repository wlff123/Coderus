from types import SimpleNamespace

import pytest

from coderus.forge import ProviderError, Repository
from coderus.pr_review.models import ChangedRanges, ReviewInput
from coderus.pr_review.spec_source import SpecSource, resolve_spec_source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_issue", "body", "expected"),
    [
        (True, "Spec: `specs/requirements.md`", SpecSource("Issue #123", "标题\n\n需求原文")),
        (False, "Spec: `specs/requirements.md`", SpecSource("specs/requirements.md")),
        (False, "Spec: `../../outside.md`", SpecSource("docs/branch-spec.md")),
    ],
)
async def test_resolves_spec_in_skill_order(has_issue, body, expected) -> None:
    repository = Repository("gitcode", "org", "repo", "https://gitcode.com/org/repo")

    def get_issue(repo, number):
        assert (repo, number) == (repository, 123)
        if not has_issue:
            raise ProviderError()
        return SimpleNamespace(title="标题", body="需求原文")

    material = ReviewInput(
        ChangedRanges({}),
        "diff",
        "fixed-base",
        "Closes #123",
        ("docs/branch-spec.md", "specs/requirements.md"),
    )
    assert await resolve_spec_source(
        material,
        repository=repository,
        provider=SimpleNamespace(get_issue=get_issue),
        head_ref="feature/branch",
        pr_body=body,
    ) == expected
