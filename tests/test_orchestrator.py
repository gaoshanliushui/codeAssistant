"""主编排器与应用门面测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeassistant.app import CodeAssistant
from codeassistant.memory import ThreadScope
from codeassistant.observability import EventType
from codeassistant.settings import Settings


@pytest.fixture
def app(settings: Settings) -> CodeAssistant:
    return CodeAssistant(settings)


async def test_app_lists_catalog(app: CodeAssistant):
    catalog = app.list_catalog()
    assert catalog["experts"], "应至少有一个专家"
    assert catalog["teams"], "应至少有一个专家团"
    names = {item["name"] for item in catalog["experts"]}
    assert "code-assistant" in names
    assert "sdt-backend" in names


async def test_orchestrator_routes_code_task_to_direct(app: CodeAssistant):
    async for event in app.stream_run("帮我重构一下这个 Python 函数"):
        if event.type == EventType.ORCHESTRATOR_ROUTE:
            assert event.payload["route"] == "direct"
            assert event.payload["target"] == "code-assistant"
            break


async def test_orchestrator_routes_named_team(app: CodeAssistant):
    async for event in app.stream_run("用 software-development-team 做一个电商系统"):
        if event.type == EventType.ORCHESTRATOR_ROUTE:
            assert event.payload["route"] == "team"
            assert event.payload["target"] == "software-development-team"
            break


async def test_orchestrator_emits_run_events(app: CodeAssistant):
    types = [event.type async for event in app.stream_run("看看代码")]
    assert EventType.RUN_STARTED in types
    assert EventType.ORCHESTRATOR_ROUTE in types
    assert EventType.RUN_COMPLETED in types


async def test_app_run_returns_result(app: CodeAssistant):
    result = await app.run("看看代码")
    assert result.route == "direct"
    assert result.target == "code-assistant"
    assert result.summary
    assert result.events


async def test_app_run_with_explicit_expert(app: CodeAssistant):
    result = await app.run("分析一下", expert="sdt-architect")
    assert result.route == "expert"
    assert result.target == "sdt-architect"


async def test_app_run_with_explicit_team(app: CodeAssistant):
    result = await app.run(
        "写稿", team="toutiao-finance-account-ops-team", mode="parallel"
    )
    assert result.route == "team"
    assert result.target == "toutiao-finance-account-ops-team"


async def test_app_run_exposes_structured_deliverables(app: CodeAssistant):
    """团队执行结果里应带上每个成员的交付物。"""

    result = await app.run("写稿", team="toutiao-finance-account-ops-team")
    deliverables = result.result.get("deliverables") or {}
    assert set(deliverables) == {
        "finance-data-collector",
        "finance-article-writer",
        "article-reviewer",
    }
    assert all("summary" in output for output in deliverables.values())


async def test_app_run_result_is_json_serializable(app: CodeAssistant):
    import json

    result = await app.run("看看代码")
    json.dumps(result.result, ensure_ascii=False, default=str)
    json.dumps([event.to_dict() for event in result.events], ensure_ascii=False, default=str)


async def test_orchestrator_route_is_reported_in_final_event(app: CodeAssistant):
    """自动路由时，最终事件里应带上真正命中的专家团。"""

    events = [
        event async for event in app.stream_run("用 software-development-team 做电商系统")
    ]
    final = next(
        event for event in reversed(events) if event.type == EventType.RUN_COMPLETED
    )
    assert final.payload["route"] == "team"
    assert final.payload["target"] == "software-development-team"


async def test_team_members_emit_per_expert_progress_events(app: CodeAssistant):
    """成员专家的进度事件必须带着专家维度冒泡出来（前端按专家展示进度）。"""

    events = [
        event
        async for event in app.stream_run(
            "写稿", team="toutiao-finance-account-ops-team", mode="supervisor"
        )
    ]

    completed = [e for e in events if e.type == EventType.EXPERT_COMPLETED]
    assert completed, "应收到成员专家的完成事件"
    assert {e.expert for e in completed} == {
        "finance-data-collector",
        "finance-article-writer",
        "article-reviewer",
    }
    assert all(e.team == "toutiao-finance-account-ops-team" for e in completed)


async def test_scope_from_namespace_parses_node_names():
    from codeassistant.observability import scope_from_namespace

    expert, team = scope_from_namespace(("member__sdt-backend:abc-123",))
    assert expert == "sdt-backend"
    assert team is None

    expert, team = scope_from_namespace(("expert__sdt-qa:xyz",))
    assert expert == "sdt-qa"

    expert, team = scope_from_namespace(("team__dev-team:1",))
    assert team == "dev-team"


async def test_tracer_receives_all_events(settings: Settings):
    """可观测性：追踪器能收到全部流式事件。"""

    class Recorder:
        def __init__(self) -> None:
            self.events = []

        def on_event(self, event) -> None:
            self.events.append(event)

    recorder = Recorder()
    app = CodeAssistant(settings)
    app.event_bus.tracer = recorder

    await app.run("看看代码")

    types = {event.type for event in recorder.events}
    assert EventType.RUN_STARTED in types
    assert EventType.ORCHESTRATOR_ROUTE in types
    assert EventType.RUN_COMPLETED in types


def test_merge_config_preserves_caller_config():
    """RunnableConfig 透传：保留调用方字段，thread_id 由本次执行决定。"""

    merged = CodeAssistant._merge_config(
        {"configurable": {"user_id": "u1"}, "tags": ["demo"], "metadata": {"a": 1}},
        "e:code-assistant:s1",
    )
    assert merged["tags"] == ["demo"]
    assert merged["metadata"] == {"a": 1}
    assert merged["configurable"]["user_id"] == "u1"
    assert merged["configurable"]["thread_id"] == "e:code-assistant:s1"


def test_merge_config_with_no_caller_config():
    merged = CodeAssistant._merge_config(None, "s:s1")
    assert merged == {"configurable": {"thread_id": "s:s1"}}


async def test_app_unknown_expert_reports_error(app: CodeAssistant):
    events = [event async for event in app.stream_run("x", expert="不存在")]
    assert any(event.type == EventType.ERROR for event in events)


async def test_app_approval_callback_is_invoked(
    tmp_path: Path, make_expert_config, settings: Settings
):
    """高危工具触发审批 → 审批回调给出结论 → 工具按结论执行。"""

    from codeassistant.configs import HitlPolicy
    from codeassistant.llm import MockChat

    app = CodeAssistant(settings)
    config = make_expert_config(
        "risky",
        tools=("write_file",),
        hitl=HitlPolicy(tools=("write_file",), mode="dynamic"),
    )
    app.registry.experts["risky"] = config
    app.factory._llm_factory = lambda _cfg: MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {
                "tool_calls": [
                    {
                        "name": "write_file",
                        "args": {"path": "note.txt", "content": "ok"},
                        "id": "c1",
                    }
                ]
            },
            {"content": "写好了"},
        ],
    )

    seen = []

    async def approval_callback(request):
        seen.append(request)
        return True, "测试通过"

    events = [
        event
        async for event in app.stream_run(
            "写个文件", expert="risky", approval_callback=approval_callback
        )
    ]

    assert seen, "审批回调应被调用"
    assert seen[0].tool == "write_file"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "ok"
    assert any(event.type == EventType.EXPERT_APPROVAL for event in events)


async def test_app_rejects_when_no_approval_channel(
    tmp_path: Path, make_expert_config, settings: Settings
):
    """没有审批通道时默认拒绝，工具不产生副作用。"""

    from codeassistant.configs import HitlPolicy
    from codeassistant.llm import MockChat

    app = CodeAssistant(settings)
    config = make_expert_config(
        "risky",
        tools=("write_file",),
        hitl=HitlPolicy(tools=("write_file",), mode="dynamic"),
    )
    app.registry.experts["risky"] = config
    app.factory._llm_factory = lambda _cfg: MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {
                "tool_calls": [
                    {
                        "name": "write_file",
                        "args": {"path": "note.txt", "content": "ok"},
                        "id": "c1",
                    }
                ]
            },
            {"content": "写好了"},
        ],
    )

    events = [
        event async for event in app.stream_run("写个文件", expert="risky")
    ]

    assert not (tmp_path / "note.txt").exists()
    assert any(event.type == EventType.EXPERT_APPROVAL for event in events)


async def test_app_static_mode_approves_batch(
    tmp_path: Path, make_expert_config, settings: Settings
):
    """static 模式：一次审批放行整批高危工具。"""

    from codeassistant.configs import HitlPolicy
    from codeassistant.llm import MockChat

    app = CodeAssistant(settings)
    config = make_expert_config(
        "batchy",
        tools=("write_file",),
        hitl=HitlPolicy(tools=("write_file",), mode="static"),
    )
    app.registry.experts["batchy"] = config
    app.factory._llm_factory = lambda _cfg: MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {
                "tool_calls": [
                    {
                        "name": "write_file",
                        "args": {"path": "batch.txt", "content": "ok"},
                        "id": "c1",
                    }
                ]
            },
            {"content": "写好了"},
        ],
    )

    seen = []

    async def approval_callback(request):
        seen.append(request)
        return True, "整批放行"

    events = [
        event
        async for event in app.stream_run(
            "写个文件", expert="batchy", approval_callback=approval_callback
        )
    ]

    assert seen, "static 模式也应触发审批"
    assert (tmp_path / "batch.txt").read_text(encoding="utf-8") == "ok"
    assert any(event.type == EventType.EXPERT_APPROVAL for event in events)


async def test_app_interrupt_before_gate(
    tmp_path: Path, make_expert_config, settings: Settings
):
    """``interrupt_before_tools``：工具执行前整体闸门，一次确认放行。"""

    from dataclasses import replace

    from codeassistant.configs import HitlPolicy
    from codeassistant.llm import MockChat

    app = CodeAssistant(settings)
    config = replace(
        make_expert_config(
            "gated",
            tools=("write_file",),
            hitl=HitlPolicy(tools=("write_file",), mode="dynamic"),
        ),
        interrupt_before_tools=True,
    )
    app.registry.experts["gated"] = config
    app.factory._llm_factory = lambda _cfg: MockChat(
        model="mock",
        script=[
            {"content": "计划"},
            {
                "tool_calls": [
                    {
                        "name": "write_file",
                        "args": {"path": "gated.txt", "content": "ok"},
                        "id": "c1",
                    }
                ]
            },
            {"content": "写好了"},
        ],
    )

    seen = []

    async def approval_callback(request):
        seen.append(request)
        return True, "闸门放行"

    async for _ in app.stream_run(
        "写个文件", expert="gated", approval_callback=approval_callback
    ):
        pass

    assert seen, "闸门应触发一次人工确认"
    assert len(seen) == 1, "闸门只应打断用户一次"
    assert (tmp_path / "gated.txt").read_text(encoding="utf-8") == "ok"


async def test_sessions_do_not_share_expert_memory(
    make_expert_config, settings: Settings
):
    """两个会话使用同一个专家时，记忆互相隔离。"""

    from codeassistant.llm import MockChat

    app = CodeAssistant(settings)
    config = make_expert_config("shared")
    app.registry.experts["shared"] = config
    app.factory._llm_factory = lambda _cfg: MockChat(
        model="mock", script=[{"content": "结论"}]
    )

    await app.run("第一轮", expert="shared", session_id="s1")
    await app.run("第二轮", expert="shared", session_id="s2")

    scope = ThreadScope(session_id="s1")
    other = ThreadScope(session_id="s2")
    assert scope.expert_thread("shared") != other.expert_thread("shared")
