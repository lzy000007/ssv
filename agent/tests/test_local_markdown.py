import asyncio


from ssv_agent.knowledge.backends.local_markdown import LocalMarkdownRetriever
from ssv_agent.knowledge.rule_chunks import (
    MAX_SECTION_LENGTH,
    rule_chunk_id,
    split_rule_clauses,
)


def test_local_markdown_returns_helmet_clause_first(tmp_path) -> None:
    for version, clause in (
        ("v1", "1.1 作业现场监控未佩戴安全帽。"),
        ("v2", "9.2.11 绝缘安全帽。"),
    ):
        path = tmp_path / "gb26860-helmet" / version / "rule.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            f"---\nkind: rule\nrule_id: gb26860-helmet\nversion: {version}\n---\n{clause}\n",
            encoding="utf-8",
        )
    result = asyncio.run(
        LocalMarkdownRetriever(tmp_path).retrieve(
            "作业现场监控未佩戴安全帽",
            top_k=2,
        )
    )

    assert result.success is True
    assert len(result.chunks) == 2
    assert result.chunks[0].metadata["source"] == "gb26860-helmet/v1/rule.md"
    assert result.chunks[0].metadata["rule_id"] == "gb26860-helmet"
    assert result.chunks[0].metadata["rule_version"] == "v1"
    assert result.chunks[0].metadata["section"] == "1.1"
    assert "未佩戴安全帽" in result.chunks[0].content
    assert result.chunks[1].metadata["source"] == "gb26860-helmet/v2/rule.md"
    assert result.chunks[1].metadata["rule_version"] == "v2"
    assert result.chunks[1].metadata["section"] == "9.2.11"
    assert "绝缘安全帽" in result.chunks[1].content


def test_local_markdown_filters_rule_identity_and_event_type(tmp_path) -> None:
    rules = tmp_path / "rules"
    (rules / "standard" / "v1").mkdir(parents=True)
    (rules / "standard" / "v1" / "rule.md").write_text(
        "---\nkind: rule\nrule_id: standard\nversion: v1\n"
        "event_type: event-a\n---\n\n1.1 目标条款。\n",
        encoding="utf-8",
    )
    (rules / "standard" / "v2").mkdir(parents=True)
    (rules / "standard" / "v2" / "rule.md").write_text(
        "---\nkind: rule\nrule_id: standard\nversion: v2\n"
        "event_type: event-b\n---\n\n1.1 目标条款。\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        LocalMarkdownRetriever(rules).retrieve(
            "目标条款",
            top_k=5,
            filters={"rule_id": "standard", "rule_version": "v2", "event_type": "event-b"},
        )
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].metadata["rule_version"] == "v2"
    assert result.chunks[0].metadata["event_types"] == ["event-b"]


def test_clause_splitter_ignores_pdf_page_noise_and_parses_markdown_numbers() -> None:
    passages = split_rule_clauses(
        "rules.md",
        "# 规则标题\n## ? 1 ?\n1 第一条。\n1\nGB26860—2011\n"
        "## ? 2 ?\n# 2 第二条。\n",
    )

    numbered = [passage for passage in passages if passage.section in {"1", "2"}]
    assert [passage.section for passage in numbered] == ["1", "2"]
    assert any(passage.section == "规则标题" for passage in passages)
    assert all("GB26860" not in passage.content for passage in numbered)


def test_long_clause_keeps_all_text_and_assigns_unique_stable_ids() -> None:
    text = "1.1 " + "安全帽佩戴要求" * 600
    passages = split_rule_clauses("long.md", text)
    assert len(passages) > 1
    assert all(len(passage.content) <= MAX_SECTION_LENGTH for passage in passages)
    assert "".join(passage.content for passage in passages) == text
    identifiers = [rule_chunk_id(passage) for passage in passages]
    assert len(set(identifiers)) == len(passages)
    assert identifiers == [
        rule_chunk_id(passage) for passage in split_rule_clauses("long.md", text)
    ]


def test_repeated_identical_sections_have_distinct_ids() -> None:
    passages = split_rule_clauses("repeated.md", "1.1 安全帽。\n1.1 安全帽。")
    assert len(passages) == 2
    assert passages[0].content == passages[1].content
    assert rule_chunk_id(passages[0]) != rule_chunk_id(passages[1])


def test_local_markdown_refreshes_when_only_front_matter_changes(tmp_path) -> None:
    path = tmp_path / "rule.md"
    template = (
        "---\nkind: rule\nrule_id: rule-1\nversion: {version}\n"
        "event_type: {event_type}\n---\n\n1.1 人员应佩戴安全帽。\n"
    )
    path.write_text(template.format(version="v1", event_type="event-a"), encoding="utf-8")
    retriever = LocalMarkdownRetriever(tmp_path)
    first = asyncio.run(retriever.retrieve("安全帽", filters={"rule_version": "v1"}))
    assert first.chunks
    path.write_text(template.format(version="v2", event_type="event-b"), encoding="utf-8")
    old = asyncio.run(retriever.retrieve("安全帽", filters={"rule_version": "v1"}))
    updated = asyncio.run(retriever.retrieve("安全帽", filters={"event_type": "event-b"}))
    assert old.chunks == []
    assert updated.chunks[0].metadata["rule_version"] == "v2"
    assert updated.chunks[0].chunk_id != first.chunks[0].chunk_id
