"""专家团共享部件。

两种协作模式（主理人调度 / 并行汇）共用同一套状态协议与成员调用逻辑，
保证「成员」在任何模式下都表现为同一个东西：一个独立编译的专家子图。

**成员是原生子图节点**（``builder.add_node("member__x", expert_graph)``），
而不是在节点里手工 ``ainvoke``——这一点很关键：

* 只有原生子图节点，人工审批的中断才能从成员专家**向上冒泡**到专家团，
  再冒泡到主编排器，最后到达前端；
* 手工 ``ainvoke`` 时子图的挂起会被静默吞掉，父图会误以为成员已经完成。

状态通道约定：

* ``record`` —— 各成员产出的汇总通道（``{成员名: 专家输出}``），带合并 reducer，
  多个成员并行写回时互不覆盖；
* ``report`` —— 专家团最终汇编出来的交付报告。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage

from ..memory import ThreadScope


def merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """字典合并 reducer（成员并行产出时使用）。"""

    merged = dict(left or {})
    merged.update(right or {})
    return merged


def concat_lists(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """列表拼接 reducer（协作记录按顺序累积）。"""

    return list(left or []) + list(right or [])


class TeamState(TypedDict, total=False):
    """专家团子图的状态 Schema。"""

    task: str
    context: list[str]
    base_context: list[str]
    focus: str
    record: Annotated[dict[str, Any], merge_dicts]
    next_member: str
    round: int
    status: str
    report: str
    team: str
    mode: str
    session_id: str


class TeamInputSchema(TypedDict, total=False):
    """专家团从父图接受的输入。"""

    task: str
    context: list[str]
    focus: str
    session_id: str


class TeamOutputSchema(TypedDict, total=False):
    """专家团写回父图的内容。"""

    record: dict[str, Any]
    report: str
    status: str


def scope_from_state(state: TeamState) -> ThreadScope:
    """从状态里取出会话作用域。

    专家团图会被缓存复用，会话 ID 必须来自**运行时状态**而不是构造参数，
    否则不同会话的成员记忆会串到一起。
    """

    return ThreadScope(session_id=str(state.get("session_id") or ""))


def build_member_context(record: dict[str, Any] | None) -> list[str]:
    """把已完成成员的产出汇总成给下一位成员的上下文。

    跨成员的信息流一律经由主理人中转——这里只暴露「已完成的产出摘要」。
    """

    context: list[str] = []
    for name, output in (record or {}).items():
        if not isinstance(output, dict):
            continue
        summary = str(output.get("summary") or "").strip()
        if summary:
            context.append(f"[{name}] {summary}")
    return context


def summarize_deliverables(record: dict[str, Any] | None) -> str:
    """把所有成员的产出拼成一段文本（供最终汇编使用）。"""

    sections: list[str] = []
    for name, output in (record or {}).items():
        if not isinstance(output, dict):
            continue
        summary = str(output.get("summary") or "").strip()
        findings = output.get("findings") or []
        block = f"### {name}\n{summary or '（无产出）'}"
        if findings:
            block += "\n遗留问题：\n" + "\n".join(f"- {item}" for item in findings)
        sections.append(block)
    return "\n\n".join(sections)


def member_focus(member_name: str, profession: str = "") -> str:
    """成员默认的聚焦说明（并行模式下由 dispatch 节点生成）。"""

    return f"你负责「{profession or member_name}」这一部分，请从该职责视角完成任务。"


def message_text(messages: list[AnyMessage] | None) -> str:
    """把消息列表压成纯文本。"""

    return "\n".join(str(message.content) for message in (messages or []))
