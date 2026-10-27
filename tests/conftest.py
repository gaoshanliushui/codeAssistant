"""测试夹具。

全部测试都不触网、不需要 API Key：模型用 :class:`MockChat` 脚本驱动，
沙箱用 pytest 的临时目录，检查点用内存实现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeassistant.llm import MockChat
from codeassistant.registry import ExpertRegistry
from codeassistant.settings import EXPERTS_DIR, Settings
from codeassistant.tools import Sandbox, ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """内存检查点 + mock 模型的配置。"""

    return Settings.load(
        workspace=tmp_path,
        experts_dir=EXPERTS_DIR,
        provider="mock",
        checkpointer="memory",
        # 会话记录写进临时目录，别污染仓库里的 .codeassistant/
        sessions_file=tmp_path / "sessions.json",
    )


@pytest.fixture
def sandbox(settings: Settings) -> Sandbox:
    return Sandbox(settings.workspace)


@pytest.fixture
def tool_registry(sandbox: Sandbox) -> ToolRegistry:
    return ToolRegistry(sandbox)


@pytest.fixture
def registry(settings: Settings, tool_registry: ToolRegistry) -> ExpertRegistry:
    return ExpertRegistry(settings, tools=tool_registry)


@pytest.fixture
def mock_llm_factory():
    """按专家名返回确定性的假模型。"""

    def factory(config):
        return MockChat(
            model="mock",
            tag=config.name,
            script=[
                {"content": f"{config.name}：执行计划"},
                {"content": f"{config.name}：最终结论"},
            ],
        )

    return factory


@pytest.fixture
def make_expert_config():
    """构造测试用专家配置。"""

    from codeassistant.configs import ExpertConfig, HitlPolicy

    def _make(
        name: str = "demo-expert",
        *,
        tools: tuple[str, ...] = ("read_file", "search_code"),
        hitl: HitlPolicy | None = None,
        max_turns: int = 5,
    ) -> ExpertConfig:
        return ExpertConfig(
            name=name,
            display_name=name,
            profession="测试专家",
            description="单元测试用",
            system_prompt="你是一个测试专家。",
            tools=tools,
            hitl=hitl or HitlPolicy(),
            max_turns=max_turns,
            source="test",
        )

    return _make
