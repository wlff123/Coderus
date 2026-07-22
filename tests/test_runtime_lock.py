from pathlib import Path

import pytest

from coderus.runtime_lock import ActiveManagerLock


def test_active_manager_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    path = tmp_path / "manager.lock"
    first = ActiveManagerLock(path)
    second = ActiveManagerLock(path)

    first.acquire()
    with pytest.raises(RuntimeError, match="already active"):
        second.acquire()
    first.release()

    second.acquire()
    second.release()
