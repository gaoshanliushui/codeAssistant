"""Web 服务测试（进程内 ASGI，不占端口、不触网）。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from codeassistant.server import create_app
from codeassistant.settings import Settings


@pytest.fixture
def api(settings: Settings):
    return create_app(settings)


@pytest.fixture
async def client(api):
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_health(client: httpx.AsyncClient):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provider"] == "mock"


async def test_catalog(client: httpx.AsyncClient):
    response = await client.get("/api/catalog")
    assert response.status_code == 200
    body = response.json()
    assert body["experts"]
    assert body["teams"]


async def test_index_page_is_served(client: httpx.AsyncClient):
    response = await client.get("/")
    assert response.status_code == 200
    assert "CodeAssistant" in response.text


async def test_chat_streams_sse_events(client: httpx.AsyncClient):
    events: list[dict] = []
    async with client.stream(
        "POST", "/api/chat", json={"task": "看看代码结构"}
    ) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            events.append(json.loads(payload))

    types = {event["type"] for event in events}
    assert "run_started" in types
    assert "orchestrator_route" in types
    assert "run_completed" in types


async def test_chat_with_explicit_team(client: httpx.AsyncClient):
    events: list[dict] = []
    async with client.stream(
        "POST",
        "/api/chat",
        json={"task": "写稿", "team": "toutiao-finance-account-ops-team", "mode": "parallel"},
    ) as response:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            events.append(json.loads(payload))

    types = {event["type"] for event in events}
    assert "team_dispatch" in types or "team_completed" in types


async def test_reload_endpoint(client: httpx.AsyncClient):
    response = await client.post("/api/reload")
    assert response.status_code == 200
    assert "summary" in response.json()


async def test_approvals_endpoint_reports_unknown_request(client: httpx.AsyncClient):
    response = await client.post(
        "/api/approvals",
        json={"request_id": "does-not-exist", "approved": True, "reason": "x"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False


async def test_approval_gate_blocks_until_resolved():
    """审批闸门：等待期间挂起，收到结论后放行。"""

    from codeassistant.approval import ApprovalRequest
    from codeassistant.server import ApprovalGate

    gate = ApprovalGate()
    request = ApprovalRequest(id="req-1", expert="demo", tool="write_file")

    async def waiter() -> tuple[bool, str]:
        return await gate.wait(request)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert not task.done(), "未给出结论前应保持挂起"

    assert gate.resolve("req-1", True, "允许") is True
    approved, note = await asyncio.wait_for(task, timeout=5)
    assert approved is True
    assert note == "允许"


async def test_approval_gate_rejects_unknown_request():
    from codeassistant.server import ApprovalGate

    gate = ApprovalGate()
    assert gate.resolve("nope", True) is False


async def test_approval_gate_resolves_pending_request_via_api(
    api, client: httpx.AsyncClient
):
    """通过 HTTP 接口给出结论，等待中的执行随即继续。"""

    from codeassistant.approval import ApprovalRequest

    gate = api.state.approval_gate
    request = ApprovalRequest(id="req-42", expert="demo", tool="run_command")

    async def waiter() -> tuple[bool, str]:
        return await gate.wait(request)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)

    response = await client.post(
        "/api/approvals",
        json={"request_id": "req-42", "approved": False, "reason": "拒绝"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    approved, note = await asyncio.wait_for(task, timeout=5)
    assert approved is False
    assert note == "拒绝"
