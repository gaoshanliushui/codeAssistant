"""代码助手工具集。

所有工具都绑定在一个 :class:`~codeassistant.tools.sandbox.Sandbox` 上，
路径越界直接抛错；高危工具（写文件 / 改文件 / 删文件 / 执行命令）
在 :mod:`codeassistant.tools.registry` 中被标记为需要人工审批。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from .base import ToolError, ToolResult
from .sandbox import Sandbox

#: 读取单个文件的最大字节数
MAX_READ_BYTES = 2 * 1024 * 1024
#: 单次搜索扫描的最大字节数
MAX_SEARCH_BYTES = 20 * 1024 * 1024
#: 搜索/列举时默认跳过的目录
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".idea",
    ".vscode",
}
#: 视作二进制、跳过全文搜索的文件后缀
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip", ".gz",
    ".tar", ".7z", ".rar", ".so", ".dylib", ".dll", ".exe", ".bin", ".class",
    ".jar", ".whl", ".mp3", ".mp4", ".mov", ".woff", ".woff2", ".ttf", ".otf",
}
#: 无论如何都不放行的命令（即便人工审批通过）
#:
#: 注意：命令黑名单只能挡住"明显"的破坏性写法，无法穷尽所有变形
#: （例如 base64 编码后解码执行）。真正的边界是「高危命令必须人工审批」。
FORBIDDEN_COMMAND_PATTERNS = (
    # rm 的递归/强制组合，目标为根目录（允许长选项与额外参数）
    r"\brm\b(?=[^|;&]*\s)(?=[^|;&]*(-[a-zA-Z]*[rf][a-zA-Z]*|--recursive|--force))"
    r"[^|;&]*\s/(\s|$|\*|--)",
    r":\(\)\s*\{.*\|.*&.*\}\s*;\s*:",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^|;]*\bof=/dev/",
    r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b",
    r">\s*/dev/(sd|nvme|hd)[a-z0-9]*",
    r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/(\s|$)",
    r"\bcurl\b[^|;]*\|\s*(ba)?sh\b",
    r"\bwget\b[^|;]*\|\s*(ba)?sh\b",
    r"\bhistory\s+-c\b",
    r"\bnvram\s+erase\b|\bdiskutil\s+erase",
)

#: 环境变量名里出现这些片段的，一律不传给子进程（避免把 API Key 泄露给模型）
SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def _sanitized_env() -> dict[str, str]:
    """构造传给子进程的环境变量：剔除敏感项。"""

    env: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if any(marker in upper for marker in SENSITIVE_ENV_MARKERS):
            continue
        env[name] = value
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _is_probably_binary(path: Path) -> bool:
    return path.suffix.lower() in BINARY_SUFFIXES


def _iter_files(root: Path, max_bytes: int = MAX_SEARCH_BYTES) -> list[Path]:
    """遍历目录下的文件，跳过符号链接指向的越界文件。

    ``os.walk`` 会列出指向文件符号链接，直接读取会读到工作区之外的内容，
    因此这里逐个校验解析后的真实路径。
    """

    collected: list[Path] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            path = Path(dirpath) / filename
            try:
                resolved = path.resolve()
                # 符号链接指向工作区之外的文件一律跳过
                if not resolved.is_relative_to(root.resolve()):
                    continue
                size = resolved.stat().st_size
            except OSError:
                continue
            if size > MAX_READ_BYTES:
                continue
            total += size
            collected.append(path)
            if total > max_bytes:
                return collected
    return collected


# ---------------------------------------------------------------------------
# 工厂：每个工具都闭包绑定 sandbox
# ---------------------------------------------------------------------------


def build_code_tools(sandbox: Sandbox) -> list[BaseTool]:
    """构造全部代码工具。"""

    def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """读取工作区内文件的文本内容，可选按行范围截取（行号从 1 开始）。"""

        target = sandbox.resolve(path)
        if not target.exists():
            return f"文件不存在: {sandbox.relative(target)}"
        if target.is_dir():
            return f"{sandbox.relative(target)} 是目录，请用 list_files 浏览。"
        if _is_probably_binary(target):
            return f"{sandbox.relative(target)} 疑似二进制文件，跳过。"

        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        first = max(1, int(start_line or 1))
        last = int(end_line or total)
        if last < first:
            first, last = last, first
        last = min(last, total)
        selected = lines[first - 1 : last]
        body = "\n".join(f"{first + i:>5}\t{line}" for i, line in enumerate(selected))
        header = f"# {sandbox.relative(target)} (共 {total} 行，显示 {first}-{last})"
        return f"{header}\n{body}"

    def write_file(path: str, content: str) -> str:
        """写入（覆盖）工作区内的文件。属于高危操作，需要人工审批。"""

        target = sandbox.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        action = "覆盖" if existed else "创建"
        return f"已{action} {sandbox.relative(target)}（{len(content)} 字符）"

    def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        """在文件中做精确字符串替换。属于高危操作，需要人工审批。"""

        target = sandbox.resolve(path)
        if not target.exists():
            return f"文件不存在: {sandbox.relative(target)}"
        text = target.read_text(encoding="utf-8")
        if old_string not in text:
            return (
                f"未在 {sandbox.relative(target)} 中找到待替换内容。"
                "请先用 read_file 确认原文（含缩进与空白）。"
            )
        count = text.count(old_string)
        if not replace_all and count > 1:
            return (
                f"待替换内容在 {sandbox.relative(target)} 中出现 {count} 次，"
                "请补充上下文或设置 replace_all=true。"
            )
        target.write_text(text.replace(old_string, new_string), encoding="utf-8")
        return f"已替换 {sandbox.relative(target)} 中 {count} 处内容"

    def delete_file(path: str) -> str:
        """删除工作区内的文件。属于高危操作，需要人工审批。"""

        target = sandbox.resolve(path)
        if not target.exists():
            return f"文件不存在: {sandbox.relative(target)}"
        if target.is_dir():
            return f"{sandbox.relative(target)} 是目录，本工具只支持删除文件。"
        target.unlink()
        return f"已删除 {sandbox.relative(target)}"

    def list_files(path: str = ".", pattern: str = "*", max_entries: int = 200) -> str:
        """列出工作区内目录下的文件与子目录。"""

        root = sandbox.resolve(path)
        if not root.exists():
            return f"目录不存在: {sandbox.relative(root)}"
        if not root.is_dir():
            return f"{sandbox.relative(root)} 不是目录。"
        entries: list[str] = []
        for item in sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if item.name in SKIP_DIRS or item.name.startswith("."):
                continue
            if pattern not in ("*", "**/*") and not item.match(pattern):
                continue
            entries.append(f"{'DIR ' if item.is_dir() else 'FILE'}  {sandbox.relative(item)}")
            if len(entries) >= max_entries:
                entries.append(f"... （已截断，上限 {max_entries} 条）")
                break
        if not entries:
            return f"{sandbox.relative(root)} 下没有匹配项。"
        return "\n".join(entries)

    def search_code(
        query: str,
        path: str = ".",
        max_results: int = 50,
        ignore_case: bool = True,
    ) -> str:
        """在工作区内按字面量搜索代码，返回 file:line 形式的结果。"""

        if not query:
            return "搜索词为空。"
        root = sandbox.resolve(path)
        if not root.exists():
            return f"目录不存在: {sandbox.relative(root)}"

        needle = query.lower() if ignore_case else query
        hits: list[str] = []
        for file_path in _iter_files(root):
            if _is_probably_binary(file_path):
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                haystack = line.lower() if ignore_case else line
                if needle in haystack:
                    hits.append(f"{sandbox.relative(file_path)}:{number}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        hits.append(f"... （已截断，上限 {max_results} 条）")
                        return "\n".join(hits)
        if not hits:
            return f"未找到包含 {query!r} 的内容。"
        return "\n".join(hits)

    def run_command(command: str, cwd: str = ".", timeout: int = 60) -> str:
        """在工作区内执行一条 shell 命令。属于高危操作，需要人工审批。

        输出包含退出码与 stdout/stderr 摘要；命令的工作目录被限制在工作区内，
        但命令本身仍可能访问系统其它位置，因此必须人工确认。
        """

        for pattern in FORBIDDEN_COMMAND_PATTERNS:
            if re.search(pattern, command):
                return f"命令被安全策略拒绝（命中禁止模式）: {command}"
        workdir = sandbox.resolve(cwd)
        if not workdir.is_dir():
            return f"工作目录不存在: {sandbox.relative(workdir)}"
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=max(1, int(timeout)),
                env=_sanitized_env(),
            )
        except subprocess.TimeoutExpired:
            return f"命令超时（>{timeout}s）: {command}"
        except OSError as exc:
            return f"命令执行失败: {exc}"

        parts = [f"$ {command}", f"exit={completed.returncode}"]
        if completed.stdout.strip():
            parts.append("--- stdout ---\n" + completed.stdout.strip()[:20000])
        if completed.stderr.strip():
            parts.append("--- stderr ---\n" + completed.stderr.strip()[:8000])
        return "\n".join(parts)

    def ast_inspect(path: str) -> str:
        """解析 Python 文件，列出模块级结构（类 / 函数 / 方法）及其行号。"""

        target = sandbox.resolve(path)
        if not target.exists():
            return f"文件不存在: {sandbox.relative(target)}"
        if target.suffix != ".py":
            return f"{sandbox.relative(target)} 不是 Python 文件，仅支持 .py。"
        source = target.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return f"语法错误 {sandbox.relative(target)}:{exc.lineno}: {exc.msg}"

        lines: list[str] = []

        def walk(node: ast.AST, prefix: str = "") -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in child.args.args]
                    lines.append(
                        f"{prefix}def {child.name}({', '.join(args)})  L{child.lineno}-{child.end_lineno}"
                    )
                elif isinstance(child, ast.ClassDef):
                    bases = ", ".join(ast.unparse(b) for b in child.bases)
                    lines.append(f"{prefix}class {child.name}({bases})  L{child.lineno}-{child.end_lineno}")
                    walk(child, prefix + "    ")

        walk(tree)
        if not lines:
            return f"{sandbox.relative(target)} 中没有顶层定义。"
        return "\n".join(lines)

    return [
        StructuredTool.from_function(
            func=read_file,
            name="read_file",
            description="读取工作区内的文本文件内容，返回带行号的文本。",
        ),
        StructuredTool.from_function(
            func=write_file,
            name="write_file",
            description="写入（覆盖）工作区内的文件。需要人工审批。",
        ),
        StructuredTool.from_function(
            func=edit_file,
            name="edit_file",
            description="对工作区内的文件做精确字符串替换。需要人工审批。",
        ),
        StructuredTool.from_function(
            func=delete_file,
            name="delete_file",
            description="删除工作区内的文件。需要人工审批。",
        ),
        StructuredTool.from_function(
            func=list_files,
            name="list_files",
            description="列出工作区内某个目录下的文件与子目录。",
        ),
        StructuredTool.from_function(
            func=search_code,
            name="search_code",
            description="在工作区内按字面量搜索代码，返回 file:line: 内容 的匹配列表。",
        ),
        StructuredTool.from_function(
            func=run_command,
            name="run_command",
            description="在工作区内执行一条 shell 命令。需要人工审批。",
        ),
        StructuredTool.from_function(
            func=ast_inspect,
            name="ast_inspect",
            description="解析 Python 文件，列出类/函数/方法及行号。",
        ),
    ]


def tool_result_from_call(name: str, args: dict[str, Any], output: str) -> ToolResult:
    """把工具调用结果包成统一结构。"""

    return ToolResult(ok=True, output=output, meta={"tool": name, "args": args})


__all__ = ["build_code_tools", "tool_result_from_call", "ToolError"]
