"""Snapshot metadata is shared briefly while store work remains independent."""

from __future__ import annotations

import asyncio
import threading

import cachetools
import pytest

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.llms import context_window
from omnigent.server import snapshot_reads as metadata
from tests.budgets import budget


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr(
        metadata,
        "_WINDOWS",
        WorkspaceScopedCache(lambda: cachetools.TTLCache(maxsize=2, ttl=60, timer=lambda: now[0])),
    )
    monkeypatch.setattr(metadata, "_INFLIGHT", WorkspaceScopedCache())
    monkeypatch.delenv("AP_CONTEXT_WINDOW_OVERRIDE", raising=False)
    return now


async def test_coalesces_pollers_and_preserves_lookup_after_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def lookup(model: str) -> int:
        calls.append(model)
        entered.set()
        assert release.wait(budget(5))
        return 200_000

    monkeypatch.setattr(context_window, "get_model_context_window", lookup)
    first = asyncio.create_task(metadata.snapshot_context_window(None, "shared-model"))
    try:
        async with asyncio.timeout(budget(2)):
            while not entered.is_set():
                await asyncio.sleep(0)
        others = [
            asyncio.create_task(metadata.snapshot_context_window(None, "shared-model"))
            for _ in range(8)
        ]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await asyncio.gather(*others) == [200_000] * 8
        assert await metadata.snapshot_context_window(None, "shared-model") == 200_000
        assert calls == ["shared-model"]
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


async def test_cache_expires_and_keeps_workspace_and_override_identity(
    monkeypatch: pytest.MonkeyPatch, isolate_cache: list[float]
) -> None:
    calls: list[tuple[int, str]] = []

    def lookup(model: str) -> int:
        calls.append((current_workspace_id(), model))
        return len(calls) * 100_000

    monkeypatch.setattr(context_window, "get_model_context_window", lookup)
    with workspace_scope(1):
        assert await metadata.snapshot_context_window(None, "first") == 100_000
        assert await metadata.snapshot_context_window(None, "first") == 100_000
        isolate_cache[0] = 60
        assert await metadata.snapshot_context_window(None, "first") == 200_000
    with workspace_scope(2):
        assert await metadata.snapshot_context_window(None, "first") == 300_000
        assert (
            await metadata.snapshot_context_window(None, "first", model_override="second")
            == 400_000
        )
    with workspace_scope(1):
        assert await metadata.snapshot_context_window(None, "first") == 500_000
    assert calls == [(1, "first"), (1, "first"), (2, "first"), (2, "second"), (1, "first")]


async def test_declared_window_and_environment_override_keep_resolver_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def lookup(model: str) -> int:
        calls.append(model)
        return 128_000

    monkeypatch.setattr(context_window, "_catalog_context_window", lookup)
    assert await metadata.snapshot_context_window(1_000_000, "first") == 1_000_000
    assert await metadata.snapshot_context_window(None, None) is None
    assert calls == []
    assert (
        await metadata.snapshot_context_window(1_000_000, "first", model_override="second")
        == 128_000
    )
    monkeypatch.setenv("AP_CONTEXT_WINDOW_OVERRIDE", "500000")
    assert (
        await metadata.snapshot_context_window(1_000_000, "first", model_override="second")
        == 500_000
    )
    assert calls == ["second"]


async def test_failed_lookup_retries_without_caching_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def lookup(model: str) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("catalog unavailable")
        return 128_000

    monkeypatch.setattr(context_window, "get_model_context_window", lookup)
    with pytest.raises(RuntimeError, match="catalog unavailable"):
        await metadata.snapshot_context_window(None, "retry-model")
    assert await metadata.snapshot_context_window(None, "retry-model") == 128_000
    assert calls == 2
