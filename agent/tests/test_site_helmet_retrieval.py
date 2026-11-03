from __future__ import annotations

import asyncio

from ssv_agent.knowledge.backends.local_markdown import LocalMarkdownRetriever
from ssv_agent.knowledge.catalog import DEFAULT_RULES_DIR
from ssv_agent.review_context import ReviewContext
from ssv_agent.search.rule_query import build_rule_filters, build_rule_query


def test_existing_site_rule_binding_retrieves_plain_markdown() -> None:
    context = ReviewContext(
        event_id="site-event",
        source="camera",
        timestamp_ms=1,
        frame_id=1,
        rule_id="site-helmet",
        rule_version="v1",
        event_type="person_without_helmet",
        rule_facts={"helmet_required_area": "confirmed"},
    )
    result = asyncio.run(
        LocalMarkdownRetriever(DEFAULT_RULES_DIR).retrieve(
            build_rule_query(context), filters=build_rule_filters(context)
        )
    )
    assert result.success and result.chunks
    clause = next(chunk for chunk in result.chunks if chunk.metadata["section"] == "1.1")
    assert clause.metadata["rule_id"] == "site-helmet"
    assert clause.metadata["rule_version"] == "v1"
    assert clause.metadata["event_types"] == ["person_without_helmet"]
    assert "review_policy" not in clause.metadata
    assert "未佩戴安全帽" in clause.content
