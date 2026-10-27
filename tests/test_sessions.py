"""会话记录：落盘、列表、拉回、以及多轮连续对话。"""

from __future__ import annotations

import json
from pathlib import Path

from codeassistant.app import CodeAssistant
from codeassistant.sessions import SessionStore
from codeassistant.settings import Settings


# --------------------------------------------------------------------------
# 存储本身
# --------------------------------------------------------------------------


def test_store_persists_and_reloads(tmp_path: Path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.begin("s1", "帮我看看项目结构")
    store.append("s1", "expert", "这是项目结构", label="code-assistant")
    store.begin("s1", "再看看测试")

    # 重新打开应当能读回
    reloaded = SessionStore(path)
    sessions = reloaded.list()
    assert len(sessions) == 1
    assert sessions[0].id == "s1"
    assert sessions[0].title == "帮我看看项目结构"
    assert [t.role for t in sessions[0].turns] == ["user", "expert", "user"]


def test_store_file_is_valid_json(tmp_path: Path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.begin("s1", "任务")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sessions"][0]["id"] == "s1"


def test_store_survives_broken_file(tmp_path: Path):
    path = tmp_path / "sessions.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    store = SessionStore(path)
    assert store.list() == []
    # 还能继续写
    store.begin("s1", "任务")
    assert len(store.list()) == 1


def test_store_lists_newest_first(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.json")
    store.begin("old", "旧会话")
    store.begin("new", "新会话")
    assert [s.id for s in store.list()] == ["new", "old"]


def test_store_title_truncated(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.json")
    session = store.begin("s1", "很长的任务描述" * 20)
    assert len(session.title) <= 40


def test_store_keeps_target(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.json")
    store.begin("s1", "任务", expert="sdt-backend")
    session = store.get("s1")
    assert session is not None
    assert session.expert == "sdt-backend"
    assert session.team == ""


# --------------------------------------------------------------------------
# 应用层：跑一次任务就应该留下一条可回看的记录
# --------------------------------------------------------------------------


async def test_run_is_recorded_as_session(tmp_path: Path):
    settings = Settings.load(
        workspace=tmp_path,
        provider="mock",
        checkpointer="memory",
        sessions_file=tmp_path / "sessions.json",
    )
    app = CodeAssistant(settings)
    try:
        result = await app.run("看看这个项目的结构", expert="code-assistant")
        assert result.status == "ok"

        sessions = app.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["title"] == "看看这个项目的结构"

        detail = app.get_session(sessions[0]["id"])
        assert detail is not None
        roles = [t["role"] for t in detail["turns"]]
        assert roles[0] == "user"
        assert "expert" in roles
    finally:
        await app.aclose()


async def test_unknown_session_is_none(tmp_path: Path):
    settings = Settings.load(
        workspace=tmp_path,
        provider="mock",
        checkpointer="memory",
        sessions_file=tmp_path / "sessions.json",
    )
    app = CodeAssistant(settings)
    try:
        assert app.get_session("does-not-exist") is None
    finally:
        await app.aclose()
