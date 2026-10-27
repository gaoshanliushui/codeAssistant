"""应用门面。

把「配置 → 沙箱 → 工具 → 专家注册表 → 图工厂 → 主编排器」串成一个可用的
CodeAssistant 实例，CLI 与 Web 服务都基于它。

对外只暴露三件事：

* :meth:`CodeAssistant.stream_run` —— 流式执行（带专家维度进度与审批事件）
* :meth:`CodeAssistant.run` —— 跑完拿到最终结果
* :meth:`CodeAssistant.list_catalog` —— 专家 / 专家团目录
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langgraph.types import Command

from .approval import ApprovalCenter, ApprovalRequest
from .configs import ExpertConfig, TeamConfig
from .graphs.factory import ExpertGraphFactory
from .graphs.orchestrator import build_orchestrator
from .memory import ThreadScope, build_checkpointer
from .observability import (
    EventBus,
    EventType,
    StreamEvent,
    extract_interrupts,
    parse_stream_chunk,
    scope_from_namespace,
)
from .registry import ExpertRegistry
from .sessions import SessionStore
from .settings import Settings
from .tools.registry import ToolRegistry
from .tools.sandbox import Sandbox

#: 审批回调：收到请求后返回 (是否通过, 说明)
ApprovalCallback = Callable[[ApprovalRequest], Awaitable[tuple[bool, str]]]


def _jsonable(value: Any) -> Any:
    """把状态里的对象转成可 JSON 序列化的结构。"""

    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass
class RunResult:
    """一次执行的结果。"""

    summary: str = ""
    status: str = "ok"
    route: str = ""
    target: str = ""
    reasoning: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    events: list[StreamEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "status": self.status,
            "route": self.route,
            "target": self.target,
            "reasoning": self.reasoning,
            "result": self.result,
        }


class CodeAssistant:
    """代码助手应用。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()
        self.sandbox = Sandbox(self.settings.workspace)
        self.tool_registry = ToolRegistry(self.sandbox)
        self.registry = ExpertRegistry(self.settings, tools=self.tool_registry)
        self.approval_center = ApprovalCenter()
        self.event_bus = EventBus()
        self.checkpointer = build_checkpointer(self.settings)
        #: 会话记录（可回看的对话流水，落在 .codeassistant/sessions.json）
        self.sessions = SessionStore(self.settings.sessions_file)
        self.factory = ExpertGraphFactory(
            self.registry,
            self.settings,
            tool_registry=self.tool_registry,
            approval_center=self.approval_center,
            checkpointer=self.checkpointer,
            event_bus=self.event_bus,
        )
        self._orchestrator: Any = None

    # -- 目录 -------------------------------------------------------------

    def list_catalog(self) -> dict[str, Any]:
        """专家 / 专家团 / 技能目录。"""

        return {
            "experts": [
                {
                    "name": config.name,
                    "display_name": config.display_name,
                    "profession": config.profession,
                    "description": config.description,
                    "tools": list(config.tools),
                    "skills": list(config.skills),
                    "hitl": list(config.hitl.tools),
                    "source": config.source,
                }
                for config in self.registry.list_experts()
            ],
            "teams": [
                {
                    "name": team.name,
                    "display_name": team.display_name,
                    "description": team.description,
                    "lead": team.lead,
                    "members": list(team.members),
                    "mode": team.mode,
                }
                for team in self.registry.list_teams()
            ],
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "agents": list(skill.agents),
                    "default": skill.default,
                    "scripts": list(skill.scripts),
                }
                for skill in self.registry.skills.list()
            ],
        }

    def get_expert(self, name: str) -> ExpertConfig | None:
        return self.registry.get_expert(name)

    def get_team(self, name: str) -> TeamConfig | None:
        return self.registry.get_team(name)

    # -- 会话记录 ---------------------------------------------------------

    def list_sessions(self) -> list[dict[str, Any]]:
        """会话列表，最近更新的排在前面。"""

        return self.sessions.summaries()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """某个会话的完整对话记录（含逐轮内容）。"""

        session = self.sessions.get(session_id)
        return session.to_dict() if session is not None else None

    def _record_event(self, session_id: str, event: StreamEvent) -> None:
        """把专家 / 专家团的产出写进会话记录。"""

        if event.type == EventType.EXPERT_COMPLETED:
            self.sessions.append(
                session_id,
                "expert",
                str(event.payload.get("text") or ""),
                label=str(event.expert or ""),
            )
        elif event.type == EventType.TEAM_COMPLETED:
            self.sessions.append(
                session_id,
                "team",
                str(event.payload.get("text") or ""),
                label=str(event.team or ""),
            )

    # -- 执行 -------------------------------------------------------------

    def _emit(self, event: StreamEvent) -> StreamEvent:
        """广播事件给追踪器（可观测性接入点）。"""

        self.event_bus.emit(event)
        return event

    @staticmethod
    def _merge_config(config: dict[str, Any] | None, thread_id: str) -> dict[str, Any]:
        """把调用方的 RunnableConfig 与本次执行的 thread_id 合并。"""

        merged: dict[str, Any] = dict(config or {})
        configurable = dict(merged.get("configurable") or {})
        configurable["thread_id"] = thread_id
        merged["configurable"] = configurable
        return merged

    def _orchestrator_graph(self) -> Any:
        if self._orchestrator is None:
            from .llm import build_chat_model

            self._orchestrator = build_orchestrator(
                self.registry,
                self.factory,
                llm=build_chat_model(self.settings),
                checkpointer=self.checkpointer,
            )
        return self._orchestrator

    async def stream_run(
        self,
        task: str,
        *,
        session_id: str | None = None,
        expert: str | None = None,
        team: str | None = None,
        mode: str | None = None,
        context: list[str] | None = None,
        approval_callback: ApprovalCallback | None = None,
        config: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式执行一次任务。

        * ``expert`` / ``team`` 指定时跳过路由，直接交给目标。
        * 命中人工审批时暂停，等待 ``approval_callback`` 的决定后从断点恢复。
        * ``config`` 用于透传调用方的 ``RunnableConfig``（callbacks / tags / metadata）。
        """

        sid = session_id or uuid.uuid4().hex[:12]
        scope = ThreadScope(session_id=sid)

        yield self._emit(
            StreamEvent(
                type=EventType.RUN_STARTED,
                payload={"task": task, "session_id": sid},
            )
        )

        if expert:
            graph = self.factory.get_by_name(expert)
            route, target = "expert", expert
            current_team = ""
            thread_id = scope.expert_thread(expert)
            payload: dict[str, Any] = {
                "task": task,
                "context": list(context or []),
            }
        elif team:
            graph = self.factory.get_team_by_name(team, mode=mode)
            route, target = "team", team
            current_team = team
            thread_id = scope.team_thread(team)
            payload = {
                "task": task,
                "context": list(context or []),
                "session_id": sid,
            }
        else:
            graph = self._orchestrator_graph()
            route, target = "", ""
            current_team = ""
            thread_id = scope.session_thread()
            payload = {
                "task": task,
                "context": list(context or []),
                "session_id": sid,
            }

        if graph is None:
            yield self._emit(
                StreamEvent(
                    type=EventType.ERROR,
                    payload={"message": f"未找到目标: {expert or team}"},
                )
            )
            return

        # 记下这一轮，等会儿可以在侧栏「最近」里翻出来
        self.sessions.begin(sid, task, expert=expert or "", team=team or "")

        run_config = self._merge_config(config, thread_id)

        inputs: Any = payload
        while True:
            interrupted = False
            async for chunk in graph.astream(
                inputs,
                run_config,
                stream_mode=["custom", "updates"],
                subgraphs=True,
            ):
                namespace, mode_name, data = parse_stream_chunk(chunk)

                if mode_name == "custom" and isinstance(data, dict):
                    ns_expert, ns_team = scope_from_namespace(namespace)
                    event = StreamEvent(
                        type=str(data.get("type", "unknown")),
                        expert=data.get("expert") or ns_expert,
                        # 专家团作为顶层目标运行时，命名空间里只有成员节点，
                        # 团队维度由当前执行目标补齐
                        team=data.get("team") or ns_team or current_team,
                        payload=dict(data.get("payload") or {}),
                    )
                    self._record_event(sid, event)
                    if event.type == EventType.ORCHESTRATOR_ROUTE:
                        # 记录路由结果，最终事件里要带上真正命中的目标
                        route = str(event.payload.get("route") or route)
                        target = str(event.payload.get("target") or target)
                        if route == "team":
                            current_team = target
                    yield self._emit(event)
                    continue

                interrupts = extract_interrupts(data)
                if interrupts:
                    interrupted = True
                    for item in interrupts:
                        value = getattr(item, "value", None)
                        if isinstance(value, dict) and value.get("kind") in (
                            "tool_approval",
                            "batch_approval",
                        ):
                            decision = await self._handle_interrupt(
                                value,
                                expert=target,
                                thread_id=run_config["configurable"]["thread_id"],
                                approval_callback=approval_callback,
                            )
                            inputs = Command(resume=decision)
                        else:
                            # interrupt_before 的节点级闸门：整批统一放行
                            await self._handle_static_gate(
                                graph,
                                run_config,
                                expert=target,
                                approval_callback=approval_callback,
                            )
                            inputs = None
                    break

            if interrupted:
                continue

            # 流结束：判断是不是被 interrupt_before 挂起了（这种暂停不产生 __interrupt__ 分片）
            state = await graph.aget_state(run_config)
            if getattr(state, "next", None) and not getattr(state, "interrupts", None):
                await self._handle_static_gate(
                    graph,
                    run_config,
                    expert=target,
                    approval_callback=approval_callback,
                )
                inputs = None
                continue

            break

        final_state = await graph.aget_state(run_config)
        values = final_state.values or {}
        record = values.get("record") or {}
        payload: dict[str, Any] = {
            "route": route,
            "target": target,
            "status": values.get("status", "ok"),
            "text": values.get("report") or values.get("summary", ""),
        }
        if not payload["text"] and record:
            payload["text"] = "\n\n".join(
                f"### {name}\n{output.get('summary', '')}"
                for name, output in record.items()
                if isinstance(output, dict)
            )
        if record:
            payload["deliverables"] = {
                name: {
                    "status": output.get("status"),
                    "summary": output.get("summary"),
                    "findings": list(output.get("findings") or []),
                    "turns_used": output.get("turns_used"),
                }
                for name, output in record.items()
                if isinstance(output, dict)
            }
        artifacts: list[Any] = []
        for output in record.values():
            if isinstance(output, dict):
                artifacts.extend(output.get("artifacts") or [])
        if artifacts:
            payload["artifacts"] = [
                item.to_dict() if hasattr(item, "to_dict") else item for item in artifacts
            ]
        yield self._emit(StreamEvent(type=EventType.RUN_COMPLETED, payload=payload))

    async def _handle_interrupt(
        self,
        value: Any,
        *,
        expert: str,
        thread_id: str,
        approval_callback: ApprovalCallback | None,
    ) -> dict[str, Any]:
        """处理一次中断：拿决定，然后从断点恢复。"""

        payload = value if isinstance(value, dict) else {}
        kind = str(payload.get("kind") or "")
        if kind not in ("tool_approval", "batch_approval"):
            # 非工具审批（例如 interrupt_before 的节点级闸门）：默认放行
            return {"approved": True, "reason": "非工具类中断，默认放行"}

        request = self.approval_center.get(str(payload.get("request_id") or ""))
        if request is None:
            request = self.approval_center.create(
                expert=str(payload.get("expert") or expert),
                tool=str(payload.get("tool") or ""),
                args=dict(payload.get("args") or {}),
                reason=str(payload.get("reason") or ""),
                risk=str(payload.get("risk") or "high"),
                thread_id=thread_id,
                request_id=str(payload.get("request_id") or "") or None,
            )

        if approval_callback is None:
            return {"approved": False, "reason": "没有可用的审批通道，默认拒绝"}

        approved, note = await approval_callback(request)
        self.approval_center.resolve(request.id, approved=approved, note=note)
        return {"approved": approved, "reason": note}

    async def _handle_static_gate(
        self,
        graph: Any,
        config: dict[str, Any],
        *,
        expert: str,
        approval_callback: ApprovalCallback | None,
    ) -> None:
        """处理 static 模式（interrupt_before）的整批审批。

        节点在恢复后会去审批中心查结论，所以这里必须先把结论登记好。
        """

        from .graphs.expert import static_batch_request_id

        state = await graph.aget_state(config)
        pending = list((state.values or {}).get("pending_calls") or [])
        if not pending:
            return

        thread_id = (config.get("configurable") or {}).get("thread_id", "")
        request = self.approval_center.create(
            expert=expert,
            tool=",".join(sorted({str(call.get("name", "")) for call in pending})),
            args={"calls": pending},
            reason="整批工具调用执行前统一放行",
            risk="high",
            thread_id=thread_id,
            request_id=static_batch_request_id(thread_id, expert, pending),
        )

        if approval_callback is None:
            approved, note = False, "没有可用的审批通道，默认拒绝"
        else:
            approved, note = await approval_callback(request)
        self.approval_center.resolve(request.id, approved=approved, note=note)

    async def run(
        self,
        task: str,
        *,
        session_id: str | None = None,
        expert: str | None = None,
        team: str | None = None,
        mode: str | None = None,
        context: list[str] | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> RunResult:
        """跑完一次任务，返回最终结果。"""

        events: list[StreamEvent] = []
        final_event: StreamEvent | None = None
        async for event in self.stream_run(
            task,
            session_id=session_id,
            expert=expert,
            team=team,
            mode=mode,
            context=context,
            approval_callback=approval_callback,
        ):
            events.append(event)
            if event.type == EventType.RUN_COMPLETED:
                final_event = event

        error_event = next((e for e in events if e.type == EventType.ERROR), None)
        if final_event is not None:
            payload = final_event.payload
            status = str(payload.get("status", "ok"))
            summary = str(payload.get("text", ""))
            route = str(payload.get("route", ""))
            target = str(payload.get("target", ""))
        elif error_event is not None:
            # 没有走到收尾（目标不存在 / 执行出错）时不能报"成功"
            payload = {}
            status = "failed"
            summary = str(error_event.payload.get("message", "执行失败"))
            route = ""
            target = ""
        else:
            payload = {}
            status = "failed"
            summary = "（本次没有产出）"
            route = ""
            target = ""

        result = RunResult(
            summary=summary,
            status=status,
            route=route,
            target=target,
            result={
                key: payload[key]
                for key in ("deliverables", "artifacts")
                if key in payload
            },
            events=events,
        )
        for event in events:
            if event.type == EventType.ORCHESTRATOR_ROUTE:
                result.route = str(event.payload.get("route", result.route))
                result.target = str(event.payload.get("target", result.target))
                result.reasoning = str(event.payload.get("reason", ""))
        return result

    # -- 热更新 -----------------------------------------------------------

    def reload(self) -> Any:
        """热重载专家包。"""

        report = self.factory.reload()
        # 专家 / 专家团集合可能变了，主编排器图需要重建
        self._orchestrator = None
        return report

    def close(self) -> None:
        """释放资源（关闭 SQLite 连接等）。"""

        saver = self.checkpointer
        closer = getattr(saver, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # pragma: no cover
                pass

    async def aclose(self) -> None:
        """异步释放资源。"""

        saver = self.checkpointer
        closer = getattr(saver, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # pragma: no cover
                pass
        else:
            self.close()

    async def __aenter__(self) -> CodeAssistant:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
