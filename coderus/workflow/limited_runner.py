from __future__ import annotations

import asyncio


class LimitedRunner:
    def __init__(self, runner: object, limit: int) -> None:
        if limit < 1:
            raise ValueError("runner limit must be positive")
        self._runner = runner
        self._semaphore = asyncio.Semaphore(limit)

    async def run(self, spec, *, cancel_event: asyncio.Event | None = None):
        async with self._semaphore:
            return await self._runner.run(spec, cancel_event=cancel_event)
