"""工具注册表。

集中维护「工具 → 风险等级 → 是否需要人工审批」的映射，
并按专家的工具白名单裁剪出可用的工具集合。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from ..configs import ToolSpec
from .code_tools import build_code_tools
from .git_tools import build_git_tools
from .sandbox import Sandbox

#: 工具声明（名称 / 描述 / 风险 / 是否需要审批 / 标签）
TOOL_SPECS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec(
            name="read_file",
            description="读取工作区内的文本文件内容，返回带行号的文本。",
            risk="low",
            tags=("read", "code"),
        ),
        ToolSpec(
            name="write_file",
            description="写入（覆盖）工作区内的文件。",
            risk="high",
            requires_approval=True,
            tags=("write", "code"),
        ),
        ToolSpec(
            name="edit_file",
            description="对工作区内的文件做精确字符串替换。",
            risk="high",
            requires_approval=True,
            tags=("write", "code"),
        ),
        ToolSpec(
            name="delete_file",
            description="删除工作区内的文件。",
            risk="high",
            requires_approval=True,
            tags=("write", "code"),
        ),
        ToolSpec(
            name="list_files",
            description="列出工作区内某个目录下的文件与子目录。",
            risk="low",
            tags=("read", "explore"),
        ),
        ToolSpec(
            name="search_code",
            description="在工作区内按字面量搜索代码。",
            risk="low",
            tags=("read", "explore"),
        ),
        ToolSpec(
            name="run_command",
            description="在工作区内执行一条 shell 命令。",
            risk="high",
            requires_approval=True,
            tags=("exec",),
        ),
        ToolSpec(
            name="ast_inspect",
            description="解析 Python 文件，列出类/函数/方法及行号。",
            risk="low",
            tags=("read", "code"),
        ),
        ToolSpec(
            name="git_status",
            description="查看工作区内 Git 仓库的当前状态。",
            risk="low",
            tags=("read", "git"),
        ),
        ToolSpec(
            name="git_diff",
            description="查看工作区内 Git 仓库的代码差异。",
            risk="low",
            tags=("read", "git"),
        ),
    ]
}

#: 未指定白名单时给专家的最小安全工具集
DEFAULT_TOOLS = ("read_file", "list_files", "search_code", "ast_inspect")


class ToolRegistry:
    """可实例化的工具集合。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self._tools: dict[str, BaseTool] = {}
        for tool in [*build_code_tools(sandbox), *build_git_tools(sandbox)]:
            self._tools[tool.name] = tool

    # -- 查询 -------------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self._tools)

    def spec(self, name: str) -> ToolSpec | None:
        return TOOL_SPECS.get(name)

    def specs(self) -> list[ToolSpec]:
        return [TOOL_SPECS[name] for name in self.names() if name in TOOL_SPECS]

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def for_expert(self, names: tuple[str, ...] | list[str]) -> list[BaseTool]:
        """按白名单裁剪出工具列表（忽略未知工具名）。"""

        if not names:
            names = DEFAULT_TOOLS
        return [self._tools[name] for name in names if name in self._tools]

    def approval_tools(self) -> tuple[str, ...]:
        """全部默认需要审批的工具。"""

        return tuple(
            spec.name for spec in TOOL_SPECS.values() if spec.requires_approval
        )

    def high_risk_tools(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in TOOL_SPECS.values() if spec.risk == "high")
