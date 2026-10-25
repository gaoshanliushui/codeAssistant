"""技能工具。

技能工具是**按专家绑定**的：每个专家只能看到并载入自己绑定的技能，
避免模型在无关技能之间游走，也避免上下文被无关内容撑爆。

三个工具：

* ``list_skills`` —— 列出可用技能（名称 + 描述）
* ``load_skill`` —— 载入某个技能的完整说明
* ``run_skill_script`` —— 执行技能自带的脚本（沙箱内 + 人工审批）
"""

from __future__ import annotations

import os
import subprocess

from langchain_core.tools import BaseTool, StructuredTool

from ..skills import Skill
from .sandbox import Sandbox

#: 执行技能脚本的超时（秒）
SCRIPT_TIMEOUT = 60


def build_skill_tools(skills: list[Skill], sandbox: Sandbox) -> list[BaseTool]:
    """构造技能工具（绑定到指定技能集合）。"""

    allowed = {skill.name: skill for skill in skills}

    def list_skills() -> str:
        """列出当前可用的技能及其说明。需要某个技能的完整内容时调用 load_skill。"""

        if not allowed:
            return "当前没有可用的技能。"
        lines = [f"- {skill.name}：{skill.to_summary()}" for skill in allowed.values()]
        return "\n".join(lines)

    def load_skill(skill_name: str) -> str:
        """载入某个技能的完整说明文档，然后按说明完成任务。"""

        skill = allowed.get(skill_name)
        if skill is None:
            available = ", ".join(allowed) or "（无）"
            return f"技能 {skill_name} 不可用。可用技能：{available}"
        return skill.to_prompt()

    def run_skill_script(skill_name: str, script: str, arguments: str = "") -> str:
        """执行技能自带的脚本（scripts/ 目录下）。属于高危操作，需要人工审批。

        参数：skill_name 为技能名，script 为脚本文件名（如 report.py），
        arguments 为传给脚本的命令行参数。
        """

        skill = allowed.get(skill_name)
        if skill is None:
            available = ", ".join(allowed) or "（无）"
            return f"技能 {skill_name} 不可用。可用技能：{available}"
        if script not in skill.scripts:
            return (
                f"技能 {skill_name} 没有脚本 {script}。"
                f"可用脚本：{', '.join(skill.scripts) or '（无）'}"
            )
        if skill.path is None:
            return f"技能 {skill_name} 的说明文件不可用，无法定位脚本。"

        script_path = (skill.path.parent / "scripts" / script).resolve()
        scripts_root = (skill.path.parent / "scripts").resolve()
        # 二次校验：脚本必须真的位于技能自己的 scripts/ 目录内
        if not script_path.is_relative_to(scripts_root) or not script_path.is_file():
            return f"脚本 {script} 不存在或不在技能目录内，已拒绝执行。"

        command = f"python3 {script_path}"
        if arguments.strip():
            command += " " + arguments.strip()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(script_path.parent),
                capture_output=True,
                text=True,
                timeout=SCRIPT_TIMEOUT,
                env=_script_env(sandbox),
            )
        except subprocess.TimeoutExpired:
            return f"脚本 {script} 执行超时（>{SCRIPT_TIMEOUT}s）。"
        except OSError as exc:  # pragma: no cover - 防御性
            return f"脚本 {script} 执行失败：{exc}"

        parts = [f"$ {command}", f"exit={completed.returncode}"]
        if completed.stdout.strip():
            parts.append("--- stdout ---\n" + completed.stdout.strip()[:20000])
        if completed.stderr.strip():
            parts.append("--- stderr ---\n" + completed.stderr.strip()[:8000])
        return "\n".join(parts)

    return [
        StructuredTool.from_function(
            func=list_skills,
            name="list_skills",
            description="列出当前可用的技能（名称与一句话说明）。",
        ),
        StructuredTool.from_function(
            func=load_skill,
            name="load_skill",
            description="载入指定技能的完整说明文档，然后按说明执行。",
        ),
        StructuredTool.from_function(
            func=run_skill_script,
            name="run_skill_script",
            description="执行技能自带的脚本（scripts/ 目录下）。需要人工审批。",
        ),
    ]


def _script_env(sandbox: Sandbox) -> dict[str, str]:
    """脚本执行环境：剔除敏感变量，并限定工作目录。"""

    env: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")):
            continue
        env[name] = value
    env["PYTHONIOENCODING"] = "utf-8"
    env["CODEASSISTANT_WORKSPACE"] = str(sandbox.root)
    return env
