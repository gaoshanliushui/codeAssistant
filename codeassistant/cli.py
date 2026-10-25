"""命令行入口。

::

    codeassistant chat                       # 交互式会话
    codeassistant run "把这个项目跑起来"       # 单次执行
    codeassistant experts                    # 列出专家 / 专家团
    codeassistant reload                     # 热重载专家包
    codeassistant serve                      # 启动 Web 服务
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .app import CodeAssistant
from .approval import ApprovalRequest
from .observability import EventType, StreamEvent, format_event
from .settings import Settings

# ---------------------------------------------------------------- 终端样式

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if _use_color() else text


# ---------------------------------------------------------------- 事件展示

EVENT_STYLE = {
    EventType.RUN_STARTED: (CYAN, "▶"),
    EventType.ORCHESTRATOR_ROUTE: (MAGENTA, "🧭"),
    EventType.EXPERT_STARTED: (CYAN, "🤖"),
    EventType.EXPERT_PLAN: (DIM, "📋"),
    EventType.EXPERT_TOOL_CALL: (YELLOW, "🔧"),
    EventType.EXPERT_TOOL_RESULT: (DIM, "↩"),
    EventType.EXPERT_APPROVAL: (RED, "🔒"),
    EventType.EXPERT_COMPLETED: (GREEN, "✅"),
    EventType.TEAM_DISPATCH: (MAGENTA, "📨"),
    EventType.TEAM_ROUND: (MAGENTA, "🔁"),
    EventType.TEAM_COMPLETED: (GREEN, "🏁"),
    EventType.RUN_COMPLETED: (GREEN, "🏁"),
    EventType.ERROR: (RED, "❌"),
}


def _event_detail(event: StreamEvent) -> str:
    """提取事件的一行摘要。"""

    payload = event.payload or {}
    kind = event.type
    if kind == EventType.RUN_STARTED:
        return f"任务：{payload.get('task', '')}（会话 {payload.get('session_id', '')}）"
    if kind == EventType.ORCHESTRATOR_ROUTE:
        return (
            f"{payload.get('route')} → {payload.get('target')}"
            + (f"（{payload['reason']}）" if payload.get("reason") else "")
        )
    if kind == EventType.EXPERT_PLAN:
        return str(payload.get("text", ""))[:160].replace("\n", " ")
    if kind == EventType.EXPERT_TOOL_CALL:
        args = json.dumps(payload.get("args", {}), ensure_ascii=False)
        return f"{payload.get('tool')}({args[:140]})"
    if kind == EventType.EXPERT_TOOL_RESULT:
        status = "ok" if payload.get("ok", True) else "failed"
        return f"{payload.get('tool')} [{status}] {str(payload.get('text', ''))[:110]}".replace(
            "\n", " "
        )
    if kind == EventType.EXPERT_APPROVAL:
        return f"等待审批：{payload.get('tool')}"
    if kind == EventType.EXPERT_COMPLETED:
        return f"{payload.get('status', 'ok')} · {str(payload.get('text', ''))[:110]}".replace(
            "\n", " "
        )
    if kind == EventType.TEAM_DISPATCH:
        return f"第 {payload.get('round')} 轮 → {payload.get('member')}：{payload.get('reason', '')}"
    if kind == EventType.TEAM_ROUND:
        return f"第 {payload.get('round')} 轮，成员 {', '.join(payload.get('members', []))}"
    if kind == EventType.TEAM_COMPLETED:
        return f"{payload.get('status', 'ok')} · {str(payload.get('text', ''))[:110]}".replace(
            "\n", " "
        )
    if kind == EventType.RUN_COMPLETED:
        return f"{payload.get('route', '')} → {payload.get('target', '')} [{payload.get('status', '')}]"
    if kind == EventType.ERROR:
        return str(payload.get("message", ""))
    return format_event(event)


def print_event(event: StreamEvent) -> None:
    """把事件打印成一行终端输出。"""

    color, icon = EVENT_STYLE.get(event.type, (DIM, "·"))
    scope = event.expert or event.team or event.node or "-"
    scope_text = _c(f"{scope}", color)
    detail = _event_detail(event)
    print(f"{icon} {scope_text} {_c(event.type, DIM):<34} {detail}")


async def terminal_approval(request: ApprovalRequest) -> tuple[bool, str]:
    """终端里的人工审批。"""

    print()
    print(_c("🔒 需要人工审批", RED))
    print(f"   专家    : {request.expert}")
    print(f"   工具    : {request.tool}")
    print(f"   参数    : {json.dumps(request.args, ensure_ascii=False)[:400]}")
    if request.reason:
        print(f"   原因    : {request.reason}")
    try:
        answer = input("   是否允许执行？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False, "用户中断"
    approved = answer in ("y", "yes", "是", "允许")
    return approved, "终端确认通过" if approved else "终端确认拒绝"


# ---------------------------------------------------------------- 命令实现


async def _run_once(
    app: CodeAssistant,
    task: str,
    *,
    expert: str | None,
    team: str | None,
    mode: str | None,
    session_id: str | None,
    as_json: bool,
) -> int:
    events: list[StreamEvent] = []
    async for event in app.stream_run(
        task,
        session_id=session_id,
        expert=expert,
        team=team,
        mode=mode,
        approval_callback=terminal_approval,
    ):
        events.append(event)
        if not as_json:
            print_event(event)

    final_event = next(
        (event for event in reversed(events) if event.type == EventType.RUN_COMPLETED),
        None,
    )
    error_event = next((event for event in events if event.type == EventType.ERROR), None)
    final = final_event.payload if final_event else {}
    if final_event is None:
        # 没有走到收尾：要么出错，要么根本没跑起来
        status = "failed"
        text = str(error_event.payload.get("message", "（本次没有产出）")) if error_event else "（本次没有产出）"
    else:
        status = str(final.get("status", "ok"))
        text = str(final.get("text", ""))
    output = final.get("output") or {}
    if as_json:
        print(
            json.dumps(
                {
                    "summary": text,
                    "status": status,
                    "route": final.get("route", ""),
                    "target": final.get("target", ""),
                    "deliverables": final.get("deliverables")
                    or output.get("deliverables")
                    or {},
                    "artifacts": final.get("artifacts") or output.get("artifacts") or [],
                    "findings": output.get("findings", []),
                    "events": [event.to_dict() for event in events],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        print()
        print(_c("── 最终结论 ──", BOLD))
        print(text)
    return 0 if status != "failed" else 1


async def _chat(app: CodeAssistant, session_id: str) -> int:
    """交互式会话。"""

    catalog = app.list_catalog()
    print(_c("CodeAssistant 交互会话", BOLD))
    print(_c(f"  工作区   : {app.settings.workspace}", DIM))
    print(_c(f"  模型     : {app.settings.provider}/{app.settings.model}", DIM))
    print(_c(f"  专家     : {len(catalog['experts'])} 位", DIM))
    print(_c(f"  专家团   : {len(catalog['teams'])} 个", DIM))
    print(_c("  输入 /exit 退出，/experts 查看专家，/help 查看帮助", DIM))
    print()

    while True:
        try:
            task = input(_c("你 › ", CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not task:
            continue
        if task in ("/exit", "/quit", ":q"):
            break
        if task == "/experts":
            _print_catalog(app)
            continue
        if task == "/help":
            print("可用命令：/experts 列出专家与专家团，/exit 退出")
            continue
        await _run_once(
            app,
            task,
            expert=None,
            team=None,
            mode=None,
            session_id=session_id,
            as_json=False,
        )
        print()
    return 0


def _print_skills(app: CodeAssistant) -> None:
    """列出技能及其绑定情况。"""

    registry = app.registry
    skills = registry.skills.list()
    if not skills:
        print(_c("还没有任何技能。", YELLOW))
        print(_c(f"  放到 {app.settings.skills_dir}/<技能名>/SKILL.md 即可", DIM))
        return

    bound_by: dict[str, list[str]] = {}
    for config in registry.list_experts():
        for name in config.skills:
            bound_by.setdefault(name, []).append(config.name)

    print(_c(f"技能（{len(skills)} 个）：", BOLD))
    for skill in skills:
        scope = ", ".join(skill.agents) if skill.agents else (
            "所有专家" if skill.default else "通用型专家（code-assistant）"
        )
        print(f"  {_c(skill.name, CYAN)}  {skill.description}")
        print(f"    来源：{skill.source}　绑定：{scope}")
        if skill.scripts:
            print(f"    脚本：{', '.join(skill.scripts)}")
        holders = bound_by.get(skill.name) or []
        if holders:
            print(f"    实际绑定：{', '.join(holders)}")


def _print_catalog(app: CodeAssistant) -> None:
    catalog = app.list_catalog()
    print(_c("专家团：", BOLD))
    for team in catalog["teams"]:
        print(
            f"  {_c(team['name'], MAGENTA)}  {team['display_name']}  "
            f"[{team['mode']}]  主理人={team['lead']}"
        )
        print(f"    成员：{', '.join(team['members'])}")
        if team["description"]:
            print(f"    {team['description'][:120]}")
    print()
    print(_c("专家：", BOLD))
    for expert in catalog["experts"]:
        hitl = f"  审批={','.join(expert['hitl'])}" if expert["hitl"] else ""
        print(
            f"  {_c(expert['name'], CYAN)}  {expert['display_name']}  "
            f"{expert['profession']}{hitl}"
        )
        if expert["description"]:
            print(f"    {expert['description'][:120]}")


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeassistant",
        description="基于 LangGraph 的多专家（专家 / 专家团）代码助手",
    )
    parser.add_argument("--workspace", help="工作区根目录（默认当前项目）")
    parser.add_argument("--provider", choices=["openai", "anthropic", "mock"], help="LLM Provider")
    parser.add_argument("--model", help="模型名")
    parser.add_argument("--checkpointer", choices=["memory", "sqlite"], help="状态持久化方式")

    sub = parser.add_subparsers(dest="command")

    run_cmd = sub.add_parser("run", help="执行一次任务")
    run_cmd.add_argument("task", help="任务描述")
    run_cmd.add_argument("--expert", help="指定专家（跳过路由）")
    run_cmd.add_argument("--team", help="指定专家团（跳过路由）")
    run_cmd.add_argument("--mode", choices=["supervisor", "parallel"], help="专家团协作模式")
    run_cmd.add_argument("--session", help="会话 ID（默认随机）")
    run_cmd.add_argument("--json", action="store_true", help="以 JSON 输出")

    chat_cmd = sub.add_parser("chat", help="交互式会话")
    chat_cmd.add_argument("--session", help="会话 ID（默认随机）")

    sub.add_parser("experts", help="列出专家与专家团")
    sub.add_parser("skills", help="列出技能及其绑定")
    sub.add_parser("reload", help="热重载专家包")

    serve_cmd = sub.add_parser("serve", help="启动 Web 服务")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    command = args.command or "chat"
    settings = Settings.load(
        workspace=args.workspace,
        provider=args.provider,
        **({"model": args.model} if args.model else {}),
        **({"checkpointer": args.checkpointer} if args.checkpointer else {}),
    )

    if command == "serve":
        from .server import serve

        serve(settings, host=args.host, port=args.port)
        return 0

    async def _amain() -> int:
        app = CodeAssistant(settings)
        try:
            if command == "experts":
                _print_catalog(app)
                return 0

            if command == "skills":
                _print_skills(app)
                return 0

            if command == "reload":
                report = app.reload()
                print(_c(f"已热重载专家包：{report.summary()}", GREEN))
                for name in report.touched_experts():
                    print(f"  专家变更：{name}")
                for name in report.touched_teams():
                    print(f"  专家团变更：{name}")
                return 0

            if command == "run":
                return await _run_once(
                    app,
                    args.task,
                    expert=args.expert,
                    team=args.team,
                    mode=args.mode,
                    session_id=args.session,
                    as_json=args.json,
                )

            return await _chat(app, args.session or "cli")
        finally:
            await app.aclose()

    return asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
