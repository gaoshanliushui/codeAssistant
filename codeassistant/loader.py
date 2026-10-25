"""专家包加载器。

直接复用仓库中已有的 codebuddy 专家包格式（``.codeassistant/my-experts/plugins/*``）：

* ``<plugin>/.codebuddy-plugin/plugin.json`` —— 包元数据
* ``<plugin>/agents/<agent>.md`` —— agent 定义（YAML frontmatter + 正文提示词）

包内 ``expertType`` 为 ``"agent"`` 表示单体专家，为 ``"team"`` 表示专家团
（主理人 ``teamInfo.leadAgent`` + 成员 ``teamInfo.memberAgents``）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLUGIN_MANIFEST = Path(".codebuddy-plugin") / "plugin.json"
FRONTMATTER_DELIMITER = "---"


@dataclass
class AgentDoc:
    """一个 agent 的原始定义。"""

    name: str
    description: str = ""
    display_name: dict[str, str] = field(default_factory=dict)
    profession: dict[str, str] = field(default_factory=dict)
    max_turns: int = 20
    system_prompt: str = ""
    path: Path | None = None

    def label(self, locale: str = "zh") -> str:
        """取展示名，缺失时回退到 agent id。"""

        return self.display_name.get(locale) or self.display_name.get("en") or self.name

    def title(self, locale: str = "zh") -> str:
        return self.profession.get(locale) or self.profession.get("en") or ""


@dataclass
class LoadedPack:
    """一个已加载的专家包。"""

    name: str
    root: Path
    plugin: dict[str, Any]
    agents: dict[str, AgentDoc] = field(default_factory=dict)

    @property
    def expert_type(self) -> str:
        return str(self.plugin.get("expertType") or "agent")

    @property
    def is_team(self) -> bool:
        return self.expert_type == "team"

    @property
    def lead_name(self) -> str:
        team_info = self.plugin.get("teamInfo") or {}
        return str(team_info.get("leadAgent") or self.plugin.get("agentName") or "")

    @property
    def member_names(self) -> list[str]:
        team_info = self.plugin.get("teamInfo") or {}
        members = list(team_info.get("memberAgents") or [])
        if not members:
            # 没有 teamInfo 时，把除主理人外的 agent 都当作成员
            members = [n for n in self.agents if n != self.lead_name]
        return members

    def display_name(self, locale: str = "zh") -> str:
        value = self.plugin.get("displayName") or {}
        if isinstance(value, dict):
            return value.get(locale) or value.get("en") or self.name
        return str(value) or self.name

    def description(self, locale: str = "zh") -> str:
        value = self.plugin.get("displayDescription") or self.plugin.get("description") or ""
        if isinstance(value, dict):
            return value.get(locale) or value.get("en") or ""
        return str(value)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆分 YAML frontmatter 与正文提示词。"""

    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        return {}, text

    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_DELIMITER:
            raw = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :]).strip()
            try:
                meta = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), body

    return {}, text


def _safe_int(value: Any, default: int) -> int:
    """尽最大努力转成 int，失败时回退默认值（不因为一个字段丢掉整个专家包）。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_agent_doc(path: Path) -> AgentDoc:
    """读取单个 agent markdown。"""

    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    display_name = meta.get("displayName") or {}
    profession = meta.get("profession") or {}
    if isinstance(display_name, str):
        display_name = {"zh": display_name}
    if isinstance(profession, str):
        profession = {"zh": profession}
    return AgentDoc(
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or ""),
        display_name={str(k): str(v) for k, v in (display_name or {}).items()},
        profession={str(k): str(v) for k, v in (profession or {}).items()},
        max_turns=_safe_int(meta.get("maxTurns"), 20),
        system_prompt=body,
        path=path,
    )


def iter_pack_roots(experts_dir: Path) -> Iterator[Path]:
    """找出所有包含 plugin.json 的包目录。

    同时支持 ``<experts_dir>/plugins/*`` 与 ``<experts_dir>/*`` 两种布局。
    """

    seen: set[Path] = set()
    candidates: list[Path] = []
    plugins_dir = experts_dir / "plugins"
    if plugins_dir.is_dir():
        candidates.extend(sorted(p for p in plugins_dir.iterdir() if p.is_dir()))
    if experts_dir.is_dir():
        candidates.extend(sorted(p for p in experts_dir.iterdir() if p.is_dir()))

    for candidate in candidates:
        if (candidate / PLUGIN_MANIFEST).is_file():
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield candidate


def load_pack(root: Path) -> LoadedPack:
    """加载单个专家包。"""

    manifest = json.loads((root / PLUGIN_MANIFEST).read_text(encoding="utf-8"))
    pack = LoadedPack(
        name=str(manifest.get("name") or root.name),
        root=root,
        plugin=manifest,
    )

    agents_dir = root / "agents"
    if agents_dir.is_dir():
        for agent_file in sorted(agents_dir.glob("*.md")):
            doc = load_agent_doc(agent_file)
            pack.agents[doc.name] = doc

    # manifest 的 agents 列表可能引用了 agents/ 之外的路径
    for relative in manifest.get("agents", []) or []:
        agent_path = (root / str(relative)).resolve()
        # 只接受包目录内的文件，防止 manifest 指向包外
        if not agent_path.is_relative_to(root.resolve()):
            continue
        if agent_path.is_file() and agent_path.suffix == ".md":
            doc = load_agent_doc(agent_path)
            pack.agents.setdefault(doc.name, doc)

    return pack


def load_packs(experts_dir: Path | str) -> list[LoadedPack]:
    """加载 ``experts_dir`` 下的全部专家包。"""

    root = Path(experts_dir).expanduser().resolve()
    packs: list[LoadedPack] = []
    for pack_root in iter_pack_roots(root):
        try:
            packs.append(load_pack(pack_root))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, yaml.YAMLError, ValueError):
            continue
    return packs
