"""Git 只读工具。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

from .sandbox import Sandbox


def _run_git(args: list[str], cwd: Path, timeout: int = 30) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return "未找到 git 可执行文件。"
    except subprocess.TimeoutExpired:
        return f"git {' '.join(args)} 执行超时。"
    except OSError as exc:  # pragma: no cover - 防御性
        return f"git {' '.join(args)} 执行失败: {exc}"

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        return f"git {' '.join(args)} 退出码 {completed.returncode}: {stderr}"
    return (completed.stdout or "").strip()


def build_git_tools(sandbox: Sandbox) -> list[BaseTool]:
    """构造 Git 状态 / 差异查看工具（只读）。"""

    def git_status(path: str = ".") -> str:
        """查看工作区内 Git 仓库的当前状态（分支、暂存区与工作区改动）。"""

        repo = sandbox.resolve(path)
        if not repo.is_dir():
            return f"目录不存在: {sandbox.relative(repo)}"
        output = _run_git(["status", "--short", "--branch"], repo)
        return output or "工作区干净（无改动）。"

    def git_diff(path: str = ".", ref: str = "") -> str:
        """查看工作区内仓库的未提交差异；给出 ref 时查看该 ref 与工作区的差异。"""

        repo = sandbox.resolve(path)
        if not repo.is_dir():
            return f"目录不存在: {sandbox.relative(repo)}"
        args = ["diff", "--stat", "--patch"]
        if ref:
            args.append(ref)
        output = _run_git(args, repo)
        if not output:
            return "没有差异内容。"
        return output[:40000]

    return [
        StructuredTool.from_function(
            func=git_status,
            name="git_status",
            description="查看工作区内 Git 仓库的当前状态（只读）。",
        ),
        StructuredTool.from_function(
            func=git_diff,
            name="git_diff",
            description="查看工作区内 Git 仓库的代码差异（只读）。",
        ),
    ]
