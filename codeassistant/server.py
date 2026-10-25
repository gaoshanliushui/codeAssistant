"""FastAPI + SSE Web 服务。

接口：

* ``GET  /``                 单页界面
* ``GET  /api/catalog``      专家 / 专家团目录
* ``POST /api/chat``         以 SSE 流式执行任务（按专家维度实时吐进度）
* ``POST /api/approvals``    对挂起的高危操作给出通过 / 拒绝
* ``GET  /api/health``       健康检查

审批是**异步挂起**的：SSE 流在审批点停住，前端弹出审批卡片，
用户点确认后调用 ``/api/approvals``，被挂起的执行随即从子图断点恢复。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .app import CodeAssistant
from .approval import ApprovalRequest
from .settings import Settings

WEB_DIR = Path(__file__).resolve().parent / "web"


class ApprovalGate:
    """把「等待人工审批」变成一个可跨请求等待的 Future。"""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[tuple[bool, str]]] = {}

    async def wait(self, request: ApprovalRequest) -> tuple[bool, str]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, str]] = loop.create_future()
        self._waiters[request.id] = future
        try:
            return await future
        finally:
            self._waiters.pop(request.id, None)

    def resolve(self, request_id: str, approved: bool, reason: str = "") -> bool:
        future = self._waiters.get(request_id)
        if future is None or future.done():
            return False
        future.set_result((approved, reason))
        return True

    def pending_ids(self) -> list[str]:
        return list(self._waiters)


class ChatRequest(BaseModel):
    task: str
    session_id: str | None = None
    expert: str | None = None
    team: str | None = None
    mode: str | None = None
    context: list[str] = Field(default_factory=list)


class ApprovalRequestBody(BaseModel):
    request_id: str
    approved: bool
    reason: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。"""

    app_settings = settings or Settings.load()
    assistant = CodeAssistant(app_settings)
    gate = ApprovalGate()

    api = FastAPI(title="CodeAssistant", version="0.1.0")
    # 暴露给测试与调试用（注册自定义专家、注入模型等）
    api.state.assistant = assistant
    api.state.approval_gate = gate

    @api.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "provider": app_settings.provider,
                "model": app_settings.model,
                "workspace": str(app_settings.workspace),
            }
        )

    @api.get("/api/catalog")
    async def catalog() -> JSONResponse:
        return JSONResponse(assistant.list_catalog())

    @api.post("/api/chat")
    async def chat(body: ChatRequest) -> StreamingResponse:
        async def event_source() -> AsyncIterator[str]:
            queue: asyncio.Queue[str | None] = asyncio.Queue()

            async def approval_callback(request: ApprovalRequest) -> tuple[bool, str]:
                payload = {
                    "type": "approval_required",
                    "request_id": request.id,
                    "expert": request.expert,
                    "team": request.team,
                    "tool": request.tool,
                    "args": request.args,
                    "reason": request.reason,
                    "risk": request.risk,
                }
                await queue.put(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
                return await gate.wait(request)

            async def runner() -> None:
                try:
                    async for event in assistant.stream_run(
                        body.task,
                        session_id=body.session_id,
                        expert=body.expert,
                        team=body.team,
                        mode=body.mode,
                        context=body.context,
                        approval_callback=approval_callback,
                    ):
                        await queue.put(
                            f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
                        )
                except Exception as exc:  # pragma: no cover - 兜底
                    error = {"type": "error", "payload": {"message": str(exc)}}
                    await queue.put(f"data: {json.dumps(error, ensure_ascii=False)}\n\n")
                finally:
                    await queue.put(None)

            task = asyncio.create_task(runner())
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @api.post("/api/approvals")
    async def approvals(body: ApprovalRequestBody) -> JSONResponse:
        ok = gate.resolve(body.request_id, body.approved, body.reason)
        try:
            assistant.approval_center.resolve(
                body.request_id, approved=body.approved, note=body.reason
            )
        except KeyError:
            pass
        return JSONResponse({"ok": ok, "request_id": body.request_id})

    @api.post("/api/reload")
    async def reload() -> JSONResponse:
        report = assistant.reload()
        return JSONResponse(
            {
                "summary": report.summary(),
                "experts": report.touched_experts(),
                "teams": report.touched_teams(),
            }
        )

    @api.get("/")
    async def index() -> Any:
        page = WEB_DIR / "index.html"
        if page.is_file():
            return FileResponse(page)
        return JSONResponse({"detail": "web/index.html 未找到"}, status_code=404)

    @api.on_event("shutdown")
    async def shutdown() -> None:  # pragma: no cover - 生命周期钩子
        await assistant.aclose()

    return api


def serve(settings: Settings | None = None, host: str = "127.0.0.1", port: int = 8765) -> None:
    """启动 Web 服务（阻塞）。"""

    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
