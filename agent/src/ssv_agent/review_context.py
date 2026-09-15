"""事件复核输入模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from ssv_agent.knowledge.schema import Chunk, RetrievalResult


class Detection(BaseModel):
    """与 ssvpub 输出对应的检测条目。"""

    class_name: str = Field(validation_alias=AliasChoices("class", "class_name"))
    class_id: int
    confidence: float
    bbox: list[float] = Field(default_factory=list)
    track_id: int = -1
    track_state: str | int | None = None
    occluded: bool = False


class ReviewContext(BaseModel):
    """一次事件复核的原始上游事实，不补造时间线字段。"""

    event_id: str
    ingress_id: str | None = None
    source: str
    timestamp_ms: int
    frame_id: int
    stream_generation: int | None = None
    source_pts: int | None = None
    detections: list[Detection] = Field(default_factory=list)
    event_type: str | None = None
    severity: str | None = None
    rule_id: str | None = None
    rule_version: str | None = None
    rule_facts: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    frame_path: str | None = None
    clip_path: str | None = None
    question: str | None = None

    @classmethod
    def from_event(cls, entry_id: str, payload: dict[str, Any]) -> "ReviewContext":
        """从 Redis 事件 payload 构造复核上下文。"""
        detections = [
            Detection.model_validate(detection)
            for detection in payload.get("detections", [])
        ]

        def optional_int(*names: str) -> int | None:
            for name in names:
                if name in payload and payload[name] is not None:
                    return int(payload[name])
            return None

        event_type = (
            payload.get("event_type")
            if "event_type" in payload
            else payload.get("type")
        )

        return cls(
            event_id=str(payload.get("event_id") or entry_id),
            ingress_id=entry_id,
            source=payload.get("source", ""),
            timestamp_ms=int(payload.get("timestamp_ms", 0)),
            frame_id=int(payload.get("frame_id", 0)),
            stream_generation=optional_int("stream_generation", "generation"),
            source_pts=optional_int("source_pts", "pts"),
            detections=detections,
            event_type=event_type,
            severity=payload.get("severity"),
            rule_id=payload.get("rule_id"),
            rule_version=payload.get("rule_version"),
            rule_facts=payload.get("rule_facts", {}),
            frame_path=payload.get("frame_path"),
            clip_path=payload.get("clip_path"),
            question=payload.get("question"),
        )


@dataclass(frozen=True)
class RuleRetrievalContext:
    """单次复验使用的规则候选，不写入事件权威状态。"""

    query: str
    result: RetrievalResult
    chunks: tuple[Chunk, ...]
    available: bool
    error_message: str | None = None

    @classmethod
    def from_result(cls, query: str, result: RetrievalResult) -> "RuleRetrievalContext":
        available = result.success and bool(result.chunks)
        return cls(
            query=query,
            result=result,
            chunks=tuple(result.chunks),
            available=available,
            error_message=result.error_message,
        )
