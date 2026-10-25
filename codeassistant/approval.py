"""人审（Human-in-the-Loop）中心。

高危工具执行前会向本中心登记一条 :class:`ApprovalRequest`，
随后专家子图在 ``interrupt()`` 处暂停，等待人工给出「通过 / 拒绝」的决定。
决定通过 ``Command(resume=...)`` 回传，支持**从子图内部断点恢复**。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

RequestState = Literal["pending", "approved", "rejected"]


@dataclass
class ApprovalRequest:
    """一条待审批的高危工具调用。"""

    id: str
    expert: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    risk: str = "high"
    team: str | None = None
    thread_id: str = ""
    created_at: float = field(default_factory=time.time)
    state: RequestState = "pending"
    decision_note: str = ""

    def to_payload(self) -> dict[str, Any]:
        """送进 ``interrupt()`` 的载荷。"""

        return {
            "kind": "tool_approval",
            "request_id": self.id,
            "expert": self.expert,
            "team": self.team,
            "tool": self.tool,
            "args": self.args,
            "reason": self.reason,
            "risk": self.risk,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "expert": self.expert,
            "team": self.team,
            "tool": self.tool,
            "args": self.args,
            "reason": self.reason,
            "risk": self.risk,
            "thread_id": self.thread_id,
            "created_at": self.created_at,
            "state": self.state,
            "decision_note": self.decision_note,
        }


class ApprovalCenter:
    """进程内的审批登记簿（CLI 与 Web 共用）。"""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}

    def create(
        self,
        *,
        expert: str,
        tool: str,
        args: dict[str, Any] | None = None,
        reason: str = "",
        risk: str = "high",
        team: str | None = None,
        thread_id: str = "",
        request_id: str | None = None,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id=request_id or uuid.uuid4().hex[:12],
            expert=expert,
            tool=tool,
            args=dict(args or {}),
            reason=reason,
            risk=risk,
            team=team,
            thread_id=thread_id,
        )
        self._requests[request.id] = request
        return request

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)

    def resolve(self, request_id: str, approved: bool, note: str = "") -> ApprovalRequest:
        request = self._requests.get(request_id)
        if request is None:
            raise KeyError(f"未知的审批请求: {request_id}")
        request.state = "approved" if approved else "rejected"
        request.decision_note = note
        return request

    def pending(self, thread_id: str | None = None) -> list[ApprovalRequest]:
        items = [
            request
            for request in self._requests.values()
            if request.state == "pending"
            and (thread_id is None or request.thread_id == thread_id)
        ]
        return sorted(items, key=lambda item: item.created_at)

    def all(self) -> list[ApprovalRequest]:
        return sorted(self._requests.values(), key=lambda item: item.created_at)

    def clear(self) -> None:
        self._requests.clear()


def parse_resume_value(value: Any) -> tuple[bool, str]:
    """解析回传给 ``interrupt()`` 的审批结果。

    支持三种形态：

    * ``True`` / ``False``
    * ``{"approved": bool, "reason": str}``
    * ``{"decisions": [{"request_id": ..., "approved": bool}, ...]}``
    """

    if value is None:
        return False, "未提供审批结果，默认拒绝。"
    if isinstance(value, bool):
        return value, "通过" if value else "拒绝"
    if isinstance(value, dict):
        if "decisions" in value:
            decisions = value.get("decisions") or []
            if decisions:
                first = decisions[0]
                return bool(first.get("approved")), str(first.get("reason", ""))
            return False, "审批结果为空，默认拒绝。"
        approved = bool(value.get("approved", value.get("accept", False)))
        return approved, str(value.get("reason", ""))
    return bool(value), ""
