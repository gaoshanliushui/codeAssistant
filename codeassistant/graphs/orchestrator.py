"""主编排器（Supervisor / Orchestrator）。

主编排器只做两件事：**意图路由**与**结果汇总**，不亲自产出专业内容。

路由结果有三种：

* ``expert`` —— 交给某个单体专家
* ``team``   —— 交给某个专家团（可指定协作模式）
* ``direct`` —— 交给内置代码助手（没有更合适的专家时）

每个专家 / 专家团都是**原生子图节点**，因此它们内部的人工审批能一路冒泡到前端。

路由优先用 LLM 判断（带工具调用），模型不可用时退回关键词规则，
保证在离线 / 弱模型环境下依然可用。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from ..contracts import merge_team_outputs
from ..observability import EventType, get_writer
from ..registry import ExpertRegistry
from .team_common import summarize_deliverables

#: 命中这些词说明是编码类任务，兜底交给内置代码助手
CODE_KEYWORDS = (
    "代码", "函数", "类", "方法", "重构", "报错", "异常", "缺陷", "编译", "脚本",
    "接口", "数据库", "sql", "api", "仓库", "项目结构", "依赖", "单元测试",
    "bug", "crash", "stacktrace", "traceback", "refactor", "lint", "git",
    "python", "java", "golang", "rust", "typescript", "javascript", "react", "vue",
    "spring", "django", "flask", "fastapi", "docker", "kubernetes", "nginx",
)

#: 直接交给内置代码助手的路由名
DIRECT = "direct"

ROUTE_SYSTEM_PROMPT = """你是多专家系统的调度员。

根据用户任务，从下面的候选中挑选最合适的一个，并说明理由。

{options}

