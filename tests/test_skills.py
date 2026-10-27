"""技能（Skill）系统测试。"""

from __future__ import annotations

import json
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from codeassistant.approval import ApprovalCenter
from codeassistant.configs import HitlPolicy
from codeassistant.graphs.expert import (
    _build_task_prompt,
    build_expert_graph,
    match_skill_commands,
)
from codeassistant.llm import MockChat
from codeassistant.registry import ExpertRegistry
from codeassistant.settings import Settings
from codeassistant.skills import Skill, SkillRegistry

# --------------------------------------------------------------------------
# 发现
# --------------------------------------------------------------------------


def _write_skill(root: Path, name: str, body: str, frontmatter: str = "") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


def test_discovers_skill_directory_layout(tmp_path: Path):
    _write_skill(
        tmp_path,
        "demo-skill",
        "这是正文说明。",
        "name: demo-skill\ndescription: 演示技能",
    )
    registry = SkillRegistry.load(global_dir=tmp_path, packs=[])

    skill = registry.get("demo-skill")
    assert skill is not None
    assert skill.description == "演示技能"
    assert "这是正文说明" in skill.instructions


def test_discovers_single_file_layout(tmp_path: Path):
    (tmp_path / "flat-skill.md").write_text(
        "---\nname: flat-skill\ndescription: 单文件技能\n---\n\n正文\n",
        encoding="utf-8",
    )
    registry = SkillRegistry.load(global_dir=tmp_path, packs=[])

    skill = registry.get("flat-skill")
    assert skill is not None
    assert skill.description == "单文件技能"


