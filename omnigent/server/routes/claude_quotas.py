"""Owner-authorized Claude quota reads over the host tunnel."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, Request

from omnigent.host.claude_quotas import ClaudeAccountQuota
from omnigent.host.frames import (
    CAP_CLAUDE_QUOTAS,
    HostClaudeQuotasFrame,
    HostClaudeQuotasResultFrame,
    encode_host_frame,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.stores.host_store import HostStore


async def request_host_claude_quotas(
    registry: HostRegistry, conn: HostConnection
) -> list[ClaudeAccountQuota]:
    """Return sanitized readings, or unavailable when the host cannot answer."""
    unavailable = [ClaudeAccountQuota(account="default")]
    if CAP_CLAUDE_QUOTAS not in conn.hello.capabilities:
        return unavailable
    request_id = secrets.token_hex(8)
    future: asyncio.Future[HostClaudeQuotasResultFrame] = (
        asyncio.get_running_loop().create_future()
    )
    conn.pending_claude_quotas[request_id] = future
    try:
        registry.send_text(conn, encode_host_frame(HostClaudeQuotasFrame(request_id)))
        # Allow the host's 20-second account reads plus tunnel overhead.
        result = await asyncio.wait_for(future, timeout=25.0)
        return result.quotas or unavailable
    except (ConnectionError, TimeoutError):
        return unavailable
    finally:
        conn.pending_claude_quotas.pop(request_id, None)


def create_claude_quotas_router(
    registry: HostRegistry, hosts: HostStore, *, auth_provider: AuthProvider | None = None
) -> APIRouter:
    router = APIRouter()

    @router.get("/hosts/{host_id}/claude-quotas")
    async def get_quotas(request: Request, host_id: str) -> dict[str, list[ClaudeAccountQuota]]:
        user = require_user(request, auth_provider)
        await asyncio.to_thread(
            resolve_host_owner, user_id=user, host_id=host_id, host_store=hosts
        )
        conn = registry.get(host_id)
        quotas = (
            await request_host_claude_quotas(registry, conn)
            if conn
            else [ClaudeAccountQuota(account="default")]
        )
        return {"quotas": quotas}

    return router
