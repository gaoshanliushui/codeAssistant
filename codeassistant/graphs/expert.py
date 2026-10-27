"""专家子图。

每个专家被封装成**独立编译**的 LangGraph 子图，拥有：

* 独立的 State Schema（:class:`ExpertState`）
* 独立的 Checkpointer（由图工厂注入）
* 独立的中断逻辑（人审点）

子图内部是一条手写的 ReAct 循环：

.. code-block:: text

    START → plan → act ─┬─(有工具调用)→ execute_tools → act
                        └─(无工具调用)→ finalize → END

因此专家子图可以脱离主编排器单独编译、单独测试。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from ..approval import ApprovalCenter, ApprovalRequest, parse_resume_value
from ..configs import ExpertConfig, HitlPolicy
from ..contracts import Artifact, ExpertOutput
from ..observability import EventType, get_writer
from ..skills import Skill, SkillRegistry
from ..tools.registry import TOOL_SPECS, ToolRegistry
from ..tools.skill_tools import build_skill_tools

#: 工具执行失败时的兜底提示
TOOL_ERROR_PREFIX = "工具执行失败："

#: 人工拒绝时的提示前缀
REJECTED_PREFIX = "该操作已被人工审批拒绝"


class ExpertState(TypedDict, total=False):
    """专家子图的独立状态 Schema。"""

    task: str
    context: list[str]
    focus: str
    messages: Annotated[list[AnyMessage], add_messages]
    #: 产出物用**普通 dict** 存储——自定义 dataclass 存进检查点
    #: 会在新版 LangGraph 里被 msgpack 严格模式拦下
    artifacts: list[dict[str, Any]]
    findings: list[str]
    pending_calls: list[dict[str, Any]]
    plan: str
    iteration: int
    status: str
    summary: str
    next_action: str
    expert: str
    turns_used: int
    #: 写回父图的唯一通道：``{专家名: 专家输出}``
    record: dict[str, Any]


class ExpertInputSchema(TypedDict, total=False):
    """专家子图接受的输入（父图传进来的部分）。"""

    task: str
    context: list[str]
    focus: str


class ExpertOutputSchema(TypedDict, total=False):
    """专家子图写回父图的内容。

    只暴露 ``record`` 一个通道——这样多个专家（专家团成员）并行写回时
    不会互相覆盖 ``task`` / ``summary`` 等共享字段。
    """

    record: dict[str, Any]


def _stable_id(*parts: str) -> str:
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def static_batch_request_id(thread_id: str, expert: str, pending: list[dict[str, Any]]) -> str:
    """static 模式下整批审批请求的确定性 ID。

    前端与专家节点用同一个函数算出同一个 ID，
    因此前端登记的审批结论能被节点准确取到。
    """

    call_ids = ",".join(sorted(str(call.get("id", "")) for call in pending))
    return _stable_id(thread_id, expert, call_ids)


def single_call_request_id(thread_id: str, expert: str, call: dict[str, Any]) -> str:
    """单个工具调用审批请求的确定性 ID。

    ID 里带上**工具名与参数**，否则不同 Provider 复用同一个 tool-call id 时，
    上一次的审批结论会被错误地套用到另一次调用上。
    """

    args_hash = hashlib.sha256(
        json.dumps(call.get("args") or {}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    return _stable_id(thread_id, expert, str(call.get("id", "")), str(call.get("name", "")), args_hash)


def lookup_decision(
    center: ApprovalCenter,
    thread_id: str,
    expert: str,
    call: dict[str, Any],
    pending: list[dict[str, Any]],
) -> ApprovalRequest | None:
    """查找前端**已经登记**的审批结论。

    前端可能提前决定（例如 ``interrupt_before`` 闸门统一放行），
    此时节点直接采用结论，不再重复打断用户。
    """

    for request_id in (
        single_call_request_id(thread_id, expert, call),
        static_batch_request_id(thread_id, expert, pending),
    ):
        request = center.get(request_id)
        if request is not None and request.state != "pending":
            return request
    return None


#: 技能调用的书写形式：``/技能名``
#: —— 必须是独立的一个词（前是空白或开头，后是空白或结尾），
#: 否则 ``/usr/local/bin`` 这样的路径会被误当成技能调用。
SKILL_COMMAND_PATTERN = re.compile(r"(?:(?<=\s)|^)/([\w\-]+)(?=\s|$)")


def match_skill_commands(task: str, skills: list[Skill]) -> list[Skill]:
    """挑出任务里 ``/技能名`` 形式显式调用的技能。

    只在**该专家绑定的技能**里匹配——别的专家的技能不该被顺手带进来。
    """

    if not task or not skills:
        return []
    matched: list[Skill] = []
    for token in SKILL_COMMAND_PATTERN.findall(task):
        for skill in skills:
            if skill.name == token and skill not in matched:
                matched.append(skill)
    return matched


def _build_task_prompt(state: ExpertState, skills: list[Skill] | None = None) -> str:
    """把任务 + 上下文 + 计划拼成用户消息。"""

    task = state.get("task", "")
    context = state.get("context") or []
    plan = state.get("plan", "")
    focus = state.get("focus", "")
    blocks = [f"任务：{task}"]
    if focus:
        blocks.append(f"本次聚焦：{focus}")
    if context:
        blocks.append("背景信息：\n" + "\n".join(f"- {item}" for item in context))
    # 用户用 ``/技能名`` 显式点了技能：直接把说明带上，
    # 省得模型再去判断「要不要载入这个技能」
    invoked = match_skill_commands(task, skills or [])
    if invoked:
        blocks.append(
            "已载入技能说明（按用户点选载入，请遵循其中的步骤）：\n"
            + "\n\n".join(skill.to_prompt() for skill in invoked)
        )
    if plan:
        blocks.append(f"执行计划：\n{plan}")
    blocks.append("请完成任务并给出结论。")
    return "\n\n".join(blocks)


def _fallback_message(exc: Exception, prefix: str = "") -> AIMessage:
    return AIMessage(content=f"{prefix}模型调用失败（{type(exc).__name__}: {exc}）。")


def _approval_required_tools(
    policy: HitlPolicy,
    selected_tools: list[BaseTool],
    declared_tools: tuple[str, ...],
) -> set[str]:
    """算出这个专家实际需要人工审批的工具集合。

    * 策略里显式列出的工具要审批；
    * **动态注入**的高危工具（例如技能自带的脚本执行器）一律要审批——
      它们不在专家声明的白名单里，风险等级是兜底安全网；
    * 专家自己声明的白名单工具则完全听策略的，配置可以收窄也可以放宽。
    """

    required = set(policy.tools)
    declared = set(declared_tools)
    for tool in selected_tools:
        spec = TOOL_SPECS.get(tool.name)
        if spec and spec.requires_approval and tool.name not in declared:
            required.add(tool.name)
    return required


def build_expert_graph(
    expert_config: ExpertConfig,
    *,
    llm: BaseChatModel,
    tool_registry: ToolRegistry,
    approval_center: ApprovalCenter | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    tools: list[BaseTool] | None = None,
    skills: list[Skill] | None = None,
) -> Any:
    """构建并编译单个专家子图。

    :param expert_config: 专家声明式配置
    :param llm: 聊天模型（OpenAI / Anthropic / Mock 均可）
    :param tool_registry: 工具注册表
    :param approval_center: 人审中心
    :param checkpointer: 该专家自己的检查点保存器
    :param tools: 覆盖默认工具白名单（测试用）
    :param skills: 该专家绑定的技能（提供技能工具 + 系统提示里的技能清单）
    """

    center = approval_center or ApprovalCenter()
    selected_tools = tools if tools is not None else tool_registry.for_expert(expert_config.tools)
    bound_skills = list(skills or [])
    if bound_skills:
        selected_tools = [
            *selected_tools,
            *build_skill_tools(bound_skills, tool_registry.sandbox),
        ]
    # 白名单在**执行期**同样生效：模型即使"幻觉"出白名单外的工具名也调不动
    allowed_tools = {tool.name: tool for tool in selected_tools}
    approval_required = _approval_required_tools(
        expert_config.hitl, selected_tools, expert_config.tools
    )
    max_turns = max(1, expert_config.max_turns)
    expert_name = expert_config.name

    # 系统提示 = 专家提示词 + 技能清单（渐进式披露：清单很便宜，正文按需载入）
    system_prompt = expert_config.system_prompt
    skill_index = SkillRegistry.render_index(bound_skills)
    if skill_index:
        system_prompt = f"{system_prompt}\n\n{skill_index}"

    # ------------------------------------------------------------------
    # 节点
    # ------------------------------------------------------------------

    def plan(state: ExpertState) -> dict[str, Any]:
        """让专家先给出简短执行计划。"""

        writer = get_writer()
        prompt = (
            "请针对下面的任务给出 3-5 步的简短执行计划（只列步骤，不要展开）。\n\n"
            f"{_build_task_prompt(state, bound_skills)}"
        )
        try:
            reply = llm.invoke(
                [SystemMessage(system_prompt), HumanMessage(prompt)]
            )
            plan_text = str(reply.content)
        except Exception as exc:  # pragma: no cover - 依赖外部服务
            plan_text = f"（模型不可用，跳过规划）{_fallback_message(exc).content}"
        if writer is not None:
            writer(
                {
                    "type": EventType.EXPERT_PLAN,
                    "expert": expert_name,
                    "payload": {"text": plan_text},
                }
            )
        # 每个任务从干净的状态开始：iteration / findings / artifacts 这些是**本轮**的产物，
        # 不重置的话，同一线程里聊第二轮时会把上一轮的计数和结论带过来——
        # iteration 累积到 max_turns 之后工具循环就再也不执行了。
        # ``messages`` 故意保留：那是跨轮记忆。
        return {
            "plan": plan_text,
            "iteration": 0,
            "pending_calls": [],
            "findings": [],
            "artifacts": [],
            "record": {},
            "status": "ok",
            "summary": "",
            "next_action": "",
            "turns_used": 0,
        }

    def act(state: ExpertState) -> dict[str, Any]:
        """决定下一步：要么发起工具调用，要么直接给出结论。"""

        writer = get_writer()
        history: list[AnyMessage] = list(state.get("messages") or [])
        # 历史在前、本轮指令在后：专家跨轮次保留记忆时，先看到上轮过程再看新指令
        messages: list[AnyMessage] = [
            SystemMessage(system_prompt),
            *history,
            HumanMessage(_build_task_prompt(state, bound_skills)),
        ]
        try:
            if selected_tools:
                bound = llm.bind_tools(selected_tools)
                reply = bound.invoke(messages)
            else:
                reply = llm.invoke(messages)
        except Exception as exc:  # pragma: no cover - 依赖外部服务
            return {
                "messages": [_fallback_message(exc)],
                "pending_calls": [],
                "iteration": state.get("iteration", 0) + 1,
                "status": "failed",
            }

        tool_calls = list(getattr(reply, "tool_calls", []) or [])
        pending = [
            {
                "id": call.get("id") or _stable_id(expert_name, str(call.get("name")), str(state.get("iteration"))),
                "name": str(call.get("name")),
                "args": dict(call.get("args") or {}),
            }
            for call in tool_calls
        ]
        if writer is not None:
            for call in pending:
                writer(
                    {
                        "type": EventType.EXPERT_TOOL_CALL,
                        "expert": expert_name,
                        "payload": {"tool": call["name"], "args": call["args"], "call_id": call["id"]},
                    }
                )
        return {
            "messages": [reply],
            "pending_calls": pending,
            "iteration": state.get("iteration", 0) + 1,
        }

    def execute_tools(state: ExpertState, config: RunnableConfig) -> dict[str, Any]:
        """执行工具调用；高危调用先过人工审批。

        节点分成两个阶段，顺序很重要：

        1. **审批阶段** —— 逐个（或整批）请求人工放行，可能在此中断；
        2. **执行阶段** —— 所有审批都通过后才真正执行工具。

        LangGraph 在恢复被中断的节点时会把整个节点**从头重跑**，
        如果审批与执行交错，已经跑过的工具会被重复执行（重复部署、重复写入…）。
        两阶段分离保证副作用只发生一次。
        """

        writer = get_writer()
        thread_id = ""
        if isinstance(config, dict):
            thread_id = (config.get("configurable") or {}).get("thread_id", "")

        pending = list(state.get("pending_calls") or [])
        policy = expert_config.hitl
        findings: list[str] = list(state.get("findings") or [])
        artifacts: list[dict[str, Any]] = list(state.get("artifacts") or [])

        # ---------------- 阶段 1：审批 ----------------
        # 白名单外的工具直接判负——不该为反正会被拒绝的调用去打扰用户
        reviewable = [call for call in pending if call["name"] in allowed_tools]
        # 结论按调用 id 存，最后**按模型给的调用顺序**回结果。
        # 顺序必须和 tool_calls 一一对应：MiniMax / DeepSeek 这类严格校验的接口
        # 遇到「结果顺序对不上」会直接报 tool call and result not match。
        verdicts: dict[str, tuple[bool, str]] = {
            call["id"]: (False, "not-allowed")
            for call in pending
            if call["name"] not in allowed_tools
        }

        if policy.mode == "static":
            needs_batch_review = any(
                call["name"] in approval_required for call in reviewable
            )
            if needs_batch_review:
                pre_registered = lookup_decision(
                    center, thread_id, expert_name, reviewable[0], reviewable
                )
                if pre_registered is not None:
                    # 前端已统一放行，不再重复打断
                    approved = pre_registered.state == "approved"
                    note = pre_registered.decision_note
                    for call in reviewable:
                        verdicts[call["id"]] = (approved, note)
                else:
                    batch_request = center.create(
                        expert=expert_name,
                        tool=",".join(sorted({str(call["name"]) for call in reviewable})),
                        args={"calls": reviewable},
                        reason=f"{expert_name} 请求批量执行 {len(reviewable)} 个高危工具",
                        risk="high",
                        thread_id=thread_id,
                        request_id=static_batch_request_id(
                            thread_id, expert_name, reviewable
                        ),
                    )
                    batch_payload = {
                        **batch_request.to_payload(),
                        "kind": "batch_approval",
                        "calls": reviewable,
                    }
                    if writer is not None:
                        writer(
                            {
                                "type": EventType.EXPERT_APPROVAL,
                                "expert": expert_name,
                                "payload": batch_payload,
                            }
                        )
                    decision = interrupt(batch_payload)
                    approved, note = parse_resume_value(decision)
                    for call in reviewable:
                        verdicts[call["id"]] = (approved, note)
        else:
            for call in reviewable:
                name = call["name"]
                if name not in approval_required:
                    verdicts[call["id"]] = (True, "")
                    continue
                pre_registered = lookup_decision(center, thread_id, expert_name, call, pending)
                if pre_registered is not None:
                    verdicts[call["id"]] = (
                        pre_registered.state == "approved",
                        pre_registered.decision_note,
                    )
                    continue
                spec = TOOL_SPECS.get(name)
                request = center.create(
                    expert=expert_name,
                    tool=name,
                    args=call["args"],
                    reason=f"{expert_name} 请求调用高危工具 {name}",
                    risk=spec.risk if spec else "medium",
                    thread_id=thread_id,
                    request_id=single_call_request_id(thread_id, expert_name, call),
                )
                if writer is not None:
                    writer(
                        {
                            "type": EventType.EXPERT_APPROVAL,
                            "expert": expert_name,
                            "payload": request.to_payload(),
                        }
                    )
                decision = interrupt(request.to_payload())
                approved, note = parse_resume_value(decision)
                verdicts[call["id"]] = (approved, note)

        # ---------------- 阶段 2：执行 ----------------
        messages: list[AnyMessage] = []
        # 严格按模型给的调用顺序回结果——顺序必须和 tool_calls 对齐
        for call in pending:
            approved, note = verdicts.get(call["id"], (False, "未处理"))
            name = call["name"]
            args = call["args"]

            if not approved:
                if note == "not-allowed":
                    message = (
                        f"工具 {name} 不在本专家的白名单内，已拒绝执行。"
                        if name in TOOL_SPECS
                        else f"未知工具 {name}"
                    )
                else:
                    message = f"{REJECTED_PREFIX}：{note or '未说明原因'}（工具：{name}）"
                findings.append(message)
                messages.append(
                    ToolMessage(content=message, tool_call_id=call["id"], name=name)
                )
                if writer is not None:
                    writer(
                        {
                            "type": EventType.EXPERT_TOOL_RESULT,
                            "expert": expert_name,
                            "payload": {
                                "tool": name,
                                "call_id": call["id"],
                                "rejected": True,
                                "text": message,
                            },
                        }
                    )
                continue

            # 白名单在执行期同样生效：模型幻觉出的工具名调不动
            tool = allowed_tools.get(name)
            if tool is None:
                text = (
                    f"工具 {name} 不在本专家的白名单内，已拒绝执行。"
                    if name in TOOL_SPECS
                    else f"未知工具 {name}"
                )
                findings.append(text)
                messages.append(
                    ToolMessage(content=text, tool_call_id=call["id"], name=name)
                )
                continue

            try:
                output = tool.invoke(args)
                text = output if isinstance(output, str) else str(output)
                ok = True
            except Exception as exc:  # 工具内部错误（沙箱越界等）
                text = f"{TOOL_ERROR_PREFIX}{exc}"
                ok = False
                findings.append(text)

            if ok:
                artifacts.append(
                    Artifact(
                        kind="code" if name in ("read_file", "search_code", "ast_inspect") else "log",
                        name=name,
                        content=text,
                        path=str(args.get("path")) if args.get("path") else None,
                    ).to_dict()
                )
            messages.append(
                ToolMessage(content=text, tool_call_id=call["id"], name=name)
            )
            if writer is not None:
                writer(
                    {
                        "type": EventType.EXPERT_TOOL_RESULT,
                        "expert": expert_name,
                        "payload": {"tool": name, "call_id": call["id"], "ok": ok, "text": text},
                    }
                )

        return {
            "messages": messages,
            "pending_calls": [],
            "findings": findings,
            "artifacts": artifacts,
        }

    def finalize(state: ExpertState) -> dict[str, Any]:
        """汇总产出，产出符合契约的结论。"""

        writer = get_writer()
        history = list(state.get("messages") or [])
        messages: list[AnyMessage] = [
            SystemMessage(system_prompt),
            HumanMessage(
                "请基于以上过程给出最终结论：说明结论、依据、以及后续建议（如果需要他人配合请写进 next_action）。"
            ),
            *history,
        ]
        try:
            reply = llm.invoke(messages)
            summary = str(reply.content).strip()
        except Exception as exc:  # pragma: no cover
            summary = _fallback_message(exc, "（结论生成失败）").content

        if not summary:
            summary = "（专家未产出文字结论）"

        findings = list(state.get("findings") or [])
        status = "ok"
        if state.get("status") == "failed":
            status = "failed"
        elif findings:
            status = "partial"

        output: ExpertOutput = {
            "expert": expert_name,
            "status": status,
            "summary": summary,
            "artifacts": list(state.get("artifacts") or []),
            "findings": findings,
            "next_action": "",
            "turns_used": state.get("iteration", 0),
        }
        if writer is not None:
            writer(
                {
                    "type": EventType.EXPERT_COMPLETED,
                    "expert": expert_name,
                    "payload": {"text": summary, "status": status},
                }
            )
        # 写回父图的唯一通道：{专家名: 专家输出}
        return {
            "summary": summary,
            "status": status,
            "next_action": "",
            "turns_used": output["turns_used"],
            "expert": expert_name,
            "record": {expert_name: output},
        }

    # ------------------------------------------------------------------
    # 组图
    # ------------------------------------------------------------------

    def route_after_act(state: ExpertState) -> str:
        if state.get("pending_calls") and state.get("iteration", 0) < max_turns:
            return "execute_tools"
        return "finalize"

    builder: StateGraph = StateGraph(
        ExpertState,
        input_schema=ExpertInputSchema,
        output_schema=ExpertOutputSchema,
    )
    builder.add_node("plan", plan)
    builder.add_node("act", act)
    builder.add_node("execute_tools", execute_tools)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "act")
    builder.add_conditional_edges(
        "act",
        route_after_act,
        {"execute_tools": "execute_tools", "finalize": "finalize"},
    )
    builder.add_edge("execute_tools", "act")
    builder.add_edge("finalize", END)

    # 可选的「执行前整体放行」闸门：编译期挂 interrupt_before，
    # 在工具执行节点之前整体暂停一次（适合顶层单专家运行）。
    interrupt_before = ["execute_tools"] if expert_config.interrupt_before_tools else []
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before or None,
    )


def _artifacts_to_contracts(items: Any) -> list[Artifact]:
    """把状态里的产出物转成契约对象（状态里存的是普通 dict）。"""

    result: list[Artifact] = []
    for item in items or []:
        if isinstance(item, Artifact):
            result.append(item)
        elif isinstance(item, dict):
            result.append(Artifact.from_dict(item))
    return result


def expert_output_from_state(state: dict[str, Any]) -> ExpertOutput:
    """把专家子图的最终状态转换成输出契约。

    兼容三种形态：

    * ``{"record": {专家名: 输出}}`` —— 作为子图节点时的输出（只暴露 record 通道）
    * ``{"__interrupt__": (...)}`` —— 因人工审批挂起，尚未产出结论
    * 完整状态字典 —— 直接读取 summary / status 等字段
    """

    if not state:
        return ExpertOutput(expert="", status="skipped", summary="")

    if "__interrupt__" in state:
        return ExpertOutput(
            expert="",
            status="need_human",
            summary="",
            artifacts=[],
            findings=[],
            next_action="等待人工审批",
            turns_used=0,
        )

    record = state.get("record")
    if isinstance(record, dict) and record:
        name, payload = next(iter(record.items()))
        output = dict(payload) if isinstance(payload, dict) else {}
        output.setdefault("expert", name)
        return ExpertOutput(
            expert=str(output.get("expert") or name),
            status=str(output.get("status") or "ok"),
            summary=str(output.get("summary") or ""),
            artifacts=_artifacts_to_contracts(output.get("artifacts")),
            findings=list(output.get("findings") or []),
            next_action=str(output.get("next_action") or ""),
            turns_used=int(output.get("turns_used") or 0),
        )

    return ExpertOutput(
        expert=state.get("expert", ""),
        status=state.get("status", "ok"),
        summary=state.get("summary", ""),
        artifacts=_artifacts_to_contracts(state.get("artifacts")),
        findings=list(state.get("findings") or []),
        next_action=state.get("next_action", ""),
        turns_used=int(state.get("turns_used", 0)),
    )


def make_approval_request(
    center: ApprovalCenter,
    *,
    expert: str,
    tool: str,
    args: dict[str, Any],
    thread_id: str,
    reason: str = "",
) -> ApprovalRequest:
    """给前端用的辅助函数：登记一条审批请求。"""

    return center.create(
        expert=expert,
        tool=tool,
        args=args,
        reason=reason,
        thread_id=thread_id,
    )
