"""PublishRequest：构造即校验，非法输入必须在进入 Forge 前被拒绝。"""

from __future__ import annotations

from pathlib import Path

import pytest

from coderus.forge import PublishRequest


def request_kwargs(workspace: Path, **overrides) -> dict:
    kwargs = {
        "workspace": workspace,
        "upstream_owner": "acme",
        "repository_name": "widgets",
        "default_branch": "main",
        "branch": "coderus/issue-42-7",
        "title": "Fix parser",
        "body": "报告",
    }
    kwargs.update(overrides)
    return kwargs


def test_valid_request_normalizes_workspace(tmp_path: Path) -> None:
    request = PublishRequest(**request_kwargs(tmp_path))

    assert request.workspace == tmp_path.resolve()
    assert request.upstream_owner == "acme"


@pytest.mark.parametrize(
    "overrides",
    [
        {"upstream_owner": " "},
        {"repository_name": ""},
        {"title": "   "},
        {"default_branch": ""},
        {"branch": ""},
    ],
)
def test_blank_fields_are_rejected(tmp_path: Path, overrides: dict) -> None:
    with pytest.raises(ValueError):
        PublishRequest(**request_kwargs(tmp_path, **overrides))


def test_relative_workspace_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        PublishRequest(**request_kwargs(Path("relative/workspace")))


def test_branch_must_differ_from_default_branch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="differ"):
        PublishRequest(**request_kwargs(tmp_path, branch="main"))
