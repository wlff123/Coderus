from types import SimpleNamespace

import pytest

from coderus.pr_review import workspace as module
from coderus.pr_review.orchestrator import PRReviewOrchestrator
from coderus.pr_review.workspace import PRWorkspace, PRWorkspaceError


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,expected", [
    (b"RPC failed; curl 56 SSL_read secret-key https://private.test", "网络连接"),
    (b"fatal: could not read Username secret-key", "认证"),
    (b"No space left on device secret-key", "磁盘"),
    (b"unknown secret-key", "Git 命令执行失败"),
])
async def test_git_failure_is_actionable_without_exposing_stderr(
    tmp_path, monkeypatch, raw, expected,
):
    async def failed(*args, **kwargs):
        return SimpleNamespace(returncode=128, stdout=b"", stderr=raw)

    monkeypatch.setattr(module, "run_process", failed)
    with pytest.raises(PRWorkspaceError) as error:
        await PRWorkspace(tmp_path)._run("git", "fetch", "upstream", cwd=tmp_path)
    summary = PRReviewOrchestrator._safe_summary(error.value)
    assert expected in summary
    assert "Git fetch" in summary
    assert "secret-key" not in summary
    assert "private.test" not in summary
