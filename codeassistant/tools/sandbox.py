"""工作区沙箱。

代码助手的全部文件/命令操作都被限制在 ``workspace`` 根目录之下：
路径先 ``resolve()``（顺带解析符号链接），再校验是否仍在根目录内，
越界一律拒绝——即使调用方拿到了审批也不会放行。
"""

from __future__ import annotations

from pathlib import Path

from .base import SandboxViolation


class Sandbox:
    """以某个目录为根的文件系统边界。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Sandbox(root={self.root})"

    def is_inside(self, candidate: Path) -> bool:
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return False
        return True

    def resolve(self, path: str | Path) -> Path:
        """把（可能相对的）路径解析成工作区内的绝对路径。

        越界时抛出 :class:`SandboxViolation`。
        """

        raw = Path(str(path)).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        candidate = candidate.resolve()
        if not self.is_inside(candidate):
            raise SandboxViolation(
                f"路径 {path!r} 超出工作区边界 {self.root}，已拒绝。"
            )
        return candidate

    def relative(self, path: str | Path) -> str:
        """转成相对工作区根的展示路径。"""

        candidate = Path(path)
        if not candidate.is_absolute():
            return str(candidate)
        try:
            return str(candidate.relative_to(self.root))
        except ValueError:
            return str(candidate)

    def contains(self, path: str | Path) -> bool:
        try:
            self.resolve(path)
        except SandboxViolation:
            return False
        return True
