"""专家包加载器测试。"""

from __future__ import annotations

from pathlib import Path

from codeassistant.loader import (
    load_agent_doc,
    load_pack,
    load_packs,
    parse_frontmatter,
)

from .conftest import EXPERTS_DIR


def test_parse_frontmatter_splits_meta_and_body():
    text = "---\nname: demo\nmaxTurns: 30\n---\n\n# 标题\n\n正文内容"
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "demo"
    assert meta["maxTurns"] == 30
    assert "正文内容" in body
    assert "name: demo" not in body


def test_parse_frontmatter_without_marker():
    meta, body = parse_frontmatter("只有正文")
    assert meta == {}
    assert body == "只有正文"


def test_load_agent_doc_reads_all_fields(tmp_path: Path):
    path = tmp_path / "demo.md"
    path.write_text(
        "---\n"
        "name: demo-agent\n"
        "description: 一个演示 agent\n"
        "displayName:\n"
        '  en: "Demo"\n'
        '  zh: "演示"\n'
        "profession:\n"
        '  en: "Engineer"\n'
        '  zh: "工程师"\n'
        "maxTurns: 42\n"
        "---\n\n"
        "你是一个演示 agent。",
        encoding="utf-8",
    )
    doc = load_agent_doc(path)
    assert doc.name == "demo-agent"
    assert doc.label("zh") == "演示"
    assert doc.label("en") == "Demo"
    assert doc.title("zh") == "工程师"
    assert doc.max_turns == 42
    assert "你是一个演示 agent" in doc.system_prompt


def test_load_packs_finds_repository_experts():
    packs = load_packs(EXPERTS_DIR)
    names = {pack.name for pack in packs}
    assert "software-development-team" in names
    assert "macro-economic-analyst" in names
    assert "toutiao-finance-account-ops-team" in names


def test_load_pack_reads_agents_and_team_info():
    packs = {pack.name: pack for pack in load_packs(EXPERTS_DIR)}
    team = packs["software-development-team"]

    assert team.is_team
    assert team.lead_name == "software-development-team-team-lead"
    assert "sdt-backend" in team.member_names
    assert "sdt-architect" in team.agents
    assert team.agents["sdt-architect"].max_turns == 50


def test_single_agent_pack_is_not_team():
    packs = {pack.name: pack for pack in load_packs(EXPERTS_DIR)}
    single = packs["macro-economic-analyst"]
    assert not single.is_team
    assert single.lead_name == "macro-economic-analyst"
    assert "macro-economic-analyst" in single.agents


def test_load_pack_missing_manifest_raises(tmp_path: Path):
    import pytest

    with pytest.raises(FileNotFoundError):
        load_pack(tmp_path)


def test_agent_prompt_contains_markdown_body():
    packs = {pack.name: pack for pack in load_packs(EXPERTS_DIR)}
    prompt = packs["software-development-team"].agents["sdt-architect"].system_prompt
    assert "架构师" in prompt
    assert "ADR" in prompt
