"""技能（Skill）系统。

技能是「可按需载入的能力单元」，采用**渐进式披露**（progressive disclosure）：

1. 专家的系统提示里只放技能清单（名称 + 一句话描述），上下文开销极小；
2. 模型判断需要时调用 ``load_skill`` 载入完整说明；
3. 技能可以携带脚本，通过 ``run_skill_script`` 执行（沙箱内 + 人工审批）。

目录格式::

    <skills-dir>/<skill-name>/SKILL.md      # 也支持 <skill-name>.md
    <skills-dir>/<skill-name>/scripts/*.py  # 可选：随技能附带的脚本
    <skills-dir>/<skill-name>/references/*  # 可选：补充资料

技能文件带 YAML frontmatter::

    ---
    name: pdf-extract
    description: 从 PDF 中提取正文与表格
    agents: [sdt-requirement]   # 可选：绑定到哪些专家
    ---

    # 正文
    …给模型看的使用说明…

技能分两处存放：

* **全局技能** —— ``.codeassistant/skills/``，对所有专家可见；
* **包内技能** —— ``<专家包>/skills/``，默认只对该包内的专家可见，
  通过 frontmatter 的 ``agents`` 字段或包内 ``agent-tools.json`` 的 ``skills`` 字段绑定。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .loader import LoadedPack, parse_frontmatter

#: 技能文件的候选名（按优先级）
SKILL_FILE_NAMES = ("SKILL.md", "skill.md", "SKILL.markdown")

#: 技能附带的脚本目录名
SCRIPTS_DIR = "scripts"

#: 技能附带的参考资料目录名
REFERENCES_DIR = "references"

#: 通用型专家：没有显式绑定规则的技能默认归它
GENERAL_EXPERT = "code-assistant"


@dataclass(frozen=True)
class Skill:
    """一个技能。"""

    name: str
    description: str = ""
    instructions: str = ""
    path: Path | None = None
    scripts: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    #: frontmatter 里声明的绑定专家
    agents: tuple[str, ...] = ()
    #: ``default: true`` —— 绑定给所有专家
    default: bool = False
    #: 来源：``global`` 或专家包名
    source: str = "global"

    @property
    def is_global(self) -> bool:
        return self.source == "global"

    def to_prompt(self) -> str:
        """技能正文的完整形态（``load_skill`` 返回的内容）。"""

        header = f"# 技能：{self.name}"
        if self.description:
            header += f"\n{self.description}"
        body = self.instructions.strip()
        parts = [header]
        if body:
            parts.append(body)
        if self.scripts:
            parts.append(
                "可用脚本（用 run_skill_script 执行）：\n"
                + "\n".join(f"- scripts/{item}" for item in self.scripts)
            )
        if self.references:
            parts.append(
                "参考资料（用 read_skill_reference 读取）：\n"
                + "\n".join(f"- references/{item}" for item in self.references)
            )
        return "\n\n".join(parts)

    def to_summary(self) -> str:
        """清单里的一行描述。"""

        return self.description or self.instructions.strip().splitlines()[0][:80]

    def config_hash(self) -> str:
        payload = json.dumps(
            {
                "name": self.name,
                "description": self.description,
                "instructions": self.instructions,
                "scripts": list(self.scripts),
                "references": list(self.references),
                "agents": list(self.agents),
                "default": self.default,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_skill_file(path: Path, source: str) -> Skill | None:
    """从 SKILL.md 读出一个技能。"""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}
    # 布局是 <dir>/<name>/SKILL.md 时默认名取目录名，是 <dir>/<name>.md 时取文件名
    if path.stem.lower() in ("skill", "skills"):
        default_name = path.parent.name
    else:
        default_name = path.stem
    name = str(meta.get("name") or default_name or "skill")

    agents = meta.get("agents") or []
    if isinstance(agents, str):
        agents = [agents]
    return Skill(
        name=name,
        description=str(meta.get("description") or ""),
        instructions=body.strip(),
        path=path,
        agents=tuple(str(item) for item in agents),
        default=bool(meta.get("default") or False),
        source=source,
    )


def _scan_skill_dir(directory: Path, source: str) -> list[Skill]:
    """扫描一个目录下的所有技能。

    支持两种布局：``<dir>/<name>/SKILL.md`` 与 ``<dir>/<name>.md``。
    """

    skills: list[Skill] = []
    if not directory.is_dir():
        return skills

    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            for filename in SKILL_FILE_NAMES:
                candidate = entry / filename
                if candidate.is_file():
                    skill = _load_skill_file(candidate, source)
                    if skill is not None:
                        skill = _with_assets(skill, entry)
                        skills.append(skill)
                    break
        elif entry.suffix.lower() == ".md" and entry.stem.lower() != "readme":
            skill = _load_skill_file(entry, source)
            if skill is not None:
                skills.append(skill)
    return skills


def _with_assets(skill: Skill, root: Path) -> Skill:
    """给技能补上 scripts/ 与 references/ 清单。"""

    scripts_dir = root / SCRIPTS_DIR
    references_dir = root / REFERENCES_DIR
    scripts = (
        tuple(sorted(p.name for p in scripts_dir.iterdir() if p.is_file()))
        if scripts_dir.is_dir()
        else ()
    )
    references = (
        tuple(sorted(p.name for p in references_dir.iterdir() if p.is_file()))
        if references_dir.is_dir()
        else ()
    )
    return Skill(
        name=skill.name,
        description=skill.description,
        instructions=skill.instructions,
        path=skill.path,
        scripts=scripts,
        references=references,
        agents=skill.agents,
        default=skill.default,
        source=skill.source,
    )


@dataclass
class SkillRegistry:
    """技能索引。"""

    skills: list[Skill] = field(default_factory=list)

    # -- 加载 -------------------------------------------------------------

    @classmethod
    def load(
        cls,
        *,
        global_dir: Path | None = None,
        packs: Iterable[LoadedPack] = (),
    ) -> SkillRegistry:
        """加载全局技能 + 各专家包内的技能。"""

        skills: list[Skill] = []
        if global_dir is not None:
            skills.extend(_scan_skill_dir(Path(global_dir), "global"))
        for pack in packs:
            skills.extend(_scan_skill_dir(pack.root / "skills", pack.name))
        # 同名技能：包内覆盖全局（更具体的优先）
        deduped: dict[str, Skill] = {}
        for skill in skills:
            deduped[skill.name] = skill
        return cls(skills=sorted(deduped.values(), key=lambda item: item.name))

    # -- 查询 -------------------------------------------------------------

    def get(self, name: str) -> Skill | None:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def list(self) -> list[Skill]:
        return list(self.skills)

    def names(self) -> list[str]:
        return [skill.name for skill in self.skills]

    def global_skills(self) -> list[Skill]:
        return [skill for skill in self.skills if skill.is_global]

    # -- 绑定 -------------------------------------------------------------

    def bind(
        self,
        expert_name: str,
        explicit: Iterable[str] = (),
    ) -> list[Skill]:
        """解析某个专家绑定的技能。

        规则（按优先级）：

        1. 专家包 ``agent-tools.json`` 里显式列出的技能 —— 一定绑定；
        2. frontmatter 声明了 ``agents`` 的技能 —— 只绑给列出的专家；
        3. frontmatter 声明了 ``default: true`` 的技能 —— 绑给所有专家；
        4. 都没声明的技能 —— 绑给通用型专家（内置代码助手）。
        """

        wanted = {str(name) for name in explicit}
        bound: list[Skill] = []
        for skill in self.skills:
            if skill.name in wanted:
                bound.append(skill)
                continue
            if skill.agents:
                if expert_name in skill.agents:
                    bound.append(skill)
                continue
            if skill.default or expert_name == GENERAL_EXPERT:
                bound.append(skill)
        return bound

    # -- 渲染 -------------------------------------------------------------

    @staticmethod
    def render_index(skills: Iterable[Skill]) -> str:
        """渲染技能清单（放进系统提示）。"""

        items = list(skills)
        if not items:
            return ""
        lines = [
            "## 可用技能",
            "以下技能可按需载入。需要某个技能的完整说明时，"
            "调用 load_skill(skill_name) 获取；技能自带的脚本用 run_skill_script 执行。",
        ]
        for skill in items:
            lines.append(f"- {skill.name}：{skill.to_summary()}")
        return "\n".join(lines)


def load_skill_meta(path: Path) -> dict[str, Any]:
    """只读技能文件的 frontmatter（给外部工具用）。"""

    try:
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return meta if isinstance(meta, dict) else {}
