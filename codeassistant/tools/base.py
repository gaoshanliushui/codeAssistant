"""工具层基础类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..configs import ToolSpec  # 声明式配置统一放在 configs.py

__all__ = ["ToolSpec", "ToolError", "ToolResult", "SandboxViolation"]


class ToolError(Exception):
    """工具执行失败（可恢复，向 LLM 汇报原因即可）。"""


class SandboxViolation(ToolError):
    """越过工作区边界的操作，直接拒绝。"""


@dataclass
class ToolResult:
    """工具执行结果。"""

    ok: bool = True
    output: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, output: str, **meta: Any) -> ToolResult:
        return cls(ok=True, output=output, meta=meta)

    @classmethod
    def failure(cls, error: str, **meta: Any) -> ToolResult:
        return cls(ok=False, output="", error=error, meta=meta)

    def to_text(self) -> str:
        return self.output if self.ok else f"ERROR: {self.error}"
