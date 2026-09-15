from __future__ import annotations

import json
from pathlib import Path

import pytest

from ssv_agent.knowledge.schema import Chunk, RetrievalResult
from ssv_agent.result import (
    ResultParseError,
    ReviewResult,
    parse_review_result,
    parse_result_markdown,
    validate_rule_citations,
    write_result_json,
    write_result_markdown,
)
from ssv_agent.review_context import RuleRetrievalContext


VALID = json.dumps(
    {
        "verdict": "violation",
        "confidence": 0.87,
        "evidence_status": "available",
        "evidence_ids": ["evidence-1"],
        "claims": [{"text": "A person is visible.", "evidence_ids": ["evidence-1"]}],
        "explanation": "The registered frame supports the finding.",
    }
)
LEGACY_UNCERTAIN = """结论: uncertain
置信度: 0.5
证据状态: missing
依据: 未登记可用证据。"""


def test_parse_valid() -> None:
    result = parse_review_result(VALID)
    assert result.verdict == "violation"
    assert result.confidence == 0.87
    assert result.evidence_status == "available"
    assert result.evidence_ids == ["evidence-1"]
    assert result.explanation


def test_parse_legacy_markdown_for_uncertain_result() -> None:
    assert parse_result_markdown(LEGACY_UNCERTAIN).verdict == "uncertain"


def test_parse_missing_evidence_requires_uncertain() -> None:
    text = VALID.replace("available", "missing").replace("violation", "compliant")
    with pytest.raises(ResultParseError):
        parse_review_result(text)


def test_parse_invalid_confidence() -> None:
    with pytest.raises(ResultParseError):
        parse_review_result(VALID.replace("0.87", "1.5"))


def test_write_result_markdown_atomic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(tmp_path / "outputs"))
    path = write_result_markdown("1-0", LEGACY_UNCERTAIN)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == LEGACY_UNCERTAIN
    assert path.parent.name == "1-0"


def test_write_result_json_atomic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(tmp_path / "outputs"))
    path = write_result_json("1-0", parse_review_result(VALID))
    assert json.loads(path.read_text(encoding="utf-8"))["verdict"] == "violation"


def test_result_artifacts_are_content_addressed_and_never_overwrite_each_other(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(tmp_path / "outputs"))
    first = parse_review_result(VALID)
    second = first.model_copy(update={"explanation": "A different review result."})
    first_json = write_result_json("case-1", first)
    same_json = write_result_json("case-1", first)
    second_json = write_result_json("case-1", second)
    first_markdown = write_result_markdown("case-1", LEGACY_UNCERTAIN)
    second_markdown = write_result_markdown("case-1", "结论: uncertain\n依据: another result")
    assert first_json == same_json
    assert first_json != second_json
    assert json.loads(first_json.read_text(encoding="utf-8"))["explanation"] == first.explanation
    assert json.loads(second_json.read_text(encoding="utf-8"))["explanation"] == second.explanation
    assert first_markdown != second_markdown
    assert first_markdown.read_text(encoding="utf-8") == LEGACY_UNCERTAIN


@pytest.mark.parametrize(
    "event_id", [".", "..", "../../escape", "nested/event", r"nested\\event", "x" * 129]
)
def test_unsafe_event_id_uses_a_stable_safe_output_directory(
    tmp_path: Path, monkeypatch, event_id: str
) -> None:
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(outputs))
    path = write_result_json(event_id, parse_review_result(VALID))
    assert path.is_relative_to(outputs.resolve())
    assert path.parent.name.startswith("event-")
    assert path == write_result_json(event_id, parse_review_result(VALID))


def test_absolute_event_id_cannot_escape_outputs_root(tmp_path: Path, monkeypatch) -> None:
    outputs = tmp_path / "outputs"
    outside = tmp_path / "outside"
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(outputs))
    path = write_result_json(str(outside), parse_review_result(VALID))
    assert path.is_relative_to(outputs.resolve())
    assert path.parent.name.startswith("event-")
    assert not outside.exists()


def _context(*chunks: Chunk, available: bool = True) -> RuleRetrievalContext:
    retrieval = RetrievalResult(
        chunks=list(chunks), query="helmet", backend="local_markdown", success=available
    )
    return RuleRetrievalContext.from_result("helmet", retrieval)


def _rule_result(**updates: object) -> ReviewResult:
    values: dict[str, object] = {
        "verdict": "violation", "confidence": 0.9, "evidence_status": "available",
        "evidence_ids": ["evidence-1"], "claims": [], "explanation": "规则和证据支持结论",
        "rule_citations": [{
            "chunk_id": "chunk-1", "source": "rules.md", "rule_id": "rule-1", "section": "5.2"
        }],
    }
    values.update(updates)
    return ReviewResult.model_validate(values)


def test_deterministic_result_requires_rule_citation() -> None:
    context = _context(Chunk(
        chunk_id="chunk-1", content="rule", score=0.9,
        metadata={"source": "rules.md", "rule_id": "rule-1", "section": "5.2"},
    ))
    with pytest.raises(ResultParseError):
        validate_rule_citations(_rule_result(rule_citations=[]), context)


def test_rule_citation_must_match_retrieved_chunk_metadata() -> None:
    context = _context(Chunk(
        chunk_id="chunk-1", content="rule", score=0.9,
        metadata={"source": "rules.md", "rule_id": "rule-1", "section": "5.2"},
    ))
    with pytest.raises(ResultParseError):
        validate_rule_citations(_rule_result(rule_citations=[{
            "chunk_id": "chunk-1", "source": "other.md", "rule_id": "rule-1", "section": "5.2"
        }]), context)


def test_unavailable_rules_only_allow_uncertain() -> None:
    context = _context(available=False)
    with pytest.raises(ResultParseError):
        validate_rule_citations(_rule_result(), context)
    uncertain = _rule_result(
        verdict="uncertain", confidence=0.4, rule_citations=[], explanation="没有可用规则依据"
    )
    assert validate_rule_citations(uncertain, context).verdict == "uncertain"
