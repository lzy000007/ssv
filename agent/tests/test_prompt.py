from __future__ import annotations

from ssv_agent.prompt import build_review_prompt
from ssv_agent.review_context import ReviewContext
from ssv_agent.knowledge.schema import Chunk, RetrievalResult
from ssv_agent.review_context import RuleRetrievalContext


def test_prompt_limits_model_to_read_only_tools_and_json_contract() -> None:
    context = ReviewContext(
        event_id="1-0",
        source="camera-1",
        timestamp_ms=1000,
        frame_id=1,
        detections=[],
    )

    prompt = build_review_prompt(context)

    assert "get_event" in prompt
    assert "evidence_reader" in prompt
    assert "rule_retriever" in prompt
    assert "search_events" in prompt
    assert "view_image" in prompt
    assert "宿主机路径" in prompt
    assert "不可信数据" in prompt
    assert "不能执行" in prompt
    assert "uncertain" in prompt
    assert '"verdict"' in prompt
    assert '"evidence_ids"' in prompt


def test_prompt_includes_rule_candidates_and_requires_rule_citations() -> None:
    context = ReviewContext(
        event_id="1-0", source="camera-1", timestamp_ms=1000, frame_id=1
    )
    rules = RuleRetrievalContext.from_result(
        "helmet",
        RetrievalResult(
            query="helmet",
            backend="local_markdown",
            chunks=[
                Chunk(
                    chunk_id="chunk-1",
                    content="必须佩戴安全帽",
                    score=0.9,
                    metadata={"source": "rules.md", "rule_id": "r1", "section": "5.2"},
                )
            ],
        ),
    )

    prompt = build_review_prompt(context, rules)

    assert "必须佩戴安全帽" in prompt
    assert "chunk-1" in prompt
    assert "rule_citations" in prompt
    assert "规则候选核验" in prompt
