"""声明式配置模型。

专家（Expert）与专家团（Team）全部用不可变 dataclass 描述，
图工厂在运行时按需把它们编译成 LangGraph 子图。
配置内容变化 → ``config_hash`` 变化 → 编译缓存失效并热更新。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

Risk = Literal["low", "medium", "high"]
MemoryScope = Literal["session", "expert", "team"]
HitlMode = Literal["dynamic", "static"]
DefaultAction = Literal["ask", "approve", "reject"]
TeamMode = Literal["supervisor", "parallel"]


@dataclass(frozen=True)
class ToolSpec:
    """工具声明（风险分级 + 是否需要人工审批）。"""

    name: str
    description: str
    risk: Risk = "low"
    requires_approval: bool = False
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "requires_approval": self.requires_approval,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class HitlPolicy:
    """人审（Human-in-the-Loop）策略。

    * ``dynamic`` —— 在工具执行节点内对每个高危调用单独 ``interrupt()``，
      逐条通过/拒绝，支持从子图内部断点恢复。
    * ``static`` —— 编译时配置 ``interrupt_before=["execute_tools"]``，
      整批工具调用执行前统一放行。
    """

    tools: tuple[str, ...] = ()
    mode: HitlMode = "dynamic"
    default_action: DefaultAction = "ask"

    def needs_approval(self, tool_name: str) -> bool:
        return tool_name in self.tools

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "mode": self.mode,
            "default_action": self.default_action,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> HitlPolicy:
        data = data or {}
        return cls(
            tools=tuple(data.get("tools", ()) or ()),
            mode=data.get("mode", "dynamic"),
            default_action=data.get("default_action", "ask"),
        )


@dataclass(frozen=True)
class ExpertConfig:
    """单个专家的声明式配置。"""

    name: str
    display_name: str = ""
    profession: str = ""
    description: str = ""
    system_prompt: str = ""
    tools: tuple[str, ...] = ()
    #: 绑定的技能名（对应 :mod:`codeassistant.skills` 里的 Skill）
    skills: tuple[str, ...] = ()
    #: 描述性标签（来自专家包的 tags / 职位），仅用于展示
    tags: tuple[str, ...] = ()
    memory_scope: MemoryScope = "expert"
    hitl: HitlPolicy = field(default_factory=HitlPolicy)
    max_turns: int = 12
    model: str | None = None
    #: 编译期挂 ``interrupt_before=["execute_tools"]``，
    #: 在工具执行节点之前整体暂停一次。
    #: 注意：``interrupt_before`` 的暂停不会从嵌套子图向上冒泡，
    #: 因此该开关只建议在「专家作为顶层目标」运行时启用；
    #: 嵌套场景（专家团成员）请使用 :class:`HitlPolicy` 的 dynamic / static 模式。
    interrupt_before_tools: bool = False
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        """参与 hash 的规范化视图（顺序无关）。"""

        return {
            "name": self.name,
            "display_name": self.display_name,
            "profession": self.profession,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "tools": sorted(self.tools),
            "skills": sorted(self.skills),
            "tags": sorted(self.tags),
            "memory_scope": self.memory_scope,
            "hitl": self.hitl.to_dict(),
            "max_turns": self.max_turns,
            "model": self.model,
            "interrupt_before_tools": self.interrupt_before_tools,
        }

    def config_hash(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def cache_key(self) -> tuple[str, str]:
        """编译缓存键：(name, config_hash)。"""

        return (self.name, self.config_hash())


@dataclass(frozen=True)
class TeamConfig:
    """专家团的声明式配置。"""

    name: str
    display_name: str = ""
    description: str = ""
    lead: str = ""
    members: tuple[str, ...] = ()
    mode: TeamMode = "supervisor"
    max_rounds: int = 8
    model: str | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "lead": self.lead,
            "members": list(self.members),
            "mode": self.mode,
            "max_rounds": self.max_rounds,
            "model": self.model,
        }

    def config_hash(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def cache_key(self) -> tuple[str, str]:
        return (self.name, self.config_hash())

    def all_agents(self) -> tuple[str, ...]:
        """主理人 + 全部成员。"""

        if self.lead and self.lead not in self.members:
            return (self.lead, *self.members)
        return tuple(self.members)
