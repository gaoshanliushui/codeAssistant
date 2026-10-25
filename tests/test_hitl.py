"""人审（Human-in-the-Loop）测试：中断、通过、拒绝、断点恢复。"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from codeassistant.approval import ApprovalCenter, parse_resume_value
from codeassistant.configs import HitlPolicy
from codeassistant.graphs.expert import build_expert_graph, expert_output_from_state
from codeassistant.llm import MockChat
from codeassistant.tools import ToolRegistry


def _script():
    return [
        {"content": "计划"},
        {
            "tool_calls": [
                {"name": "write_file", "args": {"path": "out.txt", "content": "hello"}, "id": "c1"}
            ]
        },
        {"content": "已经写好了"},
    ]


async def test_dynamic_mode_interrupts_and_approves(
    tmp_path: Path, make_expert_config, tool_registry: ToolRegistry
):
    config = make_expert_config(
        tools=("write_file",),
        hitl=HitlPolicy(tools=("write_file",), mode="dynamic"),
    )
    center = ApprovalCenter()
    graph = build_expert_graph(
        config,
        llm=MockChat(model="mock", script=_script()),
        tool_registry=tool_registry,
        approval_center=center,
        checkpointer=InMemorySaver(),
    )
    run_config = {"configurable": {"thread_id": "hitl-approve"}}

    async for _ in graph.astream({"task": "写文件"}, run_config, stream_mode=["custom"]):
        pass

    pending = center.pending()
    assert len(pending) == 1
    assert pending[0].tool == "write_file"
    assert pending[0].thread_id == "hitl-approve"

    center.resolve(pending[0].id, approved=True, note="允许")
    async for _ in graph.astream(
        Command(resume={"approved": True, "reason": "允许"}),
        run_config,
        stream_mode=["custom"],
    ):
        pass

    state = await graph.aget_state(run_config)
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"
    assert state.values.get("status") == "ok"


async def test_dynamic_mode_rejects_and_continues(
    tmp_path: Path, make_expert_config, tool_registry: ToolRegistry
):
    config = make_expert_config(
        tools=("write_file",),
        hitl=HitlPolicy(tools=("write_file",), mode="dynamic"),
    )
    center = ApprovalCenter()
    graph = build_expert_graph(
        config,
        llm=MockChat(model="mock", script=_script()),
        tool_registry=tool_registry,
        approval_center=center,
        checkpointer=InMemorySaver(),
    )
    run_config = {"configurable": {"thread_id": "hitl-reject"}}

    async for _ in graph.astream({"task": "写文件"}, run_config, stream_mode=["custom"]):
        pass

    pending = center.pending()
    center.resolve(pending[0].id, approved=False, note="不允许")

    async for _ in graph.astream(
        Command(resume={"approved": False, "reason": "不允许"}),
        run_config,
        stream_mode=["custom"],
    ):
        pass

    state = await graph.aget_state(run_config)
    findings = state.values.get("findings") or []
    assert any("已被人工审批拒绝" in item for item in findings)
    assert not (tmp_path / "out.txt").exists(), "被拒绝的工具不应产生副作用"
    assert state.values.get("status") == "partial"


async def test_low_risk_tool_needs_no_approval(
    tmp_path: Path, make_expert_config, tool_registry: ToolRegistry
):
    (tmp_path / "in.txt").write_text("data", encoding="utf-8")
    config = make_expert_config(
        tools=("read_file",),
        hitl=HitlPolicy(tools=("write_file",), mode="dynamic"),
    )
    center = ApprovalCenter()
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {"tool_calls": [{"name": "read_file", "args": {"path": "in.txt"}, "id": "c1"}]},
            {"content": "读完了"},
        ],
    )
    graph = build_expert_graph(
        config,
        llm=llm,
        tool_registry=tool_registry,
        approval_center=center,
        checkpointer=InMemorySaver(),
    )

    result = await graph.ainvoke({"task": "读"}, {"configurable": {"thread_id": "no-hitl"}})
    output = expert_output_from_state(result)

    assert center.pending() == []
    assert output["status"] == "ok"
    assert output["artifacts"]


async def test_static_mode_approves_whole_batch(
    tmp_path: Path, make_expert_config, tool_registry: ToolRegistry
):
    """static 模式：整批工具调用共享一次人工放行。"""

    config = make_expert_config(
        tools=("write_file",),
        hitl=HitlPolicy(tools=("write_file",), mode="static"),
    )
    center = ApprovalCenter()
    graph = build_expert_graph(
        config,
        llm=MockChat(model="mock", script=_script()),
        tool_registry=tool_registry,
        approval_center=center,
        checkpointer=InMemorySaver(),
    )
    run_config = {"configurable": {"thread_id": "hitl-static"}}

    interrupts: list[object] = []
    async for chunk in graph.astream(
        {"task": "写文件"}, run_config, stream_mode=["updates"]
    ):
        data = chunk[1] if isinstance(chunk, tuple) else chunk
        if isinstance(data, dict) and "__interrupt__" in data:
            interrupts.extend(data["__interrupt__"])

    assert interrupts, "static 模式应暂停等待整批审批"
    payload = interrupts[0].value
    assert payload["kind"] == "batch_approval"
    assert payload["calls"], "批次载荷应包含待执行的调用"

    async for _ in graph.astream(
        Command(resume={"approved": True, "reason": "整批放行"}),
        run_config,
        stream_mode=["updates"],
    ):
        pass

    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"


async def test_static_mode_rejects_whole_batch(
    tmp_path: Path, make_expert_config, tool_registry: ToolRegistry
):
    config = make_expert_config(
        tools=("write_file",),
        hitl=HitlPolicy(tools=("write_file",), mode="static"),
    )
    center = ApprovalCenter()
    graph = build_expert_graph(
        config,
        llm=MockChat(model="mock", script=_script()),
        tool_registry=tool_registry,
        approval_center=center,
        checkpointer=InMemorySaver(),
    )
    run_config = {"configurable": {"thread_id": "hitl-static-reject"}}

    async for _ in graph.astream({"task": "写文件"}, run_config, stream_mode=["updates"]):
        pass

    async for _ in graph.astream(
        Command(resume={"approved": False, "reason": "整批拒绝"}),
        run_config,
        stream_mode=["updates"],
    ):
        pass

    state = await graph.aget_state(run_config)
    assert not (tmp_path / "out.txt").exists()
    assert any("已被人工审批拒绝" in item for item in state.values.get("findings") or [])


async def test_interrupt_before_gate_pauses_before_tools(
    tmp_path: Path, make_expert_config, tool_registry: ToolRegistry
):
    """``interrupt_before_tools``：编译期在工具执行节点前整体挂起。"""

    from dataclasses import replace

    config = replace(
        make_expert_config(
            tools=("write_file",),
            hitl=HitlPolicy(tools=("write_file",), mode="dynamic"),
        ),
        interrupt_before_tools=True,
    )
    center = ApprovalCenter()
    graph = build_expert_graph(
        config,
        llm=MockChat(model="mock", script=_script()),
        tool_registry=tool_registry,
        approval_center=center,
        checkpointer=InMemorySaver(),
    )
    run_config = {"configurable": {"thread_id": "hitl-gate"}}

    paused = False
    async for chunk in graph.astream({"task": "写文件"}, run_config, stream_mode=["updates"]):
        data = chunk[1] if isinstance(chunk, tuple) else chunk
        if isinstance(data, dict) and "__interrupt__" in data:
            paused = True
            break

    assert paused, "interrupt_before 应在工具执行前暂停"
    state = await graph.aget_state(run_config)
    assert state.values.get("pending_calls"), "暂停时待执行调用应保留在状态里"
    assert not (tmp_path / "out.txt").exists(), "暂停时不应执行工具"


def test_parse_resume_value_variants():
    assert parse_resume_value(True) == (True, "通过")
    assert parse_resume_value(False)[0] is False
    assert parse_resume_value({"approved": True, "reason": "ok"}) == (True, "ok")
    assert parse_resume_value({"decisions": [{"approved": False}]})[0] is False
    assert parse_resume_value(None)[0] is False


async def test_team_member_approval_propagates(
    tmp_path: Path, make_expert_config, tool_registry: ToolRegistry
):
    """成员专家的审批必须能穿过专家团这一层正确冒泡与恢复。"""

    from dataclasses import replace

    from codeassistant.graphs.factory import ExpertGraphFactory
    from codeassistant.registry import ExpertRegistry
    from codeassistant.settings import Settings

    settings = Settings.load(
        workspace=tmp_path, provider="mock", checkpointer="memory"
    )
    registry = ExpertRegistry(settings, tools=tool_registry)
    team = replace(registry.get_team("toutiao-finance-account-ops-team"), members=("writer",))
    registry.experts["writer"] = make_expert_config(
        "writer",
        tools=("write_file",),
        hitl=HitlPolicy(tools=("write_file",), mode="dynamic"),
    )

    script = [
        {"content": "计划"},
        {
            "tool_calls": [
                {
                    "name": "write_file",
                    "args": {"path": "team_out.txt", "content": "ok"},
                    "id": "c1",
                }
            ]
        },
        {"content": "写好了"},
    ]
    factory = ExpertGraphFactory(
        registry,
        settings,
        tool_registry=tool_registry,
        llm_factory=lambda _cfg: MockChat(model="mock", script=list(script)),
        team_llm_factory=lambda _cfg: MockChat(model="mock", script=list(script)),
    )
    graph = factory.get_team(team, mode="supervisor")
    run_config = {"configurable": {"thread_id": "team-hitl"}}

    interrupted = False
    async for chunk in graph.astream({"task": "写文件"}, run_config, stream_mode=["updates"]):
        data = chunk[1] if isinstance(chunk, tuple) else chunk
        if isinstance(data, dict) and "__interrupt__" in data:
            interrupted = True
            break

    assert interrupted, "成员专家的审批应向上冒泡到专家团"

    async for _ in graph.astream(
        Command(resume={"approved": True, "reason": "允许"}),
        run_config,
        stream_mode=["updates"],
    ):
        pass

    assert (tmp_path / "team_out.txt").read_text(encoding="utf-8") == "ok"
