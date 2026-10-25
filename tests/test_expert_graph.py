"""专家子图测试（假 LLM + 假工具节点替换真实模型节点）。"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from codeassistant.approval import ApprovalCenter
from codeassistant.graphs.expert import build_expert_graph, expert_output_from_state
from codeassistant.llm import MockChat
from codeassistant.tools import ToolRegistry


def _build(config, tool_registry, llm, center=None, checkpointer=None):
    return build_expert_graph(
        config,
        llm=llm,
        tool_registry=tool_registry,
        approval_center=center or ApprovalCenter(),
        checkpointer=checkpointer or InMemorySaver(),
    )


async def test_expert_returns_contract(make_expert_config, tool_registry: ToolRegistry):
    config = make_expert_config()
    llm = MockChat(model="mock", script=[{"content": "计划"}, {"content": "结论：一切正常"}])
    graph = _build(config, tool_registry, llm)

    result = await graph.ainvoke(
        {"task": "检查一下"}, {"configurable": {"thread_id": "t1"}}
    )
    output = expert_output_from_state(result)

    assert output["expert"] == "demo-expert"
    assert output["status"] == "ok"
    assert output["summary"] == "结论：一切正常"
    assert output["turns_used"] >= 1
    assert isinstance(output["artifacts"], list)


async def test_expert_executes_tools_and_records_artifacts(
    tmp_path: Path, make_expert_config, tool_registry: ToolRegistry
):
    (tmp_path / "demo.txt").write_text("hello\n", encoding="utf-8")
    config = make_expert_config(tools=("read_file",))
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {"tool_calls": [{"name": "read_file", "args": {"path": "demo.txt"}, "id": "c1"}]},
            {"content": "已读取"},
        ],
    )
    graph = _build(config, tool_registry, llm)

    result = await graph.ainvoke(
        {"task": "读文件"}, {"configurable": {"thread_id": "t2"}}
    )
    output = expert_output_from_state(result)

    assert output["status"] == "ok"
    assert output["artifacts"], "工具产出应记录为 artifact"
    assert "hello" in output["artifacts"][0].content


async def test_expert_stops_at_max_turns(make_expert_config, tool_registry: ToolRegistry):
    """模型一直要求调工具时，循环必须在 max_turns 处收敛。"""

    config = make_expert_config(tools=("list_files",), max_turns=3)
    llm = MockChat(
        model="mock",
        script=[
            {
                "tool_calls": [
                    {"name": "list_files", "args": {"path": "."}, "id": "c1"}
                ]
            }
        ],
    )
    graph = _build(config, tool_registry, llm)

    result = await graph.ainvoke(
        {"task": "无限循环"}, {"configurable": {"thread_id": "t3"}}
    )
    output = expert_output_from_state(result)

    assert output["turns_used"] <= config.max_turns + 1


async def test_expert_handles_unknown_tool(make_expert_config, tool_registry: ToolRegistry):
    config = make_expert_config(tools=("list_files",))
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {"tool_calls": [{"name": "no_such_tool", "args": {}, "id": "c1"}]},
            {"content": "结束"},
        ],
    )
    graph = _build(config, tool_registry, llm)

    result = await graph.ainvoke({"task": "x"}, {"configurable": {"thread_id": "t4"}})
    output = expert_output_from_state(result)

    assert any("未知工具" in finding for finding in output["findings"])


async def test_expert_survives_model_failure(make_expert_config, tool_registry: ToolRegistry):
    """模型不可用时，专家图不应崩溃。"""

    class BrokenLLM(MockChat):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("模型炸了")

    config = make_expert_config()
    graph = _build(config, tool_registry, BrokenLLM(model="mock"))

    result = await graph.ainvoke({"task": "x"}, {"configurable": {"thread_id": "t5"}})
    output = expert_output_from_state(result)

    assert output["status"] in ("failed", "partial", "ok")
    assert output["summary"]


async def test_expert_graph_is_compilable_standalone(make_expert_config, tool_registry):
    """子图可以脱离主编排器单独编译。"""

    config = make_expert_config()
    graph = _build(config, tool_registry, MockChat(model="mock"))
    assert graph is not None
    assert hasattr(graph, "ainvoke")