def test_detects_scripts_and_references(tmp_path: Path):
    directory = tmp_path / "with-script"
    (directory / "scripts").mkdir(parents=True)
    (directory / "references").mkdir(parents=True)
    (directory / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")
    (directory / "references" / "guide.md").write_text("参考", encoding="utf-8")
    (directory / "SKILL.md").write_text(
        "---\nname: with-script\ndescription: 带脚本的技能\n---\n\n正文\n",
        encoding="utf-8",
    )

    registry = SkillRegistry.load(global_dir=tmp_path, packs=[])
    skill = registry.get("with-script")

    assert skill is not None
    assert skill.scripts == ("run.py",)
    assert skill.references == ("guide.md",)


def test_pack_skills_are_discovered(tmp_path: Path, settings: Settings):
    from codeassistant.loader import load_pack

    pack_root = tmp_path / "plugins" / "demo" / ".codebuddy-plugin"
    (pack_root).mkdir(parents=True)
    (pack_root / "plugin.json").write_text(
        json.dumps({"name": "demo", "expertType": "agent", "agentName": "demo-worker"}),
        encoding="utf-8",
    )
    agents = tmp_path / "plugins" / "demo" / "agents"
    agents.mkdir(parents=True)
    (agents / "demo-worker.md").write_text(
        "---\nname: demo-worker\ndescription: d\nmaxTurns: 20\n---\n\n提示词\n",
        encoding="utf-8",
    )
    _write_skill(
        tmp_path / "plugins" / "demo" / "skills",
        "pack-skill",
        "包内技能",
        "name: pack-skill\ndescription: 包内的技能",
    )

    pack = load_pack(tmp_path / "plugins" / "demo")
    registry = SkillRegistry.load(global_dir=None, packs=[pack])

    skill = registry.get("pack-skill")
    assert skill is not None
    assert skill.source == "demo"


# --------------------------------------------------------------------------
# 绑定
# --------------------------------------------------------------------------


def test_binding_by_agents_field():
    registry = SkillRegistry(
        skills=[
            Skill(name="a", agents=("x",)),
            Skill(name="b", default=True),
            Skill(name="c"),
        ]
    )

    assert [s.name for s in registry.bind("x")] == ["a", "b"]
    assert [s.name for s in registry.bind("y")] == ["b"]
    # 没声明规则的技能只归通用型专家
    assert [s.name for s in registry.bind("code-assistant")] == ["b", "c"]


def test_binding_by_explicit_list():
    registry = SkillRegistry(skills=[Skill(name="c")])
    assert [s.name for s in registry.bind("y", explicit=["c"])] == ["c"]


def test_registry_binds_skills_to_experts(registry: ExpertRegistry):
    backend = registry.get_expert("sdt-backend")
    reviewer = registry.get_expert("finance-article-writer")

    assert "code-review" in backend.skills
    assert "code-review" not in reviewer.skills, "不相关专家不该被绑上代码技能"


def test_pack_override_can_bind_skills(tmp_path: Path, tool_registry):
    from codeassistant.loader import load_pack

    pack_root = tmp_path / "plugins" / "demo" / ".codebuddy-plugin"
    pack_root.mkdir(parents=True)
    (pack_root / "plugin.json").write_text(
        json.dumps({"name": "demo", "expertType": "agent", "agentName": "demo-worker"}),
        encoding="utf-8",
    )
    agents = tmp_path / "plugins" / "demo" / "agents"
    agents.mkdir(parents=True)
    (agents / "demo-worker.md").write_text(
        "---\nname: demo-worker\ndescription: d\nmaxTurns: 20\n---\n\n提示词\n",
        encoding="utf-8",
    )
    (tmp_path / "plugins" / "demo" / "agent-tools.json").write_text(
        json.dumps({"demo-worker": {"skills": ["special"]}}), encoding="utf-8"
    )
    _write_skill(
        tmp_path / "plugins" / "demo" / "skills",
        "special",
        "专用技能",
        "name: special\ndescription: 专用",
    )

    pack = load_pack(tmp_path / "plugins" / "demo")
    local_settings = Settings.load(
        workspace=tmp_path,
        experts_dir=tmp_path / "plugins",
        provider="mock",
        checkpointer="memory",
    )
    local_registry = ExpertRegistry(local_settings, tools=tool_registry, packs=[pack])

    assert "special" in local_registry.get_expert("demo-worker").skills


# --------------------------------------------------------------------------
# 与专家图的集成
# --------------------------------------------------------------------------


def _skill_with_script(root: Path) -> Skill:
    directory = root / "stats-skill"
    (directory / "scripts").mkdir(parents=True, exist_ok=True)
    (directory / "scripts" / "count.py").write_text(
        "print('script-ran')\n", encoding="utf-8"
    )
    path = directory / "SKILL.md"
    path.write_text(
        "---\nname: stats-skill\ndescription: 统计技能\n---\n\n请先运行脚本再下结论。\n",
        encoding="utf-8",
    )
    return Skill(
        name="stats-skill",
        description="统计技能",
        instructions="请先运行脚本再下结论。",
        path=path,
        scripts=("count.py",),
    )


async def test_skill_index_is_in_system_prompt(make_expert_config, tool_registry, tmp_path):
    """技能清单进系统提示（只放名称+描述，正文按需载入）。"""

    skill = _skill_with_script(tmp_path)
    config = make_expert_config("with-skill", tools=("list_skills",))

    captured: list[str] = []

    class CapturingLLM(MockChat):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            captured.append(str(messages[0].content))
            return super()._generate(messages, stop, run_manager, **kwargs)

    graph = build_expert_graph(
        config,
        llm=CapturingLLM(model="mock", script=[{"content": "结论"}]),
        tool_registry=tool_registry,
        checkpointer=InMemorySaver(),
        skills=[skill],
    )
    await graph.ainvoke({"task": "t"}, {"configurable": {"thread_id": "idx"}})

    assert captured
    assert "可用技能" in captured[0]
    assert "stats-skill" in captured[0]
    assert "请先运行脚本再下结论" not in captured[0], "正文不应进系统提示"


async def test_load_skill_returns_instructions(make_expert_config, tool_registry, tmp_path):
    skill = _skill_with_script(tmp_path)
    config = make_expert_config("loader", tools=("list_skills", "load_skill"))
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {"tool_calls": [{"name": "load_skill", "args": {"skill_name": "stats-skill"}, "id": "c1"}]},
            {"content": "读完了"},
        ],
    )
    graph = build_expert_graph(
        config,
        llm=llm,
        tool_registry=tool_registry,
        checkpointer=InMemorySaver(),
        skills=[skill],
    )

    await graph.ainvoke({"task": "t"}, {"configurable": {"thread_id": "load"}})
    state = await graph.aget_state({"configurable": {"thread_id": "load"}})
    texts = [str(m.content) for m in state.values.get("messages") or []]
    assert any("请先运行脚本再下结论" in text for text in texts)


