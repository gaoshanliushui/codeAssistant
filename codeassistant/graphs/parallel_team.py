"""Map-Reduce 专家团：并行汇。

所有成员同时接到同一个任务，各自独立执行（``Send`` API 扇出），
最后由 reduce 节点汇总。对应专家包 SOP 里的「并行开发」阶段。

.. code-block:: text

    START → dispatch ─(Send 扇出)→ member_x / member_y / … → reduce → END

每个成员同样是**原生子图节点**，所以成员内部的人工审批能冒泡到专家团这一层；
并行场景下多个成员同时挂起时，用「中断 ID → 审批结论」的映射一次性恢复。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..configs import ExpertConfig, TeamConfig
from ..contracts import merge_team_outputs
from ..observability import EventType, get_writer
from .team_common import (
    TeamInputSchema,
    TeamOutputSchema,
    TeamState,
    member_focus,
    summarize_deliverables,
)


class ParallelTeam:
    """并行汇模式的专家团。"""

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

    # -- 节点 -------------------------------------------------------------

    def dispatch(self, state: TeamState) -> dict[str, Any]:
        """准备扇出所需的上下文。"""

        writer = get_writer()
        if writer is not None:
            writer(
                {
                    "type": EventType.TEAM_ROUND,
                    "team": self.team.name,
                    "payload": {
                        "round": 1,
                        "members": [member.name for member in self.members],
                    },
                }
            )
        return {"round": 1, "team": self.team.name, "mode": "parallel"}

    def fan_out(self, state: TeamState) -> list[Send]:
        """把任务分发给所有成员（并行）。

        每个成员拿到自己的 ``focus``（职责说明），因此即使共用同一个任务，
        各自的产出视角也不同。
        """

        task = state.get("task", "")
        context = list(state.get("context") or [])
        sends: list[Send] = []
        for member in self.members:
            member_context = [
                *context,
                member_focus(member.name, member.profession),
            ]
            sends.append(
                Send(
                    f"member__{member.name}",
                    {
                        "task": task,
                        "context": member_context,
                        "focus": member_focus(member.name, member.profession),
                        "round": state.get("round", 1),
                        "team": self.team.name,
                        "mode": "parallel",
                        "session_id": state.get("session_id", ""),
                    },
                )
            )
        return sends

    async def reduce(self, state: TeamState) -> dict[str, Any]:
        """汇总所有成员的并行产出。"""

        writer = get_writer()
        record = state.get("record") or {}
        merged = merge_team_outputs(self.team.name, record, None)

        try:
            reply = await self.llm.ainvoke(
                [
                    SystemMessage(
                        f"你是{self.team.display_name or self.team.name}的汇编者，"
                        "负责把成员的并行产出合并成一份结论。"
                    ),
                    HumanMessage(
                        content=(
                            "成员产出如下：\n\n"
                            f"{summarize_deliverables(record)}\n\n"
                            "请合并这些产出，指出共识、分歧与需要人工决策的地方。"
                        )
                    ),
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

    def compile(self, checkpointer: BaseCheckpointSaver | None = None) -> Any:
        builder: StateGraph = StateGraph(
            TeamState,
            input_schema=TeamInputSchema,
            output_schema=TeamOutputSchema,
        )
        builder.add_node("dispatch", self.dispatch)
        builder.add_node("reduce", self.reduce)

        mapping: dict[str, str] = {}
        for member in self.members:
            node_name = f"member__{member.name}"
            # 成员专家子图直接作为节点：中断可以向上冒泡
            builder.add_node(node_name, self.factory.get(member))
            mapping[node_name] = node_name

        builder.add_edge(START, "dispatch")
        builder.add_conditional_edges("dispatch", self.fan_out, mapping)
        for member in self.members:
            builder.add_edge(f"member__{member.name}", "reduce")
        builder.add_edge("reduce", END)

        return builder.compile(checkpointer=checkpointer)


def build_parallel_team(
    team: TeamConfig,
    members: list[ExpertConfig],
    *,
    llm: BaseChatModel,
    factory: Any,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """构建并行汇专家团图。"""

    parallel = ParallelTeam(team, members, llm=llm, factory=factory)
    return parallel.compile(checkpointer=checkpointer)
