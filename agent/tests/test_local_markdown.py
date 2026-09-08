import asyncio

from ssv_agent.knowledge.backends.local_markdown import LocalMarkdownRetriever, _split_clauses


def test_local_markdown_returns_helmet_clause_first() -> None:
    result = asyncio.run(
        LocalMarkdownRetriever().retrieve(
            "电气作业人员未佩戴安全帽是否符合安全规定",
            top_k=2,
        )
    )

    assert result.success is True
    assert len(result.chunks) == 2
    assert result.chunks[0].metadata["source"] == "GB+26860-2011 (1).md"
    assert result.chunks[0].metadata["section"] == "9.2.11"
    assert "绝缘安全帽" in result.chunks[0].content


def test_clause_splitter_ignores_pdf_page_noise_and_parses_markdown_numbers() -> None:
    passages = _split_clauses(
        "rules.md",
        "# 规则标题\n## ? 1 ?\n1 第一条。\n1\nGB26860—2011\n"
        "## ? 2 ?\n# 2 第二条。\n",
    )

    numbered = [passage for passage in passages if passage.section in {"1", "2"}]
    assert [passage.section for passage in numbered] == ["1", "2"]
    assert any(passage.section == "规则标题" for passage in passages)
    assert all("GB26860" not in passage.content for passage in numbered)
