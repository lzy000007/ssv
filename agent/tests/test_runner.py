from types import SimpleNamespace

import pytest
from langgraph.errors import GraphRecursionError

from ssv_agent.review_context import ReviewContext, RuleRetrievalContext
from ssv_agent.runner import ModelCallError, classify_model_error, run_review
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
            assert kwargs["recursion_limit"] == 256
            return [SimpleNamespace(type="values", data={"messages": [
                {"type": "ai", "content": "answer"}
            ]})]

    rule_context = RuleRetrievalContext.from_result("helmet", RetrievalResult(query="helmet"))
    result = run_review(Client(), ReviewContext(
        event_id="evt-1", source="camera", timestamp_ms=1, frame_id=1
    ), rule_context)

    assert result == "answer"
    assert seen["message"] == "evt-1:helmet"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("403 INSUFFICIENT_BALANCE", "model_insufficient_balance"),
        ("401 invalid api key", "model_authentication_failed"),
        ("403 forbidden", "model_access_denied"),
        ("429 rate limit exceeded", "model_rate_limited"),
        ("request timed out", "model_timeout"),
    ],
)
def test_classify_model_error_returns_stable_code_without_raw_message(
    message: str, code: str
) -> None:
    error = classify_model_error(RuntimeError(message))

    assert error.code == code
    assert message not in error.user_message
    assert error.diagnostic == message


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("quota", "model_insufficient_balance"),
        ("auth", "model_authentication_failed"),
        ("burst_rate", "model_rate_limited"),
    ],
)
@pytest.mark.parametrize("event_type", ["values", "messages-tuple"])
def test_run_review_surfaces_provider_fallback_as_model_error(reason, code, event_type) -> None:
    class Client:
        def stream(self, **kwargs):
            message = {
                "type": "ai",
                "content": "provider rejected the request",
                "additional_kwargs": {
                    "deerflow_error_fallback": True,
                    "error_reason": reason,
                    "error_detail": "Error code: 403 - quota",
                },
            }
            data = {"messages": [message]} if event_type == "values" else message
            return [SimpleNamespace(type=event_type, data=data)]

    with pytest.raises(ModelCallError) as raised:
        run_review(
            Client(),
            ReviewContext(event_id="evt-1", source="camera", timestamp_ms=1, frame_id=1),
        )

    assert raised.value.code == code
    assert "quota" not in raised.value.user_message


@pytest.mark.parametrize("event_type", ["values", "messages-tuple"])
def test_run_review_classifies_step_limit_without_returning_partial_output(event_type) -> None:
    upstream = GraphRecursionError("Recursion limit reached; internal diagnostic 401")

    class Client:
        def stream(self, **kwargs):
            assert kwargs["recursion_limit"] == 384
            message = {"type": "ai", "content": "partial answer"}
            data = {"messages": [message]} if event_type == "values" else message
            yield SimpleNamespace(type=event_type, data=data)
            raise upstream

    with pytest.raises(ModelCallError) as raised:
        run_review(
            Client(),
            ReviewContext(event_id="evt-1", source="camera", timestamp_ms=1, frame_id=1),
            recursion_limit=384,
        )

    assert raised.value.code == "review_step_limit_exceeded"
    assert raised.value.diagnostic == str(upstream)
    assert "401" not in raised.value.user_message
    assert raised.value.__cause__ is upstream


def test_run_review_marks_empty_stream_as_no_response() -> None:
    class Client:
        def stream(self, **kwargs):
            return []

    with pytest.raises(ModelCallError) as raised:
        run_review(
            Client(),
            ReviewContext(event_id="evt-1", source="camera", timestamp_ms=1, frame_id=1),
        )

    assert raised.value.code == "model_no_response"
    assert str(raised.value) == "模型服务未返回可解析结果。"
