"""全局配置。

所有配置项都可以通过环境变量或项目根目录下的 `.env` 覆盖，
环境变量前缀统一为 ``CODEASSISTANT_``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Provider = Literal["openai", "anthropic", "mock"]
ApprovalMode = Literal["dynamic", "static"]
CheckpointerKind = Literal["memory", "sqlite"]

#: 项目根目录（codeassistant/ 的上一级）
PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

#: 应用数据目录：状态库与专家包都放在这里
APP_DIR = PROJECT_ROOT / ".codeassistant"
#: 专家包目录（``<APP_DIR>/my-experts``）
EXPERTS_DIR = APP_DIR / "my-experts"

DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
    "mock": "mock-code-assistant",
}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


@dataclass
class Settings:
    """运行时配置。"""

    provider: Provider = "mock"
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.0

    #: 代码助手可读写的根目录（沙箱边界）
    workspace: Path = field(default_factory=lambda: PROJECT_ROOT)
    #: 专家包目录
    experts_dir: Path = field(default_factory=lambda: EXPERTS_DIR)

    #: 状态持久化
    checkpointer: CheckpointerKind = "sqlite"
    state_db: Path = field(default_factory=lambda: APP_DIR / "state.db")

    #: 协作参数
    approval_mode: ApprovalMode = "dynamic"
    max_team_rounds: int = 8
    #: 团队默认协作模式
    default_team_mode: Literal["supervisor", "parallel"] = "supervisor"

    #: 运行模式
    verbose: bool = False

    @classmethod
    def load(
        cls,
        env_file: Path | str | None = None,
        *,
        workspace: Path | str | None = None,
        experts_dir: Path | str | None = None,
        provider: str | None = None,
        **overrides: object,
    ) -> Settings:
        """从 ``.env`` + 环境变量 + 显式入参构建配置（显式入参优先级最高）。"""

        env_path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

        provider_value = (provider or _env("CODEASSISTANT_PROVIDER", "mock") or "mock").lower()
        if provider_value not in ("openai", "anthropic", "mock"):
            raise ValueError(
                f"未知的 LLM provider: {provider_value!r}，可选 openai / anthropic / mock"
            )

        base = cls(
            provider=provider_value,  # type: ignore[arg-type]
            model=_env("CODEASSISTANT_MODEL") or DEFAULT_MODELS[provider_value],
            api_key=_env("CODEASSISTANT_API_KEY"),
            base_url=_env("CODEASSISTANT_BASE_URL"),
            temperature=float(_env("CODEASSISTANT_TEMPERATURE", "0.0") or 0.0),
            workspace=Path(workspace or _env("CODEASSISTANT_WORKSPACE", str(PROJECT_ROOT))).expanduser(),
            experts_dir=Path(
                experts_dir or _env("CODEASSISTANT_EXPERTS_DIR", str(EXPERTS_DIR))
            ).expanduser(),
            checkpointer=(_env("CODEASSISTANT_CHECKPOINTER", "sqlite") or "sqlite"),  # type: ignore[arg-type]
            state_db=Path(
                _env("CODEASSISTANT_STATE_DB", str(PROJECT_ROOT / ".codeassistant" / "state.db"))
            ).expanduser(),
            approval_mode=(_env("CODEASSISTANT_APPROVAL_MODE", "dynamic") or "dynamic"),  # type: ignore[arg-type]
            max_team_rounds=int(_env("CODEASSISTANT_MAX_TEAM_ROUNDS", "8") or 8),
            default_team_mode=(
                _env("CODEASSISTANT_DEFAULT_TEAM_MODE", "supervisor") or "supervisor"
            ),  # type: ignore[arg-type]
            verbose=(_env("CODEASSISTANT_VERBOSE", "") or "").lower() in ("1", "true", "yes"),
        )
        base.workspace = base.workspace.resolve()
        base.experts_dir = base.experts_dir.resolve()
        base.state_db = base.state_db.resolve()
        if overrides:
            base = replace(base, **overrides)  # type: ignore[arg-type]
        return base

    def with_workspace(self, workspace: Path | str) -> Settings:
        ws = Path(workspace).expanduser().resolve()
        return replace(self, workspace=ws)

    def describe(self) -> str:
        return (
            f"provider={self.provider} model={self.model} "
            f"workspace={self.workspace} experts={self.experts_dir} "
            f"checkpointer={self.checkpointer} approval={self.approval_mode}"
        )
