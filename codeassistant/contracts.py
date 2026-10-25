"""统一契约层。

三层架构（专家 → 专家团 → 主编排器）之间**只**通过本模块定义的
状态协议通信，专家子图因此可以脱离主图单独编译、单独测试、整体替换。

协议划分：

* :class:`ExpertInput` / :class:`ExpertOutput` —— 专家子图的输入输出契约
* :class:`TeamInput` / :class:`TeamOutput` —— 专家团子图的输入输出契约
* :data:`ADAPTERS` 中列出的显式映射函数 —— 契约之间的转换
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

ExpertStatus = Literal["ok", "partial", "need_human", "failed", "skipped"]
TeamStatus = Literal["ok", "partial", "need_human", "failed", "skipped"]


@dataclass
class Artifact:
    """专家产出物：代码片段、文件、文档、diff、命令执行记录等。"""

    kind: Literal["code", "file", "doc", "diff", "command", "log"] = "doc"
    name: str = ""
    content: str = ""
    language: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "content": self.content,
            "language": self.language,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            kind=data.get("kind", "doc"),
            name=data.get("name", ""),
            content=data.get("content", ""),
            language=data.get("language"),
            path=data.get("path"),
        )

    def preview(self, limit: int = 200) -> str:
        body = self.content.strip().replace("\n", " ⏎ ")
        if len(body) > limit:
            body = body[:limit] + "…"
        return f"[{self.kind}] {self.name or self.path or '-'} :: {body}"


class ExpertInput(TypedDict, total=False):
    """喂给专家子图的输入契约。"""

    task: str
    context: list[str]
    upstream: list[Artifact]
    session_id: str
    extra: dict[str, Any]


class ExpertOutput(TypedDict, total=False):
    """专家子图的输出契约。"""

    expert: str
    status: ExpertStatus
    summary: str
    artifacts: list[Artifact]
    findings: list[str]
    next_action: str
    turns_used: int
    messages: list[Any]


class TeamInput(TypedDict, total=False):
    """喂给专家团子图的输入契约。"""

    task: str
    context: list[str]
    members: list[str]
    mode: Literal["supervisor", "parallel"]
    session_id: str
    extra: dict[str, Any]


class TeamOutput(TypedDict, total=False):
    """专家团子图的输出契约。"""

    team: str
    status: TeamStatus
    summary: str
    deliverables: dict[str, ExpertOutput]
    transcript: list[dict[str, Any]]
    rounds: int


# --------------------------------------------------------------------------
# 显式映射适配层
# --------------------------------------------------------------------------


def team_input_to_expert_input(team_input: TeamInput, member: str) -> ExpertInput:
    """专家团 → 成员专家：把团级任务拆成该成员的专家输入。"""

    return ExpertInput(
        task=team_input.get("task", ""),
        context=list(team_input.get("context", [])),
        upstream=[],
        session_id=team_input.get("session_id", ""),
        extra={"member": member, "team": team_input.get("team", "")},
    )


def expert_output_to_deliverable(expert_output: ExpertOutput) -> ExpertOutput:
    """成员专家输出 → 团队交付物（裁掉中间态，只保留契约字段）。"""

    return ExpertOutput(
        expert=expert_output.get("expert", ""),
        status=expert_output.get("status", "ok"),
        summary=expert_output.get("summary", ""),
        artifacts=list(expert_output.get("artifacts", [])),
        findings=list(expert_output.get("findings", [])),
        next_action=expert_output.get("next_action", ""),
        turns_used=int(expert_output.get("turns_used", 0)),
    )


def merge_team_outputs(
    team: str,
    deliverables: dict[str, ExpertOutput],
    transcript: list[dict[str, Any]] | None = None,
) -> TeamOutput:
    """汇总所有成员产出 → 团队输出契约。

    汇总规则：任一成员失败则团队 ``failed``；任一成员需要人工介入则
    ``need_human``；有部分成员未产出则 ``partial``；否则 ``ok``。
    """

    statuses = [d.get("status", "ok") for d in deliverables.values()]
    if not statuses:
        status: TeamStatus = "skipped"  # type: ignore[assignment]
    elif any(s == "failed" for s in statuses):
        status = "failed"
    elif any(s == "need_human" for s in statuses):
        status = "need_human"
    elif any(s == "partial" for s in statuses):
        status = "partial"
    else:
        status = "ok"

    sections: list[str] = []
    for name, output in deliverables.items():
        summary = (output.get("summary") or "").strip()
        sections.append(f"### {name}\n{summary or '（无产出）'}")
    summary = "\n\n".join(sections)

    return TeamOutput(
        team=team,
        status=status,
        summary=summary,
        deliverables=dict(deliverables),
        transcript=list(transcript or []),
        rounds=len(transcript or []),
    )


@dataclass
class TeamMember:
    """专家团成员描述（由 TeamConfig 转换而来）。"""

    name: str
    display_name: str = ""
    profession: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "profession": self.profession,
        }


@dataclass
class TranscriptEntry:
    """专家团协作过程记录（主理人派活 / 成员回传）。"""

    round: int
    actor: str
    action: str
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "actor": self.actor,
            "action": self.action,
            "detail": self.detail,
            "payload": self.payload,
        }
