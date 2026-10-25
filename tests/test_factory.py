"""图工厂：编译缓存与热更新。"""

from __future__ import annotations

from dataclasses import replace

from codeassistant.graphs.factory import ExpertGraphFactory
from codeassistant.registry import ExpertRegistry
from codeassistant.settings import Settings


def _factory(registry, settings, tool_registry, mock_llm_factory):
    return ExpertGraphFactory(
        registry,
        settings,
        tool_registry=tool_registry,
        llm_factory=mock_llm_factory,
    )


def test_factory_caches_by_name_and_hash(
    registry: ExpertRegistry, settings: Settings, tool_registry, mock_llm_factory
):
    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    config = registry.get_expert("sdt-backend")

    first = factory.get(config)
    second = factory.get(config)

    assert first is second
    assert factory.stats.hits == 1
    assert factory.stats.misses == 1


def test_factory_rebuilds_when_config_changes(
    registry: ExpertRegistry, settings: Settings, tool_registry, mock_llm_factory
):
    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    config = registry.get_expert("sdt-backend")

    first = factory.get(config)
    changed = replace(config, tools=("read_file",))
    second = factory.get(changed)

    assert first is not second
    assert len(factory.cached_keys()) == 2


def test_factory_invalidate_removes_entries(
    registry: ExpertRegistry, settings: Settings, tool_registry, mock_llm_factory
):
    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    config = registry.get_expert("sdt-backend")
    factory.get(config)

    removed = factory.invalidate("sdt-backend")

    assert removed
    assert factory.cached_keys() == []


def test_factory_reload_invalidates_changed_experts(
    registry: ExpertRegistry, settings: Settings, tool_registry, mock_llm_factory
):
    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    config = registry.get_expert("sdt-backend")
    factory.get(config)
    assert factory.cached_keys()

    report = factory.reload()

    assert isinstance(report.summary(), str)
    # 重新加载后缓存仍然可用
    factory.get(registry.get_expert("sdt-backend"))
    assert factory.stats.hits >= 1


def test_factory_get_team_supports_both_modes(
    registry: ExpertRegistry, settings: Settings, tool_registry, mock_llm_factory
):
    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    team = registry.get_team("toutiao-finance-account-ops-team")

    supervisor = factory.get_team(team, mode="supervisor")
    parallel = factory.get_team(team, mode="parallel")

    assert supervisor is not parallel
    assert factory.get_team(team, mode="supervisor") is supervisor


def test_factory_get_team_unknown_member_raises(
    registry: ExpertRegistry, settings: Settings, tool_registry, mock_llm_factory
):
    import pytest

    factory = _factory(registry, settings, tool_registry, mock_llm_factory)
    team = replace(registry.get_team("toutiao-finance-account-ops-team"), members=("nope",))

    with pytest.raises(ValueError):
        factory.get_team(team)
