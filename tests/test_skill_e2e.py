"""端到端：真实技能包（.codeassistant/skills）能被专家加载并执行。"""

from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from codeassistant.approval import ApprovalCenter
from codeassistant.graphs.expert import build_expert_graph
from codeassistant.llm import MockChat
from codeassistant.settings import EXPERTS_DIR
from codeassistant.skills import SkillRegistry

SKILLS_DIR = EXPERTS_DIR.parent / "skills"


@pytest.fixture
def skills() -> SkillRegistry:
    registry = SkillRegistry.load(global_dir=SKILLS_DIR, packs=[])
    if not registry.skills:
        pytest.skip("示例技能未安装")
    return registry


def test_example_skills_are_discovered(skills: SkillRegistry):
    names = skills.names()
    assert "code-stats" in names
    assert "code-review" in names
    assert "commit-message" in names


def test_code_stats_skill_ships_a_script(skills: SkillRegistry):
    skill = skills.get("code-stats")
    assert skill is not None
    assert "stats.py" in skill.scripts


async def test_code_stats_script_runs_after_approval(
    skills: SkillRegistry, tool_registry, tmp_path: Path
):
    """真实示例技能：脚本经人工审批后跑出统计结果。"""

    skill = skills.get("code-stats")
    assert skill is not None
    # 用临时目录当统计目标，避免扫到整个仓库
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    from codeassistant.configs import ExpertConfig, HitlPolicy

    config = ExpertConfig(
        name="stats-expert",
        display_name="统计专家",
        profession="测试",
        description="d",
        system_prompt="你是统计专家。",
        tools=("list_skills", "load_skill"),
        hitl=HitlPolicy(tools=(), mode="dynamic"),
        max_turns=5,
    )
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {
                "tool_calls": [
                    {
                        "name": "run_skill_script",
                        "args": {
                            "skill_name": "code-stats",
                            "script": "stats.py",
                            "arguments": f"--path {tmp_path} --top 3",
                        },
                        "id": "c1",
                    }
                ]
            },
            {"content": "统计完成"},
        ],
    )
    center = ApprovalCenter()
    graph = build_expert_graph(
        config,
        llm=llm,
        tool_registry=tool_registry,
        approval_center=center,
        checkpointer=InMemorySaver(),
        skills=[skill],
    )
    run_config = {"configurable": {"thread_id": "e2e-stats"}}

    async for _ in graph.astream({"task": "统计"}, run_config, stream_mode=["updates"]):
        pass

    pending = center.pending()
    assert pending, "执行技能脚本应触发人工审批"
    center.resolve(pending[0].id, approved=True, note="允许")

    async for _ in graph.astream(
        Command(resume={"approved": True, "reason": "允许"}),
        run_config,
        stream_mode=["updates"],
    ):
        pass

    state = await graph.aget_state(run_config)
    texts = [str(m.content) for m in state.values.get("messages") or []]
    assert any("Python" in text for text in texts), "脚本应输出语言统计"
    assert not state.values.get("findings"), f"不应有失败项: {state.values.get('findings')}"
