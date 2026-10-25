"""图工厂：运行时动态装配 + 编译缓存 + 配置热更新。

核心机制：

* **配置 + 图工厂** —— 专家用声明式 :class:`ExpertConfig` 描述，
  工厂在运行时按需构建并编译子图（任务到达才装配）。
* **编译缓存** —— 以 ``(name, config_hash)`` 为键缓存已编译的图，
  相同配置不会重复编译。
* **热更新** —— 专家包变更后重新加载，受影响的缓存条目失效并重建，
  未变更的图继续复用。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from ..approval import ApprovalCenter
from ..configs import ExpertConfig, TeamConfig
from ..memory import NamespacedSaver, build_checkpointer
from ..observability import EventBus
from ..registry import ExpertRegistry, ReloadReport
from ..settings import Settings
from ..tools.registry import ToolRegistry
from ..tools.sandbox import Sandbox
from .expert import build_expert_graph

LLMFactory = Callable[[ExpertConfig], BaseChatModel]


@dataclass
class FactoryStats:
    """编译缓存统计。"""

    hits: int = 0
    misses: int = 0
    size: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "size": self.size}


class ExpertGraphFactory:
    """专家子图工厂。"""

    def __init__(
        self,
        registry: ExpertRegistry,
        settings: Settings,
        *,
        tool_registry: ToolRegistry | None = None,
        approval_center: ApprovalCenter | None = None,
        llm_factory: LLMFactory | None = None,
        team_llm_factory: Callable[[TeamConfig], BaseChatModel] | None = None,
        checkpointer: Any | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.tool_registry = (
            tool_registry
            or registry.tools
            or ToolRegistry(Sandbox(settings.workspace))
        )
        self.approval_center = approval_center or ApprovalCenter()
        self.event_bus = event_bus or EventBus()
        self._base_checkpointer = checkpointer if checkpointer is not None else build_checkpointer(settings)
        self._llm_factory = llm_factory
        self._team_llm_factory = team_llm_factory
        self._cache: dict[tuple[str, str], Any] = {}
        self._namespaces: dict[str, str] = {}
        self.stats = FactoryStats()

    # -- LLM --------------------------------------------------------------

    def _build_llm(self, config: ExpertConfig) -> BaseChatModel:
        if self._llm_factory is not None:
            return self._llm_factory(config)
        from ..llm import build_chat_model

        return build_chat_model(self.settings, model=config.model)

    # -- 装配 -------------------------------------------------------------

    def get(self, config: ExpertConfig) -> Any:
        """获取（必要时编译）某个专家的子图。"""

        key = config.cache_key
        cached = self._cache.get(key)
        if cached is not None:
            self.stats.hits += 1
            return cached

        self.stats.misses += 1
        namespace = self._namespaces.setdefault(config.name, f"expert:{config.name}")
        graph = build_expert_graph(
            config,
            llm=self._build_llm(config),
            tool_registry=self.tool_registry,
            approval_center=self.approval_center,
            checkpointer=NamespacedSaver(self._base_checkpointer, namespace),
        )
        self._cache[key] = graph
        self.stats.size = len(self._cache)
        return graph

    def get_by_name(self, name: str) -> Any | None:
        config = self.registry.get_expert(name)
        if config is None:
            return None
        return self.get(config)

    # -- 专家团 -----------------------------------------------------------

    def get_team(self, team: TeamConfig, mode: str | None = None) -> Any:
        """获取（必要时编译）专家团图。

        ``mode`` 覆盖专家包声明的协作模式：``supervisor``（主理人动态分派）
        或 ``parallel``（并行汇）。
        """

        from .parallel_team import build_parallel_team
        from .supervisor_team import build_supervisor_team

        effective_mode = mode or team.mode
        key = (f"team:{team.name}", f"{team.config_hash()}:{effective_mode}")
        cached = self._cache.get(key)
        if cached is not None:
            self.stats.hits += 1
            return cached

        self.stats.misses += 1
        members = self.registry.team_members(team)
        if not members:
            raise ValueError(f"专家团 {team.name} 没有任何可用成员。")

        from ..llm import build_chat_model

        llm = (
            self._team_llm_factory(team)
            if self._team_llm_factory is not None
            else build_chat_model(self.settings, model=team.model)
        )
        namespace = self._namespaces.setdefault(key[0], f"team:{team.name}")
        checkpointer = NamespacedSaver(self._base_checkpointer, namespace)

        builder = build_supervisor_team if effective_mode == "supervisor" else build_parallel_team
        graph = builder(
            team,
            members,
            llm=llm,
            factory=self,
            checkpointer=checkpointer,
        )
        self._cache[key] = graph
        self.stats.size = len(self._cache)
        return graph

    def get_team_by_name(self, name: str, mode: str | None = None) -> Any | None:
        team = self.registry.get_team(name)
        if team is None:
            return None
        return self.get_team(team, mode=mode)

    # -- 缓存管理 ---------------------------------------------------------

    def invalidate(self, name: str) -> list[str]:
        """失效某个专家的全部编译缓存，返回被清除的缓存键。"""

        removed = [key for key in self._cache if key[0] == name]
        for key in removed:
            self._cache.pop(key, None)
        self.stats.size = len(self._cache)
        return removed

    def invalidate_all(self) -> None:
        self._cache.clear()
        self.stats.size = 0

    def reload(self) -> ReloadReport:
        """热更新：重新加载专家包并失效受影响的缓存。"""

        report = self.registry.reload()
        for name in report.touched_experts():
            self.invalidate(name)
        return report

    def cached_keys(self) -> list[tuple[str, str]]:
        return sorted(self._cache)
