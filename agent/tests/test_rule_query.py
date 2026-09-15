from ssv_agent.review_context import ReviewContext
from ssv_agent.search.rule_query import build_rule_query


def test_build_rule_query_contains_authoritative_event_facts() -> None:
    context = ReviewContext(
        event_id="evt-1",
        source="camera-1",
        timestamp_ms=1000,
        frame_id=3,
        event_type="person_without_helmet",
        severity="high",
        rule_id="GB26860-2011-5.2.2",
        rule_facts={"helmet_required": True},
        detections=[
            {
                "class": "person",
                "class_id": 0,
                "confidence": 0.91,
                "bbox": [1, 2, 3, 4],
            }
        ],
        question="是否违反安全帽佩戴规则？",
    )

    query = build_rule_query(context)

    assert "person_without_helmet" in query
    assert "GB26860-2011-5.2.2" in query
    assert "helmet_required" in query
    assert "person" in query
    assert "high" in query
    assert "是否违反安全帽佩戴规则" in query
