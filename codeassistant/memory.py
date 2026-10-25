"""记忆与状态持久化。

三层命名空间的 ``thread_id``，保证多用户 / 多专家并行时的记忆隔离：

===============  ===========================  ============================
层级              thread_id 形态              用途
===============  ===========================  ============================
会话级            ``s:<session_id>``          主编排器的路由与总结记忆
专家级            ``e:<expert>:<session_id>``  单个专家跨轮次的记忆
团队级            ``t:<team>:<session_id>``    专家团协作过程的记忆
===============  ===========================  ============================

在此之上再叠加 ``checkpoint_ns``，用于区分同一张图内部的节点。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from .settings import Settings


@dataclass(frozen=True)
class ThreadScope:
    """一个会话内的三层 thread_id 命名空间。"""

    session_id: str

    def session_thread(self) -> str:
        return f"s:{self.session_id}"

    def expert_thread(self, expert: str) -> str:
        return f"e:{expert}:{self.session_id}"

    def team_thread(self, team: str) -> str:
        return f"t:{team}:{self.session_id}"

    @staticmethod
    def namespace(*parts: str) -> tuple[str, ...]:
        """生成 checkpoint_ns（区分同图内节点）。"""

        return tuple(p for p in parts if p)

    def config(self, thread_id: str, **extra: Any) -> dict[str, Any]:
        """构造带 thread_id 的 RunnableConfig。"""

        configurable: dict[str, Any] = {"thread_id": thread_id}
        configurable.update(extra)
        return {"configurable": configurable}


class HybridSqliteSaver(BaseCheckpointSaver):
    """同时支持同步与异步调用的 SQLite 检查点。

    同步调用走 ``sqlite3`` 连接，异步调用走 ``aiosqlite`` 连接，
    两者指向同一个数据库文件。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sync_conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._sync_conn.execute("PRAGMA journal_mode=WAL")
        self._sync_conn.execute("PRAGMA busy_timeout=30000")
        from langgraph.checkpoint.sqlite import SqliteSaver

        self._sync_saver = SqliteSaver(self._sync_conn)
        self._async_saver: Any = None

    async def _async(self) -> Any:
        if self._async_saver is None:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            conn = await aiosqlite.connect(str(self.path))
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=30000")
            self._async_saver = AsyncSqliteSaver(conn)
        return self._async_saver

    # -- 同步接口 ----------------------------------------------------------

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return self._sync_saver.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        self._sync_saver.put_writes(config, writes, task_id, task_path)

    def get_tuple(self, config: Any) -> Any:
        return self._sync_saver.get_tuple(config)

    def list(self, config: Any, *, filter: Any = None, before: Any = None, limit: Any = None) -> Iterator[Any]:
        return self._sync_saver.list(config, filter=filter, before=before, limit=limit)

    def delete_thread(self, thread_id: str) -> None:
        self._sync_saver.delete_thread(thread_id)

    # -- 异步接口 ----------------------------------------------------------

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        saver = await self._async()
        return await saver.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        saver = await self._async()
        await saver.aput_writes(config, writes, task_id, task_path)

    async def aget_tuple(self, config: Any) -> Any:
        saver = await self._async()
        return await saver.aget_tuple(config)

    async def alist(
        self, config: Any, *, filter: Any = None, before: Any = None, limit: Any = None
    ) -> Any:
        """列出检查点（异步生成器）。"""

        saver = await self._async()
        async for item in saver.alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        saver = await self._async()
        await saver.adelete_thread(thread_id)

    # -- 关闭 -------------------------------------------------------------

    def close(self) -> None:
        """关闭同步连接。"""

        try:
            self._sync_conn.close()
        except Exception:  # pragma: no cover
            pass

    async def aclose(self) -> None:
        """关闭异步连接（aiosqlite 的工作线程不是守护线程，必须显式关闭）。"""

        if self._async_saver is not None:
            conn = getattr(self._async_saver, "conn", None)
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # pragma: no cover
                    pass
            self._async_saver = None
        self.close()


def build_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    """按配置构造检查点保存器。"""

    if settings.checkpointer == "memory":
        return InMemorySaver()
    return HybridSqliteSaver(settings.state_db)


class NamespacedSaver(BaseCheckpointSaver):
    """给底层检查点套一层 ``checkpoint_ns``。

    同一个 thread 下，不同专家 / 专家团的状态互不干扰，
    同时又共享同一份持久化存储。
    """

    def __init__(self, saver: BaseCheckpointSaver, namespace: str) -> None:
        self.saver = saver
        self.namespace = namespace

    def _ns(self, config: Any) -> Any:
        if not isinstance(config, dict):
            return config
        merged = dict(config)
        configurable = dict(merged.get("configurable") or {})
        current = configurable.get("checkpoint_ns") or ""
        if isinstance(current, (tuple, list)):
            current = "|".join(str(part) for part in current)
        configurable["checkpoint_ns"] = (
            f"{self.namespace}|{current}" if current else self.namespace
        )
        merged["configurable"] = configurable
        return merged

    # -- 同步 -------------------------------------------------------------

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return self.saver.put(self._ns(config), checkpoint, metadata, new_versions)

    def put_writes(
        self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        self.saver.put_writes(self._ns(config), writes, task_id, task_path)

    def get_tuple(self, config: Any) -> Any:
        return self.saver.get_tuple(self._ns(config))

    def list(self, config: Any, *, filter: Any = None, before: Any = None, limit: Any = None) -> Iterator[Any]:
        return self.saver.list(self._ns(config), filter=filter, before=before, limit=limit)

    def delete_thread(self, thread_id: str) -> None:
        # 底层接口只按 thread_id 删除，因此会清掉该会话下**所有**命名空间的检查点
        # （即整段会话记忆一起删除）——这正是"删除一个会话"的预期语义。
        self.saver.delete_thread(thread_id)

    # -- 异步 -------------------------------------------------------------

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return await self.saver.aput(self._ns(config), checkpoint, metadata, new_versions)

    async def aput_writes(
        self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        await self.saver.aput_writes(self._ns(config), writes, task_id, task_path)

    async def aget_tuple(self, config: Any) -> Any:
        return await self.saver.aget_tuple(self._ns(config))

    async def alist(
        self, config: Any, *, filter: Any = None, before: Any = None, limit: Any = None
    ) -> Any:
        """列出检查点（异步生成器）。"""

        async for item in self.saver.alist(
            self._ns(config), filter=filter, before=before, limit=limit
        ):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        await self.saver.adelete_thread(thread_id)

