"""Wait for thread-owned resources before propagating task cancellation."""

import asyncio
from collections.abc import Callable


async def run_sync[Value](operation: Callable[[], Value]) -> Value:
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise
