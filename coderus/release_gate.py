from __future__ import annotations

import os
from pathlib import Path

from coderus.config import Settings


class ReleaseGate:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    @classmethod
    def from_settings(cls, settings: Settings) -> ReleaseGate:
        configured = os.environ.get("CODERUS_RELEASE_GATE")
        path = (
            Path(configured)
            if configured
            else settings.database.path.expanduser().resolve().parent / "release-draining"
        )
        return cls(path)

    def allows_work(self) -> bool:
        return not self.path.exists()
