from types import SimpleNamespace

from ssv_agent.review_context import ReviewContext, RuleRetrievalContext
from ssv_agent.runner import run_review
from ssv_agent.knowledge.schema import RetrievalResult


def test_run_review_passes_rule_context_to_prompt(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def prompt(context, rule_context=None):
        seen["message"] = f"{context.event_id}:{rule_context.query}"
        return "prompt"

    monkeypatch.setattr("ssv_agent.runner.build_review_prompt", prompt)

    class Client:
        def stream(self, **kwargs):
            assert kwargs["message"] == "prompt"
            return [SimpleNamespace(type="values", data={"messages": [
                {"type": "ai", "content": "answer"}
            ]})]

    rule_context = RuleRetrievalContext.from_result("helmet", RetrievalResult(query="helmet"))
    result = run_review(Client(), ReviewContext(
        event_id="evt-1", source="camera", timestamp_ms=1, frame_id=1
    ), rule_context)

    assert result == "answer"
    assert seen["message"] == "evt-1:helmet"