可选动作：
- 调用 select_target 工具，target 填候选名；
- 如果没有任何候选合适，target 填 direct（由通用代码助手直接处理）。
"""


class OrchestratorState(TypedDict, total=False):
    """主编排器的状态 Schema。"""

    task: str
    context: list[str]
    focus: str
    record: dict[str, Any]
    report: str
    route: str
    target: str
    status: str
    reasoning: str
    session_id: str


def _make_select_tool() -> StructuredTool:
    def select_target(target: str, reason: str = "") -> str:
        """选择处理该任务的专家或专家团。"""

        return f"已选择 {target}：{reason}"

    return StructuredTool.from_function(
        func=select_target,
        name="select_target",
        description="选择处理当前任务的专家、专家团，或 direct（通用代码助手）。",
    )


class Orchestrator:
    """意图路由 + 结果汇总。"""

    def __init__(
        self,
        registry: ExpertRegistry,
        factory: Any,
        *,
        llm: BaseChatModel,
    ) -> None:
        self.registry = registry
        self.factory = factory
        self.llm = llm

    # -- 候选 -------------------------------------------------------------

    def _options(self) -> str:
        lines: list[str] = []
        for team in self.registry.list_teams():
            members = "、".join(team.members)
            lines.append(
                f"- [team] {team.name}（{team.display_name}）：{team.description} 成员：{members}"
            )
        for expert in self.registry.list_experts():
            if expert.name == "code-assistant":
                continue
            lines.append(
                f"- [expert] {expert.name}（{expert.display_name}）：{expert.description}"
            )
        lines.append(
            "- [direct] code-assistant：通用代码助手，处理检索、阅读、修改、解释代码的任务"
        )
        return "\n".join(lines)

    def _fallback_route(self, task: str) -> tuple[str, str, str]:
        """关键词兜底路由。"""

        for team in self.registry.list_teams():
            if team.name in task or (team.display_name and team.display_name in task):
                return "team", team.name, f"任务中点名了专家团 {team.display_name or team.name}"
        for expert in self.registry.list_experts():
            if expert.name == "code-assistant":
                continue
            if expert.name in task:
                return "expert", expert.name, f"任务中点名了专家 {expert.display_name or expert.name}"
        lowered = task.lower()
        if any(keyword in lowered for keyword in CODE_KEYWORDS):
            return DIRECT, "code-assistant", "任务是编码类，交给通用代码助手"
        return DIRECT, "code-assistant", "没有更匹配的专家，使用通用代码助手"

    # -- 节点 -------------------------------------------------------------

    async def route(self, state: OrchestratorState) -> dict[str, Any]:
        """判断该交给谁。"""

        writer = get_writer()
        task = state.get("task", "")
        route = ""
        target = ""
        reasoning = ""

        try:
            reply = await self.llm.bind_tools([_make_select_tool()]).ainvoke(
                [
                    SystemMessage(ROUTE_SYSTEM_PROMPT.format(options=self._options())),
                    HumanMessage(content=f"用户任务：{task}"),
                ]
            )
            for call in getattr(reply, "tool_calls", []) or []:
                if call.get("name") != "select_target":
                    continue
                args = call.get("args") or {}
                candidate = str(args.get("target") or "").strip()
                reasoning = str(args.get("reason") or "")
                if candidate in self.registry.teams:
                    route, target = "team", candidate
                elif candidate in self.registry.experts:
                    route, target = "expert", candidate
                elif candidate == DIRECT:
                    route, target = DIRECT, "code-assistant"
                break
        except Exception:  # pragma: no cover - 依赖外部服务
            route, target, reasoning = "", "", ""

        if not route:
            route, target, reasoning = self._fallback_route(task)

        if writer is not None:
            writer(
                {
                    "type": EventType.ORCHESTRATOR_ROUTE,
                    "payload": {"route": route, "target": target, "reason": reasoning},
                }
            )
        return {
            "route": route,
            "target": target,
            "focus": reasoning,
            "reasoning": reasoning,
        }

    async def respond(self, state: OrchestratorState) -> dict[str, Any]:
        """汇总最终答复。"""

        record = state.get("record") or {}
        route = state.get("route", DIRECT)

        merged = merge_team_outputs(state.get("target", ""), record, [])
        status = merged.get("status", "ok")

        if route == "team":
            summary = state.get("report", "") or summarize_deliverables(record)
        elif record:
            summary = summarize_deliverables(record)
        else:
            summary = state.get("report", "")

        if not summary:
            summary = "（本次没有产出文字结论）"

        return {"report": summary, "status": status}

    # -- 组图 -------------------------------------------------------------

    def compile(self, checkpointer: BaseCheckpointSaver | None = None) -> Any:
        builder: StateGraph = StateGraph(OrchestratorState)
        builder.add_node("route", self.route)
        builder.add_node("respond", self.respond)

        mapping: dict[str, str] = {}
        node_of: dict[str, str] = {}
        for expert in self.registry.list_experts():
            node_name = f"expert__{expert.name}"
            builder.add_node(node_name, self.factory.get(expert))
            mapping[node_name] = node_name
            node_of[expert.name] = node_name
        for team in self.registry.list_teams():
            node_name = f"team__{team.name}"
            builder.add_node(node_name, self.factory.get_team(team))
            mapping[node_name] = node_name
            node_of[team.name] = node_name

        def route_to_node(state: OrchestratorState) -> str:
            target = state.get("target", "")
            return node_of.get(target, node_of.get("code-assistant", "respond"))

        builder.add_edge(START, "route")
        builder.add_conditional_edges("route", route_to_node, mapping)
        for node_name in mapping.values():
            builder.add_edge(node_name, "respond")
        builder.add_edge("respond", END)

        return builder.compile(checkpointer=checkpointer)


def build_orchestrator(
    registry: ExpertRegistry,
    factory: Any,
    *,
    llm: BaseChatModel,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """构建主编排器图。"""

    orchestrator = Orchestrator(registry, factory, llm=llm)
    return orchestrator.compile(checkpointer=checkpointer)


__all__ = [
    "CODE_KEYWORDS",
    "DIRECT",
    "Orchestrator",
    "OrchestratorState",
    "build_orchestrator",
]
