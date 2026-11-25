"""Keep snapshot reads and cached catalog metadata off runner recovery workers."""

from __future__ import annotations

import asyncio
import os

import cachetools

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.llms.context_window import resolve_effective_context_window
from omnigent.util.dedicated_executor import DedicatedExecutor

_Key = tuple[int | None, str | None, str | None, str | None]
_WINDOWS: WorkspaceScopedCache[_Key, int | None] = WorkspaceScopedCache(
    lambda: cachetools.TTLCache(maxsize=1024, ttl=60)
)
_INFLIGHT: WorkspaceScopedCache[_Key, asyncio.Task[int | None]] = WorkspaceScopedCache()
_READS = DedicatedExecutor(
    max_workers=4, thread_name_prefix="server-snapshot-read", process_role="server"
)
run_snapshot_read = _READS.run
_CATALOG = DedicatedExecutor(
    max_workers=4, thread_name_prefix="server-snapshot-catalog", process_role="server"
)


async def snapshot_context_window(
    spec_context_window: int | None,
    model: str | None,
    *,
    model_override: str | None = None,
) -> int | None:
    """Coalesce catalog lookups and cache display windows for sixty seconds.

    Explicit windows and model-less sessions need no catalog. Compaction keeps
    using the resolver directly; this cache belongs only to snapshot display.
    """
    effective_model = model_override if model_override is not None else model
    if (spec_context_window is not None and model_override is None) or not effective_model:
        return resolve_effective_context_window(
            spec_context_window, model, model_override=model_override
        )
    key = (
        spec_context_window,
        model,
        model_override,
        os.environ.get("AP_CONTEXT_WINDOW_OVERRIDE"),
    )
    try:
        return _WINDOWS[key]
    except KeyError:
        pass
    task = _INFLIGHT.get(key)
    if task is None:

        async def load() -> int | None:
            window = await _CATALOG.run(
                resolve_effective_context_window,
                spec_context_window,
                model,
                model_override=model_override,
            )
            _WINDOWS[key] = window
            return window

        task = asyncio.create_task(load())
        _INFLIGHT[key] = task

        def finished(done: asyncio.Task[int | None]) -> None:
            if _INFLIGHT.get(key) is done:
                _INFLIGHT.pop(key, None)
            # A cancelled HTTP request can leave the shared lookup without a waiter.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
    return await asyncio.shield(task)
