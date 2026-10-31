"""Only host owners receive quota readings, including through usage."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host.claude_quotas import ClaudeAccountQuota
from omnigent.host.frames import (
    CAP_CLAUDE_QUOTAS,
    HostClaudeQuotasFrame,
    HostClaudeQuotasResultFrame,
    HostHelloFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.claude_quotas import (
    create_claude_quotas_router,
    request_host_claude_quotas,
)
from omnigent.server.routes.usage import create_usage_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.routes.test_mcp_servers import _Auth

_HOST = "a828988dc0b441fb8d04dad3761773b9"


@pytest.fixture
def quota_app(db_uri):
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(_HOST, "laptop", "owner")
    registry = HostRegistry()
    conn = registry.register(
        _HOST,
        AsyncMock(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="laptop",
            capabilities=[CAP_CLAUDE_QUOTAS],
        ),
        owner="owner",
    )
    app = FastAPI()
    app.include_router(create_claude_quotas_router(registry, hosts, auth_provider=_Auth()))
    app.include_router(
        create_usage_router(
            SqlAlchemyConversationStore(db_uri),
            host_registry=registry,
            host_store=hosts,
            auth_provider=_Auth(),
        )
    )

    @app.exception_handler(OmnigentError)
    async def error(request, exc):
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    return app, registry, conn


@pytest.mark.parametrize("path", [f"/hosts/{_HOST}/claude-quotas", "/usage"])
async def test_owner_receives_account_quota_readings(quota_app, path):
    app, _, conn = quota_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(client.get(path, headers={"x-test-user": "owner"}))
        frame = decode_host_frame((await asyncio.wait_for(conn.outbound_queue.get(), 2)).data)
        assert isinstance(frame, HostClaudeQuotasFrame)
        conn.pending_claude_quotas[frame.request_id].set_result(
            HostClaudeQuotasResultFrame(frame.request_id, [ClaudeAccountQuota(account="work")])
        )
        response = await task
    assert response.status_code == 200
    readings = (
        response.json()["quotas"]
        if path != "/usage"
        else response.json()["claude_quotas"][0]["quotas"]
    )
    assert readings[0]["account"] == "work"
    assert readings[0]["status"] == "unavailable"
    assert not conn.pending_claude_quotas


async def test_collaborator_cannot_read_host_quotas(quota_app):
    app, _, conn = quota_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/hosts/{_HOST}/claude-quotas", headers={"x-test-user": "collaborator"}
        )
        usage = await client.get("/usage", headers={"x-test-user": "collaborator"})
    assert response.status_code == 403
    assert usage.json()["claude_quotas"] == []
    assert conn.outbound_queue.empty()


async def test_old_host_and_lost_tunnel_return_unavailable(quota_app):
    _, registry, conn = quota_app
    conn.hello.capabilities.clear()
    assert (await request_host_claude_quotas(registry, conn))[0].status == "unavailable"
    assert conn.outbound_queue.empty()
    conn.hello.capabilities.append(CAP_CLAUDE_QUOTAS)
    task = asyncio.create_task(request_host_claude_quotas(registry, conn))
    await asyncio.wait_for(conn.outbound_queue.get(), 2)
    registry.deregister(conn.host_id)
    assert (await task)[0].status == "unavailable"
    assert not conn.pending_claude_quotas
