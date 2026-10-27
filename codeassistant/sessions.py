"""会话记录。

把「一次对话」记成可回看的历史：网页侧栏的「最近」列表、点开某个任务后拉回来的
对话记录，都来自这里。

设计要点：

* **与检查点分离** —— LangGraph 的 checkpoint 存的是图的内部状态（消息、迭代计数），
  而这里存的是**给人看的对话流水**（谁说了什么、哪个专家产出了什么结论），
  两者生命周期不同，混在一起会让「拉回历史」这件事变得不可控。
* **单个 JSON 文件** —— 单用户本地工具，几十上百个会话的体量用不着数据库；
  文件放在 ``.codeassistant/sessions.json``，人可以直接打开看 / 改 / 删。
* **写入是原子的** —— 先写临时文件再 ``replace()``，避免进程被杀时留下半个文件。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 会话标题的最大长度
TITLE_LIMIT = 40
#: 保留的会话数量上限（超出后丢弃最旧的）
SESSION_LIMIT = 100


@dataclass
class Turn:
    """对话中的一轮。

    ``role`` 取值：``user``（用户）、``expert``（某个专家的结论）、``team``（专家团结论）。
    """

    role: str
    text: str
    label: str = ""
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "label": self.label, "ts": self.ts}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Turn:
        return cls(
            role=str(data.get("role") or "user"),
            text=str(data.get("text") or ""),
            label=str(data.get("label") or ""),
            ts=float(data.get("ts") or 0.0),
        )


@dataclass
class Session:
    """一次会话（可多轮）。"""

    id: str
    title: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    #: 本次会话锁定的目标（专家 / 专家团），空串表示自动路由
    expert: str = ""
    team: str = ""
    turns: list[Turn] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expert": self.expert,
            "team": self.team,
            "turns": [t.to_dict() for t in self.turns],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        turns = [Turn.from_dict(t) for t in data.get("turns") or [] if isinstance(t, dict)]
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            expert=str(data.get("expert") or ""),
            team=str(data.get("team") or ""),
            turns=turns,
        )

    def summary(self) -> dict[str, Any]:
        """列表用的摘要（不含逐轮内容）。"""

        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expert": self.expert,
            "team": self.team,
            "turns": len(self.turns),
        }


class SessionStore:
    """会话记录仓库（内存 + JSON 文件持久化）。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self._sessions: dict[str, Session] = {}
        self._loaded = False

    # -- 读写 -------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        items = data.get("sessions") if isinstance(data, dict) else data
        for item in items or []:
            if not isinstance(item, dict):
                continue
            session = Session.from_dict(item)
            if session.id:
                self._sessions[session.id] = session

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"sessions": [s.to_dict() for s in self._sessions.values()]}
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:  # pragma: no cover - 磁盘异常时不影响主流程
            pass

    # -- 查询 -------------------------------------------------------------

    def list(self) -> list[Session]:
        """全部会话，最近更新的排在前面。"""

        self._load()
        return sorted(self._sessions.values(), key=lambda s: s.updated_at, reverse=True)

    def get(self, session_id: str) -> Session | None:
        self._load()
        return self._sessions.get(session_id)

    def summaries(self) -> list[dict[str, Any]]:
        return [s.summary() for s in self.list()]

    # -- 写入 -------------------------------------------------------------

    def begin(
        self,
        session_id: str,
        task: str,
        *,
        expert: str = "",
        team: str = "",
    ) -> Session:
        """开始一轮：新建会话或往已有会话追加一条用户消息。"""

        self._load()
        now = time.time()
        session = self._sessions.get(session_id)
        if session is None:
            session = Session(
                id=session_id,
                title=task.strip()[:TITLE_LIMIT],
                created_at=now,
                updated_at=now,
            )
            self._sessions[session_id] = session
        if expert:
            session.expert = expert
        if team:
            session.team = team
        session.updated_at = now
        session.turns.append(Turn(role="user", text=task, ts=now))
        self._trim()
        self._save()
        return session

    def append(self, session_id: str, role: str, text: str, label: str = "") -> None:
        """追加一条非用户消息（专家 / 专家团的产出）。"""

        self._load()
        session = self._sessions.get(session_id)
        if session is None or not text:
            return
        now = time.time()
        session.turns.append(Turn(role=role, text=text, label=label, ts=now))
        session.updated_at = now
        self._save()

    def _trim(self) -> None:
        """超出上限时丢弃最旧的会话。"""

        if len(self._sessions) <= SESSION_LIMIT:
            return
        ordered = sorted(self._sessions.values(), key=lambda s: s.updated_at)
        for session in ordered[: len(self._sessions) - SESSION_LIMIT]:
            self._sessions.pop(session.id, None)

    def clear(self) -> None:
        self._load()
        self._sessions.clear()
        self._save()
