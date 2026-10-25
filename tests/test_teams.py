"""专家团测试：主理人调度与并行汇。"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

from codeassistant.graphs.factory import ExpertGraphFactory
from codeassistant.registry import ExpertRegistry
from codeassistant.settings import Settings
from codeassistant.tools import ToolRegistry

TEAM_NAME = "toutiao-finance-account-ops-team"


def _factory(registry, settings, tool_registry, mock_llm_factory):
    return ExpertGraphFactory(
        registry,
        settings,
        tool_registry=tool_registry,
        llm_factory=mock_llm_factory,
        checkpointer=InMemorySaver(),
    )


async def test_supervisor_team_dispatches_to_all_members(
    registry: ExpertRegistry, settings: Settings, tool_registry: ToolRegistry, mock_llm_factory
):
    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    team = registry.get_team(TEAM_NAME)
    graph = factory.get_team(team, mode="supervisor")

    result = await graph.ainvoke(
        {"task": "写一篇财经稿"}, {"configurable": {"thread_id": "team-1"}}
    )

    record = result.get("record") or {}
    assert set(record) == set(team.members)
    assert result.get("status") == "ok"
    assert result.get("report"), "专家团应汇编出交付报告"
    assert all(output.get("summary") for output in record.values())


async def test_supervisor_team_routes_members_in_order(
    registry: ExpertRegistry, settings: Settings, tool_registry: ToolRegistry, mock_llm_factory
):
    """主理人按顺序派活，成员产出汇总在 record 里。"""

    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    team = registry.get_team(TEAM_NAME)
    graph = factory.get_team(team, mode="supervisor")

    result = await graph.ainvoke({"task": "任务"}, {"configurable": {"thread_id": "team-2"}})
    record = result.get("record") or {}

    assert list(record) == list(team.members)


async def test_parallel_team_runs_all_members(
    registry: ExpertRegistry, settings: Settings, tool_registry: ToolRegistry, mock_llm_factory
):
    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    team = registry.get_team(TEAM_NAME)
    graph = factory.get_team(team, mode="parallel")

    result = await graph.ainvoke(
        {"task": "写一篇财经稿"}, {"configurable": {"thread_id": "team-p"}}
    )

    record = result.get("record") or {}
    assert set(record) == set(team.members)
    assert result.get("status") == "ok"
    assert result.get("report")


async def test_team_result_uses_contract(
    registry: ExpertRegistry, settings: Settings, tool_registry: ToolRegistry, mock_llm_factory
):
    from codeassistant.contracts import merge_team_outputs

    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    team = registry.get_team(TEAM_NAME)
    graph = factory.get_team(team, mode="supervisor")

    result = await graph.ainvoke({"task": "任务"}, {"configurable": {"thread_id": "team-3"}})
    merged = merge_team_outputs(team.name, result.get("record"), None)

    assert merged["team"] == team.name
    assert merged["status"] in ("ok", "partial", "need_human", "failed")
    assert set(merged["deliverables"]) == set(team.members)


async def test_team_with_single_member(
    registry: ExpertRegistry, settings: Settings, tool_registry: ToolRegistry, mock_llm_factory
):
    from dataclasses import replace

    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    team = replace(registry.get_team(TEAM_NAME), members=("finance-data-collector",))
    graph = factory.get_team(team, mode="supervisor")

    result = await graph.ainvoke({"task": "任务"}, {"configurable": {"thread_id": "team-4"}})
    assert set(result.get("record") or {}) == {"finance-data-collector"}


async def test_team_members_do_not_clobber_each_other(
    registry: ExpertRegistry, settings: Settings, tool_registry: ToolRegistry, mock_llm_factory
):
    """并行模式下多个成员写回 record，不能互相覆盖。"""

    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    team = registry.get_team(TEAM_NAME)
    graph = factory.get_team(team, mode="parallel")

    result = await graph.ainvoke({"task": "任务"}, {"configurable": {"thread_id": "team-5"}})
    record = result.get("record") or {}

    assert len(record) == len(team.members)
    assert all(name in record for name in team.members)


def test_team_config_hash_reflects_members(
    registry: ExpertRegistry, settings: Settings, tool_registry: ToolRegistry, mock_llm_factory
):
    from dataclasses import replace

    team = registry.get_team(TEAM_NAME)
    changed = replace(team, members=("finance-data-collector",))
    assert team.config_hash() != changed.config_hash()