async def test_load_skill_refuses_unbound_skill(make_expert_config, tool_registry, tmp_path):
    skill = _skill_with_script(tmp_path)
    config = make_expert_config("loader2", tools=("list_skills", "load_skill"))
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {"tool_calls": [{"name": "load_skill", "args": {"skill_name": "other"}, "id": "c1"}]},
            {"content": "结束"},
        ],
    )
    graph = build_expert_graph(
        config,
        llm=llm,
        tool_registry=tool_registry,
        checkpointer=InMemorySaver(),
        skills=[skill],
    )

    await graph.ainvoke({"task": "t"}, {"configurable": {"thread_id": "load2"}})
    state = await graph.aget_state({"configurable": {"thread_id": "load2"}})
    texts = [str(m.content) for m in state.values.get("messages") or []]
    assert any("不可用" in text for text in texts)


async def test_run_skill_script_requires_approval(
    make_expert_config, tool_registry, tmp_path
):
    """技能脚本属于高危操作：必须人工审批后才执行。"""

    skill = _skill_with_script(tmp_path)
    config = make_expert_config(
        "runner",
        tools=("list_skills", "load_skill"),
        hitl=HitlPolicy(tools=(), mode="dynamic"),
    )
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {
                "tool_calls": [
                    {
                        "name": "run_skill_script",
                        "args": {"skill_name": "stats-skill", "script": "count.py"},
                        "id": "c1",
                    }
                ]
            },
            {"content": "跑完了"},
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
    run_config = {"configurable": {"thread_id": "script-1"}}

    async for _ in graph.astream({"task": "跑脚本"}, run_config, stream_mode=["updates"]):
        pass

    pending = center.pending()
    assert pending, "技能脚本应触发人工审批"
    assert pending[0].tool == "run_skill_script"

    center.resolve(pending[0].id, approved=True, note="允许")
    async for _ in graph.astream(
        Command(resume={"approved": True, "reason": "允许"}),
        run_config,
        stream_mode=["updates"],
    ):
        pass

    state = await graph.aget_state(run_config)
    texts = [str(m.content) for m in state.values.get("messages") or []]
    assert any("script-ran" in text for text in texts)


