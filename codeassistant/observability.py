"""可观测性：统一的流式事件模型。

LangGraph 的 ``astream(..., stream_mode=["custom", "updates"])`` 会把
子图里的自定义写入（通过 ``get_stream_writer()``）原样吐出来，
本模块把这些原始事件包装成统一的 :class:`StreamEvent`，
并带上「专家 / 专家团」维度，前端据此按专家实时展示进度。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class EventType:
    """事件类型常量。"""

    EXPERT_STARTED = "expert_started"
    EXPERT_PLAN = "expert_plan"
    EXPERT_TOKEN = "expert_token"
    EXPERT_TOOL_CALL = "expert_tool_call"
    EXPERT_TOOL_RESULT = "expert_tool_result"
    EXPERT_APPROVAL = "approval_required"
    EXPERT_COMPLETED = "expert_completed"
    TEAM_ROUND = "team_round"
    TEAM_DISPATCH = "team_dispatch"
    TEAM_COMPLETED = "team_completed"
    ORCHESTRATOR_ROUTE = "orchestrator_route"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    ERROR = "error"


@dataclass
class StreamEvent:
    """一条流式事件。"""

    type: str
    expert: str | None = None
    team: str | None = None
    node: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "expert": self.expert,
            "team": self.team,
            "node": self.node,
            "payload": self.payload,
            "ts": self.ts,
        }


class Tracer(Protocol):
    """追踪回调接口（实现它即可接入 APM / LangSmith 等）。"""

    def on_event(self, event: StreamEvent) -> None:  # pragma: no cover - 协议声明
        ...


class NullTracer:
    """默认的无操作追踪器。"""

    def on_event(self, event: StreamEvent) -> None:
        return None


class EventBus:
    """事件广播：既写入 LangGraph 流，也回调追踪器。"""

    #: 保留的历史事件上限（长驻服务下防止内存无限增长）
    MAX_HISTORY = 2000

    def __init__(self, tracer: Tracer | None = None) -> None:
        self.tracer = tracer or NullTracer()
        self.history: list[StreamEvent] = []

    def emit(self, event: StreamEvent) -> None:
        self.history.append(event)
        if len(self.history) > self.MAX_HISTORY:
            del self.history[: len(self.history) - self.MAX_HISTORY]
        try:
            self.tracer.on_event(event)
        except Exception:  # pragma: no cover - 追踪器异常不能影响主流程
            pass

    def emit_to(
        self,
        writer: Callable[[Any], Any] | None,
        event: StreamEvent,
    ) -> None:
        """把事件写进 LangGraph 自定义流 + 广播给追踪器。"""

        self.emit(event)
        if writer is not None:
            try:
                writer(event.to_dict())
            except Exception:  # pragma: no cover
                pass


def get_writer() -> Callable[[Any], Any] | None:
    """获取 LangGraph 自定义流写入器（不在图上下文中时返回 ``None``）。"""

    try:
        from langgraph.config import get_stream_writer
    except Exception:  # pragma: no cover - 旧版本 langgraph
        return None
    try:
        return get_stream_writer()
    except Exception:
        return None


def extract_interrupts(chunk: Any) -> list[Any]:
    """从 LangGraph 流式分片中提取中断信息。"""

    if not isinstance(chunk, dict):
        return []
    if "__interrupt__" in chunk:
        value = chunk["__interrupt__"]
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]
    return []


def parse_stream_chunk(chunk: Any) -> tuple[tuple[str, ...], str, Any]:
    """拆解 LangGraph 的流式分片。

    ``subgraphs=True`` 时分片是 ``(namespace, mode, data)`` 三元组，
    否则是 ``(mode, data)`` 二元组。
    """

    if isinstance(chunk, tuple) and len(chunk) == 3:
        namespace, mode, data = chunk
        return tuple(str(part) for part in (namespace or ())), str(mode), data
    if isinstance(chunk, tuple) and len(chunk) == 2:
        mode, data = chunk
        return (), str(mode), data
    return (), "updates", chunk


def scope_from_namespace(namespace: tuple[str, ...]) -> tuple[str | None, str | None]:
    """从命名空间推断（专家名，专家团名）。

    命名空间元素形如 ``member__sdt-backend:<task-id>`` 或 ``team__xxx:<task-id>``。
    """

    expert: str | None = None
    team: str | None = None
    for part in namespace:
        node = part.split(":", 1)[0]
        if node.startswith("member__"):
            expert = node[len("member__") :]
        elif node.startswith("expert__"):
            expert = node[len("expert__") :]
        elif node.startswith("team__"):
            team = node[len("team__") :]
    return expert, team


def format_event(event: StreamEvent) -> str:
    """把事件格式化成一行终端可读文本。"""

    scope = event.expert or event.team or event.node or "-"
    payload = event.payload or {}
    detail = payload.get("text") or payload.get("summary") or ""
    if not detail:
        detail = ", ".join(
            f"{key}={payload[key]}" for key in sorted(payload) if key != "text"
        )
    return f"[{event.type:<22}] {scope:<28} {detail}".rstrip()
