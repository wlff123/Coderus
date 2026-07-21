import asyncio
import sys
import time
from pathlib import Path

import pytest

from coderus.processes import CommandTimedOut, run_process, run_process_sync

_SPAWN_CHILD = """
import subprocess
import sys
import time
from pathlib import Path

child_code = (
    "import sys,time; from pathlib import Path; "
    "time.sleep(1); Path(sys.argv[1]).write_text('orphan', encoding='utf-8')"
)
subprocess.Popen([sys.executable, "-c", child_code, sys.argv[1]])
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
time.sleep(30)
"""


@pytest.mark.asyncio
async def test_run_process_timeout_terminates_the_process_group(tmp_path: Path) -> None:
    orphan_marker = tmp_path / "orphan.txt"
    ready = tmp_path / "ready.txt"
    slow_child = _SPAWN_CHILD.replace("time.sleep(1)", "time.sleep(3)")

    with pytest.raises(CommandTimedOut):
        await run_process(
            (sys.executable, "-c", slow_child, str(orphan_marker), str(ready)),
            cwd=tmp_path,
            env={},
            timeout_seconds=1.0,
        )

    await asyncio.sleep(3.1)
    assert ready.exists()
    assert not orphan_marker.exists()


@pytest.mark.asyncio
async def test_run_process_cancellation_terminates_the_process_group(tmp_path: Path) -> None:
    orphan_marker = tmp_path / "cancelled-orphan.txt"
    ready = tmp_path / "cancelled-ready.txt"
    running = asyncio.create_task(
        run_process(
            (sys.executable, "-c", _SPAWN_CHILD, str(orphan_marker), str(ready)),
            cwd=tmp_path,
            env={},
            timeout_seconds=30,
        )
    )
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.01)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    await asyncio.sleep(1.1)
    assert ready.exists()
    assert not orphan_marker.exists()


def test_run_process_sync_timeout_terminates_the_process_group(tmp_path: Path) -> None:
    orphan_marker = tmp_path / "sync-orphan.txt"
    ready = tmp_path / "sync-ready.txt"
    slow_child = _SPAWN_CHILD.replace("time.sleep(1)", "time.sleep(3)")

    with pytest.raises(CommandTimedOut):
        run_process_sync(
            (sys.executable, "-c", slow_child, str(orphan_marker), str(ready)),
            cwd=tmp_path,
            env={},
            timeout_seconds=1.0,
        )

    time.sleep(3.1)
    assert ready.exists()
    assert not orphan_marker.exists()
