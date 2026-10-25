#!/usr/bin/env python3
"""统计仓库里各语言的文件数与代码行数。

只读脚本：不修改任何文件。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    ".idea", ".vscode",
}

LANG_BY_SUFFIX = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".jsx": "JavaScript", ".java": "Java", ".kt": "Kotlin", ".go": "Go",
    ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cs": "C#", ".swift": "Swift", ".m": "Objective-C",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".sql": "SQL",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".vue": "Vue",
    ".md": "Markdown", ".yml": "YAML", ".yaml": "YAML", ".json": "JSON",
    ".toml": "TOML", ".xml": "XML", ".gradle": "Gradle",
}


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            yield Path(dirpath) / filename


def main() -> int:
    parser = argparse.ArgumentParser(description="统计各语言的文件数与代码行数")
    parser.add_argument("--path", default=".", help="统计根目录")
    parser.add_argument("--top", type=int, default=10, help="输出最大的 N 个文件")
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"目录不存在: {root}")
        return 1

    stats: dict[str, dict[str, int]] = {}
    file_lines: list[tuple[int, Path]] = []

    for path in iter_files(root):
        lang = LANG_BY_SUFFIX.get(path.suffix.lower())
        if lang is None:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = sum(1 for line in content.splitlines() if line.strip())
        entry = stats.setdefault(lang, {"files": 0, "lines": 0})
        entry["files"] += 1
        entry["lines"] += lines
        file_lines.append((lines, path))

    if not stats:
        print("没有找到可统计的源文件。")
        return 0

    total_lines = sum(entry["lines"] for entry in stats.values())
    total_files = sum(entry["files"] for entry in stats.values())

    print(f"统计目录: {root}")
    print(f"文件总数: {total_files}   代码行数: {total_lines}")
    print()
    print(f"{'语言':<14}{'文件数':>8}{'行数':>10}{'占比':>9}")
    print("-" * 41)
    for lang, entry in sorted(stats.items(), key=lambda item: -item[1]["lines"]):
        ratio = entry["lines"] / total_lines * 100 if total_lines else 0
        print(f"{lang:<14}{entry['files']:>8}{entry['lines']:>10}{ratio:>8.1f}%")

    if args.top > 0:
        print()
        print(f"最大的 {min(args.top, len(file_lines))} 个文件：")
        for lines, path in sorted(file_lines, reverse=True)[: args.top]:
            print(f"  {lines:>7} 行  {path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
