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
    tmp_path, monkeypatch, caplog, raw, expected,
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
    assert "secret-key" not in caplog.text
    assert "private.test" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 2, 3])
async def test_network_preparation_retry_is_bounded(tmp_path, monkeypatch, caplog, failures):
    calls = []
    delays = []
    manager = PRWorkspace(tmp_path)

    async def prepare_once(*args, **kwargs):
        calls.append(module._TASK_ID.get())
        if len(calls) <= failures:
            raise module.GitNetworkError("网络异常")
        return tmp_path

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(manager, "_prepare_once", prepare_once)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    if failures == 3:
        with pytest.raises(module.GitNetworkError, match="自动重试 2 次"):
            await manager.prepare(task_id=595)
    else:
        assert await manager.prepare(task_id=595) == tmp_path
    assert calls == [595] * min(failures + 1, 3)
    assert delays == [5, 15][:min(failures, 2)]
    assert module._TASK_ID.get() is None
    assert "RV-595" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PRWorkspaceError("认证失败"), module.asyncio.CancelledError()])
async def test_permanent_failure_and_cancellation_are_not_retried(tmp_path, monkeypatch, error):
    manager = PRWorkspace(tmp_path)
    calls = []

    async def prepare_once(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(manager, "_prepare_once", prepare_once)
    with pytest.raises(type(error)):
        await manager.prepare(task_id=595)
    assert len(calls) == 1
    assert module._TASK_ID.get() is None


def test_git_diagnostics_never_echo_credentials():
    raw = (b'RPC failed; curl 56 SSL_read https://user:password@host/?key=abc '
           b'Authorization: Bearer abc sk-secret')
    diagnostic, retryable = module._git_diagnostics(raw)
    assert diagnostic == "rpc failed, ssl"
    assert retryable
    assert module._git_diagnostics(b'403 SSL authentication failed secret') == (
        "authentication", False
    )


@pytest.mark.asyncio
async def test_retry_discards_partial_clone_and_uses_fresh_staging(tmp_path, monkeypatch):
    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "staging")
    checkouts = []
    revisions = iter(["a" * 40, "b" * 40])

    async def run(*args, cwd):
        if args[1] == "clone":
            checkout = module.Path(args[-1])
            if checkouts:
                assert not checkouts[0].exists()
                assert checkout != checkouts[0]
            checkout.mkdir()
            checkouts.append(checkout)
            if len(checkouts) == 1:
                (checkout / "partial").touch()
                raise module.GitNetworkError("网络异常")
        if args[1] == "rev-parse":
            return next(revisions)
        return ""

    async def sleep(_):
        pass

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    workspace = await manager.prepare(
        595, "https://gitcode.com/acme/example", 1, "main", "a" * 40, "b" * 40,
        "feature", "https://gitcode.com/acme/example",
    )
    assert len(checkouts) == 2
    assert not (workspace / "partial").exists()
    assert list(manager.staging_root.iterdir()) == []
