"""回归测试：覆盖评审中发现的真实缺陷。

每个用例对应一个曾经存在的 bug，防止回归。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from codeassistant.app import CodeAssistant
from codeassistant.configs import HitlPolicy
from codeassistant.graphs.expert import build_expert_graph, expert_output_from_state
from codeassistant.llm import MockChat
from codeassistant.memory import NamespacedSaver
from codeassistant.settings import Settings
from codeassistant.tools import Sandbox, ToolRegistry

# --------------------------------------------------------------------------
# 工具白名单必须在**执行期**生效
# --------------------------------------------------------------------------


async def test_tool_outside_whitelist_is_refused(tmp_path: Path, make_expert_config, tool_registry):
    """只读白名单的专家不能调用 delete_file，即使模型硬要调。"""

    victim = tmp_path / "victim.txt"
    victim.write_text("不要删我", encoding="utf-8")

    config = make_expert_config(
        "readonly",
        tools=("read_file",),
        hitl=HitlPolicy(tools=(), mode="dynamic"),  # 白名单外的高危工具也不该走到审批
    )
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {"tool_calls": [{"name": "delete_file", "args": {"path": "victim.txt"}, "id": "c1"}]},
            {"content": "结束"},
        ],
    )
    graph = build_expert_graph(
        config, llm=llm, tool_registry=tool_registry, checkpointer=InMemorySaver()
    )

    result = await graph.ainvoke({"task": "删掉它"}, {"configurable": {"thread_id": "wl-1"}})
    output = expert_output_from_state(result)

    assert victim.exists(), "白名单外的工具不应被执行"
    assert any("白名单" in finding for finding in output["findings"])


# --------------------------------------------------------------------------
# 恢复被中断的节点时，工具不能重复执行
# --------------------------------------------------------------------------


async def test_tools_run_once_after_resume(tmp_path: Path, make_expert_config, tool_registry):
    """审批中断恢复后，已经通过审批的工具只执行一次。"""

    counter = tmp_path / "counter.txt"

    config = make_expert_config(
        "counter",
        tools=("run_command", "delete_file"),
        hitl=HitlPolicy(tools=("delete_file",), mode="dynamic"),
    )
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {"command": "echo run >> counter.txt"},
                        "id": "c1",
                    },
                    {
                        "name": "delete_file",
                        "args": {"path": "counter.txt"},
                        "id": "c2",
                    },
                ]
            },
            {"content": "结束"},
        ],
    )
    graph = build_expert_graph(
        config, llm=llm, tool_registry=tool_registry, checkpointer=InMemorySaver()
    )
    run_config = {"configurable": {"thread_id": "once-1"}}

    async for _ in graph.astream({"task": "跑一下"}, run_config, stream_mode=["updates"]):
        pass

    async for _ in graph.astream(
        Command(resume={"approved": False, "reason": "拒绝删除"}),
        run_config,
        stream_mode=["updates"],
    ):
        pass

    content = counter.read_text(encoding="utf-8") if counter.exists() else ""
    assert content.count("run") == 1, f"命令只应执行一次，实际内容：{content!r}"


# --------------------------------------------------------------------------
# 沙箱：符号链接不能被搜索工具绕过
# --------------------------------------------------------------------------


def test_search_code_does_not_follow_symlink_out_of_workspace(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP_SECRET_TOKEN=abc123", encoding="utf-8")
    (workspace / "link.txt").symlink_to(secret)

    tools = ToolRegistry(Sandbox(workspace))
    output = tools.get("search_code").invoke({"query": "TOP_SECRET"})

    # 若跟随了符号链接，结果里会出现 link.txt:1 与外部文件的内容
    assert "link.txt" not in output, "搜索不能读到工作区之外的符号链接内容"
    assert "未找到" in output


# --------------------------------------------------------------------------
# 子进程环境变量不能泄露 API Key
# --------------------------------------------------------------------------


def test_run_command_does_not_leak_api_key(tmp_path: Path, tool_registry, monkeypatch):
    monkeypatch.setenv("CODEASSISTANT_API_KEY", "sk-secret-123")
    monkeypatch.setenv("MY_AUTH_TOKEN", "tok-secret-456")

    output = tool_registry.get("run_command").invoke({"command": "env"})

    assert "sk-secret-123" not in output
    assert "tok-secret-456" not in output


# --------------------------------------------------------------------------
# 检查点的历史查询（alist 是异步生成器）
# --------------------------------------------------------------------------


async def test_state_history_works(make_expert_config, tool_registry):
    from codeassistant.graphs.expert import build_expert_graph as _build

    config = make_expert_config("hist")
    graph = _build(
        config,
        llm=MockChat(model="mock", script=[{"content": "结论"}]),
        tool_registry=tool_registry,
        checkpointer=NamespacedSaver(InMemorySaver(), "expert:hist"),
    )
    run_config = {"configurable": {"thread_id": "hist-1"}}
    await graph.ainvoke({"task": "t"}, run_config)

    history = [item async for item in graph.aget_state_history(run_config)]
    assert history, "应能查到检查点历史"


# --------------------------------------------------------------------------
# 专家团：不能反复派活给同一个成员
# --------------------------------------------------------------------------


async def test_supervisor_does_not_redispatch_finished_member(
    registry, settings: Settings, tool_registry, mock_llm_factory
):
    from dataclasses import replace

    from codeassistant.graphs.factory import ExpertGraphFactory

    factory = ExpertGraphFactory(
        registry,
        settings,
        tool_registry=tool_registry,
        llm_factory=mock_llm_factory,
        checkpointer=InMemorySaver(),
    )
    team = replace(
        registry.get_team("toutiao-finance-account-ops-team"),
        members=("finance-data-collector", "finance-article-writer"),
    )
    graph = factory.get_team(team, mode="supervisor")

    result = await graph.ainvoke({"task": "任务"}, {"configurable": {"thread_id": "redispatch"}})

    record = result.get("record") or {}
    assert set(record) == {"finance-data-collector", "finance-article-writer"}, (
        "两位成员都应该被派活过，不能一直重复派同一个人"
    )


# --------------------------------------------------------------------------
# 目标不存在时不能报"成功"
# --------------------------------------------------------------------------


async def test_app_run_reports_failure_for_unknown_target(app: CodeAssistant):
    result = await app.run("x", expert="__not_exist__")
    assert result.status == "failed"
    assert result.summary


async def test_cli_exit_code_follows_status(app: CodeAssistant):
    result = await app.run("x", expert="__not_exist__")
    assert result.status != "ok"


# --------------------------------------------------------------------------
# 审批结论不能跨调用复用
# --------------------------------------------------------------------------


def test_approval_id_includes_tool_and_args():
    from codeassistant.graphs.expert import single_call_request_id

    call_a = {"id": "call_1", "name": "write_file", "args": {"path": "a.txt"}}
    call_b = {"id": "call_1", "name": "write_file", "args": {"path": "b.txt"}}
    call_c = {"id": "call_1", "name": "delete_file", "args": {"path": "a.txt"}}

    assert single_call_request_id("t", "e", call_a) != single_call_request_id("t", "e", call_b)
    assert single_call_request_id("t", "e", call_a) != single_call_request_id("t", "e", call_c)
    assert single_call_request_id("t", "e", call_a) == single_call_request_id("t", "e", call_a)


# --------------------------------------------------------------------------
# 坏掉的一个 agent 文件不应该拖垮整个专家包
# --------------------------------------------------------------------------


def test_loader_survives_bad_max_turns(tmp_path: Path):
    from codeassistant.loader import load_agent_doc

    path = tmp_path / "bad.md"
    path.write_text(
        "---\nname: bad\nmaxTurns: 不是数字\n---\n\n提示词\n",
        encoding="utf-8",
    )
    doc = load_agent_doc(path)
    assert doc.max_turns == 20


def test_loader_ignores_manifest_path_escape(tmp_path: Path):
    import json

    from codeassistant.loader import load_pack

    pack_root = tmp_path / "pack"
    (pack_root / ".codebuddy-plugin").mkdir(parents=True)
    (pack_root / ".codebuddy-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "evil",
                "expertType": "agent",
                "agentName": "x",
                "agents": ["../../outside.md"],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "outside.md").write_text("---\nname: outside\n---\n\n坏\n", encoding="utf-8")

    pack = load_pack(pack_root)
    assert "outside" not in pack.agents


# --------------------------------------------------------------------------
# 事件历史不能无限增长
# --------------------------------------------------------------------------


def test_event_bus_history_is_bounded():
    from codeassistant.observability import EventBus, EventType, StreamEvent

    bus = EventBus()
    for index in range(EventBus.MAX_HISTORY + 50):
        bus.emit(StreamEvent(type=EventType.ERROR, payload={"i": index}))

    assert len(bus.history) == EventBus.MAX_HISTORY


# --------------------------------------------------------------------------
# 专家团成员之间不能互相覆盖产出
# --------------------------------------------------------------------------


async def test_parallel_team_members_do_not_clobber(
    registry, settings: Settings, tool_registry, mock_llm_factory
):
    from codeassistant.graphs.factory import ExpertGraphFactory

    factory = ExpertGraphFactory(
        registry,
        settings,
        tool_registry=tool_registry,
        llm_factory=mock_llm_factory,
        checkpointer=InMemorySaver(),
    )
    team = registry.get_team("toutiao-finance-account-ops-team")
    graph = factory.get_team(team, mode="parallel")

    result = await graph.ainvoke({"task": "任务"}, {"configurable": {"thread_id": "clobber"}})

    record = result.get("record") or {}
    assert len(record) == len(team.members)


@pytest.fixture
def app(settings: Settings) -> CodeAssistant:
    return CodeAssistant(settings)


def test_workspace_env_is_respected(tmp_path: Path):
    settings = Settings.load(workspace=tmp_path, provider="mock", checkpointer="memory")
    assert settings.workspace == tmp_path.resolve()
    assert os.path.isdir(settings.workspace)