async def test_run_skill_script_rejects_unknown_script(
    make_expert_config, tool_registry, tmp_path
):
    skill = _skill_with_script(tmp_path)
    config = make_expert_config(
        "runner2",
        tools=("list_skills", "load_skill"),
        hitl=HitlPolicy(tools=(), mode="dynamic"),
    )
    llm = MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {
                "tool_calls": [
                    {
                        "name": "run_skill_script",
                        "args": {"skill_name": "stats-skill", "script": "../../etc/passwd"},
                        "id": "c1",
                    }
                ]
            },
            {"content": "结束"},
        ],
    )
    graph = build_expert_graph(
        config,
        llm=llm,
        tool_registry=tool_registry,
        checkpointer=InMemorySaver(),
        skills=[skill],
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
    run_config = {"configurable": {"thread_id": "script-2"}}
    async for _ in graph.astream({"task": "t"}, run_config, stream_mode=["updates"]):
        pass
    pending = center.pending()
    assert pending, "技能脚本应触发人工审批"
    center.resolve(pending[0].id, approved=True, note="允许")
    async for _ in graph.astream(
        Command(resume={"approved": True, "reason": "允许"}),
        run_config,
        stream_mode=["updates"],
    ):
        pass
    state = await graph.aget_state(run_config)
    texts = [str(m.content) for m in state.values.get("messages") or []]
    assert any("没有脚本" in text or "不在技能目录内" in text for text in texts)


def test_skill_render_index(make_expert_config):
    skills = [Skill(name="a", description="甲"), Skill(name="b", description="乙")]
    index = SkillRegistry.render_index(skills)
    assert "可用技能" in index
    assert "- a：甲" in index
    assert SkillRegistry.render_index([]) == ""


# --------------------------------------------------------------------------
# ``/技能名`` —— 输入框里点选技能后写进任务的那一串
# --------------------------------------------------------------------------


def test_match_skill_commands_finds_named_skill():
    skills = [Skill(name="stats-skill"), Skill(name="other")]
    matched = match_skill_commands("/stats-skill 帮我统计", skills)
    assert [s.name for s in matched] == ["stats-skill"]


def test_match_skill_commands_ignores_unbound_names():
    """没绑定的技能名不该被带进来。"""

    skills = [Skill(name="stats-skill")]
    assert match_skill_commands("/not-bound 做点事", skills) == []


def test_match_skill_commands_ignores_path_like_slash():
    """路径里的斜杠不是技能调用。"""

    skills = [Skill(name="usr"), Skill(name="stats-skill")]
    assert match_skill_commands("看看 /usr/local/bin", skills) == []


def test_task_prompt_carries_invoked_skill():
    skill = Skill(name="stats-skill", description="统计技能", instructions="请先运行脚本再下结论。")
    prompt = _build_task_prompt({"task": "/stats-skill 帮我统计"}, [skill])
    assert "已载入技能说明" in prompt
    assert "请先运行脚本再下结论。" in prompt
    assert "任务：/stats-skill 帮我统计" in prompt, "原始任务文本要保留"


def test_task_prompt_without_invocation_stays_lean():
    """没点技能时，任务提示里不该多出技能正文（渐进式披露）。"""

    skill = Skill(name="stats-skill", instructions="请先运行脚本再下结论。")
    prompt = _build_task_prompt({"task": "帮我统计"}, [skill])
    assert "已载入技能说明" not in prompt


async def test_slash_command_reaches_the_model(
    make_expert_config, tool_registry, tmp_path
):
    """端到端：任务里的 /技能名 会把技能说明带进给模型的提示。"""

    skill = _skill_with_script(tmp_path)
    config = make_expert_config("slashed", tools=("list_skills",))

    prompts: list[str] = []

    class CapturingLLM(MockChat):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            prompts.append(str(messages[-1].content))
            return super()._generate(messages, stop, run_manager, **kwargs)

    graph = build_expert_graph(
        config,
        llm=CapturingLLM(model="mock", script=[{"content": "计划"}, {"content": "结论"}]),
        tool_registry=tool_registry,
        checkpointer=InMemorySaver(),
        skills=[skill],
    )
    await graph.ainvoke(
        {"task": "/stats-skill 帮我统计"}, {"configurable": {"thread_id": "slash"}}
    )

    assert prompts, "模型应该被调用过"
    assert any("已载入技能说明" in text for text in prompts)
    assert any("请先运行脚本再下结论。" in text for text in prompts)


async def test_slash_command_not_bound_is_not_injected(
    make_expert_config, tool_registry, tmp_path
):
    """没绑定给该专家的技能，点不动也不该被注入。"""

    skill = _skill_with_script(tmp_path)
    config = make_expert_config("noslash", tools=("list_skills",))

    prompts: list[str] = []

    class CapturingLLM(MockChat):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            prompts.append(str(messages[-1].content))
            return super()._generate(messages, stop, run_manager, **kwargs)

    graph = build_expert_graph(
        config,
        llm=CapturingLLM(model="mock", script=[{"content": "计划"}, {"content": "结论"}]),
        tool_registry=tool_registry,
        checkpointer=InMemorySaver(),
        skills=[],  # 没绑定任何技能
    )
    await graph.ainvoke(
        {"task": "/stats-skill 帮我统计"}, {"configurable": {"thread_id": "noslash"}}
    )

    assert not any("已载入技能说明" in text for text in prompts)
