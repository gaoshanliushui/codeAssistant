"""专家注册表。

把 ``.codeassistant/my-experts/plugins/*`` 里的专家包转换成声明式的
:class:`~codeassistant.configs.ExpertConfig` / :class:`~codeassistant.configs.TeamConfig`，
并负责：

* 工具绑定（三级策略：包内覆盖文件 → 职位关键词 → 默认安全集）
* 人审点推导（白名单里的高危工具自动进入审批清单）
* 热重载（重新读取专家包，变化的配置失效、没变的保留）
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .builtin import (
    CODE_ASSISTANT_DESCRIPTION,
    CODE_ASSISTANT_PROMPT,
    CODE_ASSISTANT_TOOLS,
)
from .configs import ExpertConfig, HitlPolicy, TeamConfig, ToolSpec
from .loader import LoadedPack, load_packs
from .settings import Settings
from .skills import Skill, SkillRegistry
from .tools.registry import DEFAULT_TOOLS, TOOL_SPECS, ToolRegistry

#: 职位关键词 → 工具白名单（按顺序匹配，先命中先用）
TOOL_BINDINGS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("架构", "architect", "架构师"),
        ("read_file", "list_files", "search_code", "ast_inspect", "git_status", "git_diff"),
    ),
    (
        ("前端", "frontend"),
        ("read_file", "list_files", "search_code", "ast_inspect", "write_file", "edit_file", "git_diff"),
    ),
    (
        ("后端", "backend"),
        (
            "read_file",
            "list_files",
            "search_code",
            "ast_inspect",
            "write_file",
            "edit_file",
            "run_command",
            "git_diff",
        ),
    ),
    (
        ("测试", "qa", "质量"),
        ("read_file", "list_files", "search_code", "ast_inspect", "run_command"),
    ),
    (
        ("运维", "devops", "sre"),
        ("read_file", "list_files", "search_code", "run_command", "git_status", "git_diff"),
    ),
    (
        ("需求", "requirement", "产品"),
        ("read_file", "list_files", "search_code"),
    ),
    (
        ("主理", "lead", "总监", "主编", "director", "负责人"),
        ("read_file", "list_files", "search_code", "git_status"),
    ),
)

#: ReAct 循环的迭代上限（agent md 里的 maxTurns 是会话轮数，语义不同）
MAX_AGENT_ITERATIONS = 10


@dataclass
class ReloadReport:
    """热重载结果。"""

    added_experts: list[str] = field(default_factory=list)
    removed_experts: list[str] = field(default_factory=list)
    changed_experts: list[str] = field(default_factory=list)
    added_teams: list[str] = field(default_factory=list)
    removed_teams: list[str] = field(default_factory=list)
    changed_teams: list[str] = field(default_factory=list)

    def touched_experts(self) -> list[str]:
        return sorted(set(self.added_experts) | set(self.changed_experts) | set(self.removed_experts))

    def touched_teams(self) -> list[str]:
        return sorted(set(self.added_teams) | set(self.changed_teams) | set(self.removed_teams))

    def summary(self) -> str:
        return (
            f"专家 +{len(self.added_experts)} / ~{len(self.changed_experts)} "
            f"/ -{len(self.removed_experts)}；"
            f"专家团 +{len(self.added_teams)} / ~{len(self.changed_teams)} "
            f"/ -{len(self.removed_teams)}"
        )


def _match_keywords(*texts: str) -> str:
    """返回命中的绑定规则标识（未命中返回空串）。"""

    haystack = " ".join(t.lower() for t in texts if t)
    for keywords, _ in TOOL_BINDINGS:
        if any(keyword.lower() in haystack for keyword in keywords):
            return keywords[0]
    return ""


def _load_overrides(pack: LoadedPack) -> dict[str, dict[str, Any]]:
    """读取包内可选的 ``agent-tools.json`` 覆盖文件。"""

    path = pack.root / "agent-tools.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


class ExpertRegistry:
    """所有可用专家 / 专家团的索引。"""

    def __init__(
        self,
        settings: Settings,
        tools: ToolRegistry | None = None,
        packs: Iterable[LoadedPack] | None = None,
        skills: SkillRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.tools = tools

        self.packs: list[LoadedPack] = list(packs) if packs is not None else load_packs(settings.experts_dir)
        if packs is None and not settings.experts_dir.is_dir():
            # 目录不存在时只会剩下内置专家，这里明确提示，避免"专家凭空消失"
            print(
                f"⚠️  专家包目录不存在：{settings.experts_dir}\n"
                f"    仅加载内置专家 code-assistant；"
                f"检查 CODEASSISTANT_EXPERTS_DIR 或 .env 里的相对路径。",
                file=sys.stderr,
            )
        self.skills = (
            skills
            if skills is not None
            else SkillRegistry.load(global_dir=settings.skills_dir, packs=self.packs)
        )
        self.experts: dict[str, ExpertConfig] = {}
        self.teams: dict[str, TeamConfig] = {}
        self._build()

    # -- 构建 -------------------------------------------------------------

    def _build(self) -> None:
        self.experts = {}
        self.teams = {}
        self._register_builtin()
        for pack in self.packs:
            overrides = _load_overrides(pack)
            for agent_name, doc in pack.agents.items():
                config = self._expert_from_doc(pack, doc, overrides.get(agent_name, {}))
                self.experts[config.name] = config
            if pack.is_team:
                team = self._team_from_pack(pack)
                if team is not None:
                    self.teams[team.name] = team

    def _register_builtin(self) -> None:
        hitl = tuple(
            name
            for name in CODE_ASSISTANT_TOOLS
            if (spec := TOOL_SPECS.get(name)) and spec.requires_approval
        )
        self.experts["code-assistant"] = ExpertConfig(
            name="code-assistant",
            display_name="代码助手",
            profession="代码助手",
            description=CODE_ASSISTANT_DESCRIPTION,
            system_prompt=CODE_ASSISTANT_PROMPT,
            tools=CODE_ASSISTANT_TOOLS,
            skills=tuple(skill.name for skill in self.skills.bind("code-assistant")),
            tags=("代码检索", "代码修改", "问题定位"),
            memory_scope="session",
            hitl=HitlPolicy(tools=hitl, mode=self.settings.approval_mode),
            max_turns=MAX_AGENT_ITERATIONS,
            source="builtin",
        )

    def _expert_from_doc(
        self,
        pack: LoadedPack,
        doc: Any,
        override: dict[str, Any],
    ) -> ExpertConfig:
        tools = self._resolve_tools(pack, doc, override)
        hitl_tools = tuple(
            name for name in tools if (spec := TOOL_SPECS.get(name)) and spec.requires_approval
        )
        hitl_override = override.get("hitl") or {}
        mode = hitl_override.get("mode", self.settings.approval_mode)
        default_action = hitl_override.get("default_action", "ask")

        tags = pack.plugin.get("tags") or []
        tag_list = tuple(
            str(tag.get("zh") or tag.get("en") or tag) if isinstance(tag, dict) else str(tag)
            for tag in tags
        )
        if doc.profession:
            tag_list = tuple(dict.fromkeys((*tag_list, doc.title())))

        # 绑定技能：包内 agent-tools.json 显式声明的 + frontmatter 声明绑定到本专家的
        explicit_skills = [str(name) for name in (override.get("skills") or [])]
        bound = self.skills.bind(doc.name, explicit=explicit_skills)

        return ExpertConfig(
            name=doc.name,
            display_name=doc.label(),
            profession=doc.title(),
            description=doc.description,
            system_prompt=doc.system_prompt,
            tools=tools,
            skills=tuple(skill.name for skill in bound),
            tags=tag_list,
            memory_scope="expert",
            hitl=HitlPolicy(
                tools=hitl_tools,
                mode=mode,
                default_action=default_action,
            ),
            max_turns=min(MAX_AGENT_ITERATIONS, max(3, doc.max_turns)),
            source=str(pack.root),
        )

    def _resolve_tools(
        self,
        pack: LoadedPack,
        doc: Any,
        override: dict[str, Any],
    ) -> tuple[str, ...]:
        explicit = override.get("tools")
        if explicit:
            return tuple(str(name) for name in explicit)

        matched = _match_keywords(doc.profession.get("zh", ""), doc.profession.get("en", ""), doc.name)
        for keywords, tools in TOOL_BINDINGS:
            if keywords[0] == matched:
                return tools
        return DEFAULT_TOOLS

    def _team_from_pack(self, pack: LoadedPack) -> TeamConfig | None:
        lead = pack.lead_name
        members = tuple(pack.member_names)
        if not lead or not members:
            return None
        overrides = _load_overrides(pack)
        team_override = overrides.get("_team") or {}
        mode = team_override.get("mode") or (
            "parallel"
            if self._looks_parallel(members)
            else self.settings.default_team_mode
        )
        max_rounds = int(team_override.get("max_rounds") or self.settings.max_team_rounds)
        return TeamConfig(
            name=pack.name,
            display_name=pack.display_name(),
            description=pack.description(),
            lead=lead,
            members=members,
            mode=mode,
            max_rounds=max_rounds,
            source=str(pack.root),
        )

    @staticmethod
    def _looks_parallel(members: tuple[str, ...]) -> bool:
        """启发式判断团队更适合并行模式。

        只有「成员少、且成员之间彼此独立（前后端）」的团队才默认走并行，
        其余按专家包自带的 SOP 走主理人调度。
        """

        independent = ("frontend", "backend", "前端", "后端")
        hits = sum(1 for member in members if any(tag in member.lower() for tag in independent))
        return hits >= 2 and len(members) <= 3

    # -- 查询 -------------------------------------------------------------

    def get_expert(self, name: str) -> ExpertConfig | None:
        return self.experts.get(name)

    def get_team(self, name: str) -> TeamConfig | None:
        return self.teams.get(name)

    def list_experts(self) -> list[ExpertConfig]:
        return [self.experts[name] for name in sorted(self.experts)]

    def list_teams(self) -> list[TeamConfig]:
        return [self.teams[name] for name in sorted(self.teams)]

    def team_members(self, team: TeamConfig) -> list[ExpertConfig]:
        return [self.experts[name] for name in team.members if name in self.experts]

    def tool_specs_for(self, config: ExpertConfig) -> list[ToolSpec]:
        return [TOOL_SPECS[name] for name in config.tools if name in TOOL_SPECS]

    def skills_for(self, config: ExpertConfig) -> list[Skill]:
        """某个专家实际绑定的技能对象。"""

        bound = self.skills.bind(config.name, explicit=config.skills)
        return bound

    # -- 热重载 -----------------------------------------------------------

    def reload(self, packs: Iterable[LoadedPack] | None = None) -> ReloadReport:
        """重新加载专家包并对比前后差异。"""

        old_experts = self.experts
        old_teams = self.teams
        self.packs = list(packs) if packs is not None else load_packs(self.settings.experts_dir)
        self._build()

        report = ReloadReport()
        for name, config in self.experts.items():
            if name not in old_experts:
                report.added_experts.append(name)
            elif old_experts[name].config_hash() != config.config_hash():
                report.changed_experts.append(name)
        for name in old_experts:
            if name not in self.experts and name != "code-assistant":
                report.removed_experts.append(name)

        for name, config in self.teams.items():
            if name not in old_teams:
                report.added_teams.append(name)
            elif old_teams[name].config_hash() != config.config_hash():
                report.changed_teams.append(name)
        for name in old_teams:
            if name not in self.teams:
                report.removed_teams.append(name)

        return report


def build_registry(settings: Settings, tools: ToolRegistry | None = None) -> ExpertRegistry:
    """构造专家注册表。"""

    return ExpertRegistry(settings, tools=tools)
