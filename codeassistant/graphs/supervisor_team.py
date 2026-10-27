"""Supervisor 专家团：主理人动态分派。

协作模型与专家包里描述的铁律一致：

1. 主理人（lead）拆任务、派活、盯进度、控风险
2. 成员产出**必须**回传主理人，再由主理人转交下一位成员
   （跨成员信息流一律经由主理人中转，成员之间不直连）
3. 主理人最终汇编交付报告

图的形状（成员是运行时动态生成的）：

.. code-block:: text

    START → lead ─┬─(派活给 member_x)→ member_x → lead → …
                  └─(收尾)→ finalize → END

每个 ``member_x`` 都是**成员专家子图本身**（原生子图节点），
因此成员内部的人工审批能正确冒泡到专家团这一层。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from ..configs import ExpertConfig, TeamConfig
from ..contracts import merge_team_outputs
from ..observability import EventType, get_writer
from .team_common import (
    TeamInputSchema,
    TeamOutputSchema,
    TeamState,
    build_member_context,
    summarize_deliverables,
)

FINISH = "__finish__"

LEAD_SYSTEM_PROMPT = """你是专家团的主理人（交付总监）。

你的职责：
1. 拆解任务，判断下一步该交给哪位成员；
2. 给成员下达清晰、聚焦的子任务（说明交付什么、什么标准算完成）；
3. 审阅成员回传的产出，必要时要求补做或转交给另一位成员；
4. 成员全部完成后，把产出整合成**用户要的交付物本身**（文章、方案、代码、报告本体）。

铁律：
- 所有成员产出都先回传给你，由你决定转交给谁，成员之间不直连；
- 每次只派活给一位成员，等其回传后再决定下一步；
- 成员产出已足够时，调用 dispatch 工具把 member 设为 done 收尾。

