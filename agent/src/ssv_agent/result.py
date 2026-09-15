"""复核结果解析与落盘。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from ssv_agent.review_context import RuleRetrievalContext


_SAFE_EVENT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def _outputs_root() -> Path:
    root = Path(os.getenv("SSV_OUTPUTS_DIR", "outputs")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _event_output_dir(root: Path, event_id: str) -> Path:
    """为事件选择受 outputs root 约束的稳定目录。"""
    if event_id not in {".", ".."} and _SAFE_EVENT_ID.fullmatch(event_id):
        directory_name = event_id
    else:
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        directory_name = f"event-{digest}"

    directory = (root / directory_name).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("event output directory escapes outputs root")
    directory.mkdir(parents=True, exist_ok=True)
    resolved = directory.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("event output directory escapes outputs root")
    return resolved


def _write_artifact(root: Path, directory: Path, filename: str, content: bytes) -> Path:
    """原子写入内容寻址 artifact，已有同内容目标直接复用。"""
    target = directory / filename
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("result artifact escapes outputs root")
    if target.exists():
        return resolved

    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".result-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("result artifact escapes outputs root")
    return resolved


class ReviewClaim(BaseModel):
    """模型给出的可追溯陈述。"""

    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class RuleCitation(BaseModel):
    """模型对本次规则检索候选的引用。"""

    chunk_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    section: str = Field(min_length=1)


class ReviewResult(BaseModel):
    """已通过结构化契约校验的复核结果。"""

    verdict: Literal["compliant", "violation", "uncertain"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_status: Literal["available", "missing"]
    evidence_ids: list[str] = Field(default_factory=list)
    claims: list[ReviewClaim] = Field(default_factory=list)
    rule_citations: list[RuleCitation] = Field(default_factory=list)
    explanation: str
    policy_id: str | None = None
    model_id: str | None = None

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> "ReviewResult":
        if self.evidence_status == "missing" and self.verdict != "uncertain":
            raise ValueError("证据缺失时结论必须为 uncertain")
        if self.verdict != "uncertain" and not self.evidence_ids:
            raise ValueError("非 uncertain 结论必须引用证据")
        claim_evidence_ids = {
            evidence_id for claim in self.claims for evidence_id in claim.evidence_ids
        }
        if not claim_evidence_ids.issubset(self.evidence_ids):
            raise ValueError("claims 只能引用结果声明的证据")
        return self


class ResultParseError(ValueError):
    """结果模板解析/校验失败。"""


def validate_rule_citations(
    result: ReviewResult,
    rule_context: RuleRetrievalContext,
) -> ReviewResult:
    """校验确定性结论只引用本次检索得到且元数据一致的规则片段。"""
    if result.verdict == "uncertain":
        return result
    if not rule_context.available:
        raise ResultParseError("规则不可用时结论必须为 uncertain")
    if not result.rule_citations:
        raise ResultParseError("确定性结论必须引用规则")

    candidates = {
        chunk.chunk_id: chunk
        for chunk in rule_context.chunks
    }
    for citation in result.rule_citations:
        chunk = candidates.get(citation.chunk_id)
        if chunk is None:
            raise ResultParseError("规则引用的 chunk 不在本次检索结果中")
        metadata = chunk.metadata
        if (
            citation.source != metadata.get("source")
            or citation.rule_id != metadata.get("rule_id")
            or citation.section != metadata.get("section")
        ):
            raise ResultParseError("规则引用元数据与检索结果不一致")
    return result


def parse_result_markdown(text: str) -> ReviewResult:
    """兼容解析历史 ``result.md`` 模板。"""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    verdict = fields.get("结论")
    confidence_raw = fields.get("置信度")
    evidence_status = fields.get("证据状态")
    explanation = fields.get("依据", "")
    evidence_ids = [
        item.strip()
        for item in fields.get("证据ID", "").replace("，", ",").split(",")
        if item.strip()
    ]

    if verdict not in {"compliant", "violation", "uncertain"}:
        raise ResultParseError(f"非法结论: {verdict!r}")
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError) as exc:
        raise ResultParseError(f"非法置信度: {confidence_raw!r}") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ResultParseError(f"置信度越界: {confidence}")
    if evidence_status not in {"available", "missing"}:
        raise ResultParseError(f"非法证据状态: {evidence_status!r}")
    if not explanation.strip():
        raise ResultParseError("依据不能为空")
    try:
        return ReviewResult(
            verdict=verdict,
            confidence=confidence,
            evidence_status=evidence_status,
            evidence_ids=evidence_ids,
            explanation=explanation,
        )
    except ValidationError as exc:
        raise ResultParseError(str(exc)) from exc


def parse_review_result(text: str) -> ReviewResult:
    """优先解析新 JSON 契约，历史 Markdown 仅作为兼容回退。"""
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        _, _, candidate = candidate.partition("\n")
        candidate = candidate.rsplit("```", 1)[0].strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return parse_result_markdown(text)
    if not isinstance(payload, dict):
        raise ResultParseError("JSON 复核结果必须是对象")
    try:
        return ReviewResult.model_validate(payload)
    except ValidationError as exc:
        raise ResultParseError(str(exc)) from exc


def write_result_markdown(event_id: str, text: str) -> Path:
    """原子写入内容寻址的历史 Markdown artifact。"""
    root = _outputs_root()
    content = text.encode("utf-8")
    directory = _event_output_dir(root, event_id)
    digest = hashlib.sha256(content).hexdigest()
    return _write_artifact(root, directory, f"result-{digest}.md", content)


def write_result_json(event_id: str, result: ReviewResult) -> Path:
    """原子写入内容寻址的结构化复核结果。"""
    root = _outputs_root()
    content = (
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    directory = _event_output_dir(root, event_id)
    digest = hashlib.sha256(content).hexdigest()
    return _write_artifact(root, directory, f"result-{digest}.json", content)
