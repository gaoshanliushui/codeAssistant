"""记忆隔离测试：三层 thread_id 命名空间。"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

from codeassistant.graphs.expert import build_expert_graph
from codeassistant.llm import MockChat
from codeassistant.memory import NamespacedSaver, ThreadScope, build_checkpointer
from codeassistant.settings import Settings
from codeassistant.tools import ToolRegistry


def test_thread_scope_namespaces():
    scope = ThreadScope(session_id="abc")
    assert scope.session_thread() == "s:abc"
    assert scope.expert_thread("sdt-backend") == "e:sdt-backend:abc"
    assert scope.team_thread("software-development-team") == "t:software-development-team:abc"


def test_thread_scope_isolates_sessions():
    a = ThreadScope(session_id="a")
    b = ThreadScope(session_id="b")
    assert a.expert_thread("x") != b.expert_thread("x")
    assert a.session_thread() != b.session_thread()


def test_namespaced_saver_prefixes_checkpoint_ns():
    saver = NamespacedSaver(InMemorySaver(), "expert:demo")
    config = saver._ns({"configurable": {"thread_id": "t1"}})
    assert config["configurable"]["checkpoint_ns"] == "expert:demo"


def test_namespaced_saver_keeps_existing_namespace():
    saver = NamespacedSaver(InMemorySaver(), "expert:demo")
    config = saver._ns({"configurable": {"thread_id": "t1", "checkpoint_ns": "node"}})
    assert config["configurable"]["checkpoint_ns"] == "expert:demo|node"


async def test_expert_memory_is_isolated_between_experts(
    tmp_path, make_expert_config, tool_registry: ToolRegistry
):
    """两个专家在同一个会话里的记忆互不干扰。"""

    scope = ThreadScope(session_id="shared")
    saver = InMemorySaver()

    def build(name: str):
        config = make_expert_config(name)
        return build_expert_graph(
            config,
            llm=MockChat(model="mock", script=[{"content": f"{name} 的结论"}]),
            tool_registry=tool_registry,
            checkpointer=NamespacedSaver(saver, f"expert:{name}"),
        )

    graph_a = build("expert-a")
    graph_b = build("expert-b")

    await graph_a.ainvoke(
        {"task": "任务"}, {"configurable": {"thread_id": scope.expert_thread("expert-a")}}
    )

    state_a = await graph_a.aget_state(
        {"configurable": {"thread_id": scope.expert_thread("expert-a")}}
    )
    state_b = await graph_b.aget_state(
        {"configurable": {"thread_id": scope.expert_thread("expert-b")}}
    )

    assert state_a.values.get("summary")
    assert not state_b.values.get("summary"), "专家 B 不应看到专家 A 的记忆"


async def test_expert_memory_persists_across_turns(
    make_expert_config, tool_registry: ToolRegistry
):
    """同一专家在同一会话内，第二次执行能读到上一次的记忆。"""

    scope = ThreadScope(session_id="s1")
    saver = InMemorySaver()
    config = make_expert_config("rememberer")
    graph = build_expert_graph(
        config,
        llm=MockChat(model="mock", script=[{"content": "第一次的结论"}]),
        tool_registry=tool_registry,
        checkpointer=NamespacedSaver(saver, "expert:rememberer"),
    )
    thread_id = scope.expert_thread("rememberer")

    await graph.ainvoke({"task": "第一轮"}, {"configurable": {"thread_id": thread_id}})
    await graph.ainvoke({"task": "第二轮"}, {"configurable": {"thread_id": thread_id}})

    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    messages = state.values.get("messages") or []
    assert len(messages) >= 2, "同一专家的对话历史应累积"


def test_build_checkpointer_memory(settings: Settings):
    from langgraph.checkpoint.memory import InMemorySaver

    assert isinstance(build_checkpointer(settings), InMemorySaver)


def test_build_checkpointer_sqlite(tmp_path):
    from codeassistant.memory import HybridSqliteSaver

    local = Settings.load(
        workspace=tmp_path, provider="mock", checkpointer="sqlite", state_db=tmp_path / "s.db"
    )
    saver = build_checkpointer(local)
    assert isinstance(saver, HybridSqliteSaver)
    saver.close()