可用成员：{members}
"""


def _make_dispatch_tool(member_names: list[str]) -> StructuredTool:
    member_list = ", ".join(member_names + [FINISH])

    def dispatch(member: str, reason: str = "") -> str:
        """派活给某位成员，或在 member 传 done 时收尾汇编。"""

        return f"已派活给 {member}：{reason}"

    return StructuredTool.from_function(
        func=dispatch,
        name="dispatch",
        description=(
            "把当前子任务派给某位成员执行。可选成员："
            f"{member_list}。所有工作完成后把 member 设为 {FINISH}。"
        ),
    )


def _parse_dispatch(reply: Any, member_names: list[str]) -> tuple[str, str]:
    """从模型回复里解析出「下一位成员 + 派活说明」。"""

    tool_calls = list(getattr(reply, "tool_calls", []) or [])
    for call in tool_calls:
        if call.get("name") != "dispatch":
            continue
        args = call.get("args") or {}
        member = str(args.get("member") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if member == FINISH or member.lower() in ("done", "finish", "end"):
            return FINISH, reason
        if member in member_names:
            return member, reason
    return "", ""


class SupervisorTeam:
    """Supervisor 模式的专家团。"""

    def __init__(
        self,
        team: TeamConfig,
        members: list[ExpertConfig],
        *,
        llm: BaseChatModel,
        factory: Any,
    ) -> None:
        self.team = team
        self.members = members
        self.llm = llm
        self.factory = factory
        self._member_names = [member.name for member in members]
        self._member_map = {member.name: member for member in members}

    # -- 节点 -------------------------------------------------------------

    async def lead(self, state: TeamState) -> dict[str, Any]:
        """主理人决策：派活给谁，还是收尾。"""

        writer = get_writer()
        round_index = state.get("round", 0)
        record = state.get("record") or {}
        remaining = [name for name in self._member_names if name not in record]

        prompt = HumanMessage(
            content=(
                f"任务：{state.get('task', '')}\n"
                f"已完成产出：\n{summarize_deliverables(record) or '（暂无）'}\n"
                f"尚未出活的成员：{', '.join(remaining) or '（无）'}\n"
                f"这是第 {round_index + 1} 轮（共最多 {self.team.max_rounds} 轮）。"
                "请派活给下一位成员，或在产出已充分时收尾。"
            )
        )

        next_member = ""
        reason = ""
        try:
            # 只把「还没出活」的成员交给主理人挑选，避免重复派活
            bound = self.llm.bind_tools([_make_dispatch_tool(remaining or self._member_names)])
            reply = await bound.ainvoke(
                [
                    SystemMessage(
                        LEAD_SYSTEM_PROMPT.format(members=", ".join(self._member_names))
                    ),
                    prompt,
                ]
            )
            next_member, reason = _parse_dispatch(reply, remaining or self._member_names)
        except Exception:  # pragma: no cover - 依赖外部服务
            next_member, reason = "", ""

        if not next_member or next_member in record:
            next_member, reason = self._fallback_dispatch(record, remaining)

        if writer is not None:
            writer(
                {
                    "type": EventType.TEAM_DISPATCH,
                    "team": self.team.name,
                    "payload": {
                        "member": next_member,
                        "reason": reason,
                        "round": round_index + 1,
                    },
                }
            )

        # 主理人把已完成成员的产出整理成下一位成员的上下文（跨成员信息流经主理人中转）
        # 调用方传入的原始上下文只在第一轮读取一次并单独留存，避免多轮重复叠加
        base_context = state.get("base_context")
        if base_context is None:
            base_context = [item for item in (state.get("context") or []) if item]
        member_context = [
            *base_context,
            *build_member_context(record),
        ]
        return {
            "next_member": next_member,
            "focus": reason,
            "context": member_context,
            "base_context": base_context,
            "round": round_index + 1,
            "team": self.team.name,
            "mode": "supervisor",
        }

    def _fallback_dispatch(
        self, record: dict[str, Any], remaining: list[str]
    ) -> tuple[str, str]:
        """模型没有给出有效派活时的兜底：按顺序派给还没出活的成员。"""

        if remaining:
            member = remaining[0]
            profession = self._member_map[member].profession or member
            return member, f"请负责「{self.team.name}」任务中与「{profession}」相关的部分。"
        return FINISH, "所有成员均已产出，准备汇编。"

    async def finalize(self, state: TeamState) -> dict[str, Any]:
        """主理人把成员产出整合成**用户要的交付物**。"""

        writer = get_writer()
        record = state.get("record") or {}
        merged = merge_team_outputs(self.team.name, record, None)
        task = state.get("task", "")

        prompt = HumanMessage(
            content=(
                f"用户任务：\n{task or '（未提供）'}\n\n"
                "各位成员的产出如下：\n\n"
                f"{summarize_deliverables(record)}\n\n"
                "请直接完成用户的任务：把成员产出整合成用户要的**交付物本身**"
                "（文章、方案、代码、报告本体等），不要汇报团队协作过程、"
                "成员分工或下一步计划。"
            )
        )
        try:
            reply = await self.llm.ainvoke(
                [
                    SystemMessage(
                        f"你是{self.team.display_name or self.team.name}的主理人。"
                        "所有成员产出都经过你汇总，请直接交付用户要的成果本身。"
                    ),
                    prompt,
                ]
            )
            report = str(reply.content).strip()
        except Exception:  # pragma: no cover
            report = merged.get("summary", "")

        if writer is not None:
            writer(
                {
                    "type": EventType.TEAM_COMPLETED,
                    "team": self.team.name,
                    "payload": {"text": report, "status": merged.get("status")},
                }
            )
        return {
            "report": report or merged.get("summary", ""),
            "status": merged.get("status", "ok"),
        }

    # -- 组图 -------------------------------------------------------------

    def route_from_lead(self, state: TeamState) -> str:
        next_member = state.get("next_member", "")
        round_index = state.get("round", 0)
        if next_member and next_member != FINISH and next_member in self._member_map:
            if round_index > self.team.max_rounds:
                return "finalize"
            return f"member__{next_member}"
        return "finalize"

    def compile(self, checkpointer: BaseCheckpointSaver | None = None) -> Any:
        builder: StateGraph = StateGraph(
            TeamState,
            input_schema=TeamInputSchema,
            output_schema=TeamOutputSchema,
        )
        builder.add_node("lead", self.lead)
        builder.add_node("finalize", self.finalize)

        mapping: dict[str, str] = {"finalize": "finalize"}
        for member in self.members:
            node_name = f"member__{member.name}"
            # 成员专家子图直接作为节点：中断可以向上冒泡
            builder.add_node(node_name, self.factory.get(member))
            mapping[node_name] = node_name

        builder.add_edge(START, "lead")
        builder.add_conditional_edges("lead", self.route_from_lead, mapping)
        for member in self.members:
            builder.add_edge(f"member__{member.name}", "lead")
        builder.add_edge("finalize", END)

        return builder.compile(checkpointer=checkpointer)


def build_supervisor_team(
    team: TeamConfig,
    members: list[ExpertConfig],
    *,
    llm: BaseChatModel,
    factory: Any,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """构建 Supervisor 专家团图。"""

    supervisor = SupervisorTeam(team, members, llm=llm, factory=factory)
    return supervisor.compile(checkpointer=checkpointer)
