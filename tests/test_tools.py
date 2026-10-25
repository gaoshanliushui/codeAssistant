"""沙箱与工具测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeassistant.tools import Sandbox, SandboxViolation, ToolRegistry


def test_sandbox_allows_paths_inside(tmp_path: Path):
    sandbox = Sandbox(tmp_path)
    target = sandbox.resolve("a/b.txt")
    assert str(target).startswith(str(tmp_path))


def test_sandbox_rejects_parent_escape(tmp_path: Path):
    sandbox = Sandbox(tmp_path)
    with pytest.raises(SandboxViolation):
        sandbox.resolve("../escape.txt")
    with pytest.raises(SandboxViolation):
        sandbox.resolve("/etc/passwd")


def test_sandbox_rejects_symlink_escape(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)

    sandbox = Sandbox(workspace)
    with pytest.raises(SandboxViolation):
        sandbox.resolve("link.txt")


def test_read_file_lists_content_with_line_numbers(tmp_path: Path, tool_registry: ToolRegistry):
    (tmp_path / "demo.txt").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    tool = tool_registry.get("read_file")
    output = tool.invoke({"path": "demo.txt"})
    assert "第一行" in output
    assert "共 3 行" in output


def test_read_file_supports_line_range(tmp_path: Path, tool_registry: ToolRegistry):
    (tmp_path / "demo.txt").write_text("a\nb\nc\nd\n", encoding="utf-8")
    output = tool_registry.get("read_file").invoke(
        {"path": "demo.txt", "start_line": 2, "end_line": 3}
    )
    assert "b" in output and "c" in output
    assert "a" not in output.split("\n", 1)[1]


def test_write_and_read_roundtrip(tmp_path: Path, tool_registry: ToolRegistry):
    tool_registry.get("write_file").invoke({"path": "out/x.txt", "content": "hello"})
    assert (tmp_path / "out" / "x.txt").read_text(encoding="utf-8") == "hello"


def test_edit_file_replaces_exact_text(tmp_path: Path, tool_registry: ToolRegistry):
    (tmp_path / "demo.py").write_text("value = 1\n", encoding="utf-8")
    output = tool_registry.get("edit_file").invoke(
        {"path": "demo.py", "old_string": "value = 1", "new_string": "value = 2"}
    )
    assert "已替换" in output
    assert (tmp_path / "demo.py").read_text(encoding="utf-8") == "value = 2\n"


def test_edit_file_requires_unique_match(tmp_path: Path, tool_registry: ToolRegistry):
    (tmp_path / "demo.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    output = tool_registry.get("edit_file").invoke(
        {"path": "demo.py", "old_string": "x = 1", "new_string": "x = 2"}
    )
    assert "出现 2 次" in output
    assert (tmp_path / "demo.py").read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_search_code_finds_matches(tmp_path: Path, tool_registry: ToolRegistry):
    (tmp_path / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("other = 1\n", encoding="utf-8")
    output = tool_registry.get("search_code").invoke({"query": "target"})
    assert "a.py:1" in output
    assert "b.py" not in output


def test_search_code_reports_no_match(tmp_path: Path, tool_registry: ToolRegistry):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    output = tool_registry.get("search_code").invoke({"query": "不存在的东西"})
    assert "未找到" in output


def test_ast_inspect_lists_definitions(tmp_path: Path, tool_registry: ToolRegistry):
    (tmp_path / "m.py").write_text(
        "class Foo:\n    def bar(self):\n        pass\n\n\ndef top():\n    pass\n",
        encoding="utf-8",
    )
    output = tool_registry.get("ast_inspect").invoke({"path": "m.py"})
    assert "class Foo" in output
    assert "def bar" in output
    assert "def top" in output


def test_ast_inspect_reports_syntax_error(tmp_path: Path, tool_registry: ToolRegistry):
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    output = tool_registry.get("ast_inspect").invoke({"path": "bad.py"})
    assert "语法错误" in output


def test_run_command_executes_in_workspace(tmp_path: Path, tool_registry: ToolRegistry):
    output = tool_registry.get("run_command").invoke({"command": "echo codeassistant-ok"})
    assert "codeassistant-ok" in output


def test_run_command_rejects_forbidden_patterns(tool_registry: ToolRegistry):
    output = tool_registry.get("run_command").invoke({"command": "rm -rf / --no-preserve-root"})
    assert "安全策略拒绝" in output


def test_write_file_outside_workspace_is_rejected(tool_registry: ToolRegistry):
    with pytest.raises(SandboxViolation):
        tool_registry.get("write_file").invoke({"path": "/tmp/escape-codeassistant.txt", "content": "x"})


def test_tool_registry_exposes_specs_and_risk(tool_registry: ToolRegistry):
    assert tool_registry.spec("read_file").risk == "low"
    assert tool_registry.spec("write_file").risk == "high"
    assert tool_registry.spec("run_command").requires_approval is True
    assert "write_file" in tool_registry.approval_tools()


def test_for_expert_filters_by_whitelist(tool_registry: ToolRegistry):
    tools = tool_registry.for_expert(("read_file", "search_code"))
    assert {tool.name for tool in tools} == {"read_file", "search_code"}


def test_for_expert_falls_back_to_default_set(tool_registry: ToolRegistry):
    tools = tool_registry.for_expert(())
    assert {tool.name for tool in tools} == {"read_file", "list_files", "search_code", "ast_inspect"}
