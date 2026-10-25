"""专家注册表：工具绑定、人审点推导、团队配置、热重载。"""

from __future__ import annotations

import json
from pathlib import Path

from codeassistant.registry import ExpertRegistry
from codeassistant.settings import Settings


def test_builtin_expert_always_available(registry: ExpertRegistry):
    config = registry.get_expert("code-assistant")
    assert config is not None
    assert "read_file" in config.tools
    assert "write_file" in config.hitl.tools


def test_repository_experts_are_loaded(registry: ExpertRegistry):
    for name in (
        "sdt-requirement",
        "sdt-architect",
        "sdt-frontend",
        "sdt-backend",
        "sdt-qa",
        "sdt-devops",
        "macro-economic-analyst",
    ):
        assert registry.get_expert(name) is not None, name


def test_teams_are_loaded_with_lead_and_members(registry: ExpertRegistry):
    team = registry.get_team("software-development-team")
    assert team is not None
    assert team.lead == "software-development-team-team-lead"
    assert len(team.members) == 6
    assert team.mode in ("supervisor", "parallel")


def test_tool_binding_by_profession(registry: ExpertRegistry):
    backend = registry.get_expert("sdt-backend")
    architect = registry.get_expert("sdt-architect")
    devops = registry.get_expert("sdt-devops")

    assert "write_file" in backend.tools
    assert "run_command" in backend.tools
    assert "write_file" not in architect.tools
    assert "run_command" in devops.tools


def test_hitl_derived_from_high_risk_tools(registry: ExpertRegistry):
    backend = registry.get_expert("sdt-backend")
    architect = registry.get_expert("sdt-architect")

    assert set(backend.hitl.tools) == {"write_file", "edit_file", "run_command"}
    assert architect.hitl.tools == ()


def test_expert_memory_scope_and_hash(registry: ExpertRegistry):
    config = registry.get_expert("sdt-backend")
    assert config.memory_scope == "expert"
    assert len(config.config_hash()) == 16
    assert config.config_hash() == registry.get_expert("sdt-backend").config_hash()


def test_config_hash_changes_with_tools(registry: ExpertRegistry):
    from dataclasses import replace

    config = registry.get_expert("sdt-backend")
    changed = replace(config, tools=("read_file",))
    assert changed.config_hash() != config.config_hash()


def test_reload_detects_changes(tmp_path: Path, settings: Settings, tool_registry):
    """改动专家包后 reload 能识别出变化的专家。"""

    pack_root = tmp_path / "packs" / "plugins" / "demo-team" / ".codebuddy-plugin"
    pack_root.mkdir(parents=True)
    agents_dir = tmp_path / "packs" / "plugins" / "demo-team" / "agents"
    agents_dir.mkdir(parents=True)

    (pack_root / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo-team",
                "expertType": "team",
                "agentName": "demo-lead",
                "teamInfo": {"leadAgent": "demo-lead", "memberAgents": ["demo-worker"]},
                "displayName": {"zh": "演示团"},
                "description": "演示",
            }
        ),
        encoding="utf-8",
    )
    for name in ("demo-lead", "demo-worker"):
        (agents_dir / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: d\ndisplayName:\n  zh: '{name}'\n"
            f"profession:\n  zh: '职位'\nmaxTurns: 20\n---\n\n提示词\n",
            encoding="utf-8",
        )

    local_settings = Settings.load(
        workspace=tmp_path,
        experts_dir=tmp_path / "packs",
        provider="mock",
        checkpointer="memory",
    )
    local_registry = ExpertRegistry(local_settings, tools=tool_registry)
    assert local_registry.get_expert("demo-worker") is not None
    before = local_registry.get_expert("demo-worker").config_hash()

    # 改提示词 → hash 变化
    (agents_dir / "demo-worker.md").write_text(
        "---\nname: demo-worker\ndescription: d\ndisplayName:\n  zh: 'demo-worker'\n"
        "profession:\n  zh: '职位'\nmaxTurns: 20\n---\n\n改了提示词\n",
        encoding="utf-8",
    )
    report = local_registry.reload()
    assert "demo-worker" in report.changed_experts
    assert local_registry.get_expert("demo-worker").config_hash() != before


def test_agent_tools_override_file(tmp_path: Path, tool_registry):
    """包内 agent-tools.json 可以覆盖工具绑定。"""

    pack_root = tmp_path / "plugins" / "demo" / ".codebuddy-plugin"
    pack_root.mkdir(parents=True)
    agents_dir = tmp_path / "plugins" / "demo" / "agents"
    agents_dir.mkdir(parents=True)

    (pack_root / "plugin.json").write_text(
        json.dumps({"name": "demo", "expertType": "agent", "agentName": "demo-worker"}),
        encoding="utf-8",
    )
    (agents_dir / "demo-worker.md").write_text(
        "---\nname: demo-worker\ndescription: d\nprofession:\n  zh: '后端工程师'\n"
        "maxTurns: 20\n---\n\n提示词\n",
        encoding="utf-8",
    )
    (tmp_path / "plugins" / "demo" / "agent-tools.json").write_text(
        json.dumps({"demo-worker": {"tools": ["read_file"]}}), encoding="utf-8"
    )

    local_settings = Settings.load(
        workspace=tmp_path,
        experts_dir=tmp_path,
        provider="mock",
        checkpointer="memory",
    )
    local_registry = ExpertRegistry(local_settings, tools=tool_registry)
    assert local_registry.get_expert("demo-worker").tools == ("read_file",)


def test_team_mode_override(tmp_path: Path, tool_registry):
    pack_root = tmp_path / "plugins" / "demo-team" / ".codebuddy-plugin"
    pack_root.mkdir(parents=True)
    agents_dir = tmp_path / "plugins" / "demo-team" / "agents"
    agents_dir.mkdir(parents=True)

    (pack_root / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo-team",
                "expertType": "team",
                "agentName": "demo-lead",
                "teamInfo": {"leadAgent": "demo-lead", "memberAgents": ["demo-worker"]},
            }
        ),
        encoding="utf-8",
    )
    for name in ("demo-lead", "demo-worker"):
        (agents_dir / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: d\nmaxTurns: 20\n---\n\n提示词\n",
            encoding="utf-8",
        )
    (tmp_path / "plugins" / "demo-team" / "agent-tools.json").write_text(
        json.dumps({"_team": {"mode": "parallel"}}), encoding="utf-8"
    )

    local_settings = Settings.load(
        workspace=tmp_path,
        experts_dir=tmp_path,
        provider="mock",
        checkpointer="memory",
    )
    local_registry = ExpertRegistry(local_settings, tools=tool_registry)
    assert local_registry.get_team("demo-team").mode == "parallel"
