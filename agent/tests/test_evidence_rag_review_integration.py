from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ssv_agent.config import RecordingEvidenceConfig
from ssv_agent.event_store import EventLedger, JobKind
from ssv_agent.knowledge.schema import Chunk, RetrievalResult
from ssv_agent.review_context import ReviewContext
from ssv_agent.recording_evidence import RecordingEvidenceArtifact
from ssv_agent.workers import RecordingEvidenceWorker, ReviewWorker


def _record_event(db_path: Path, root: Path) -> None:
    with EventLedger(
        db_path, evidence_roots=[str(root)],
        recording_evidence=RecordingEvidenceConfig(enabled=True),
    ) as ledger:
        ledger.record(ReviewContext(
            event_id="evt-1", source="camera-1", timestamp_ms=1000, frame_id=1,
            event_type="person_without_helmet", severity="high",
        ))


def _artifacts(root: Path) -> tuple[RecordingEvidenceArtifact, ...]:
    directory = root / "derived" / "evt-1"
    directory.mkdir(parents=True)
    values = [
        ("clip", "context.mp4", "video/mp4"),
        ("frame", "frame-01.jpg", "image/jpeg"),
        ("frame", "frame-02.jpg", "image/jpeg"),
        ("frame", "frame-03.jpg", "image/jpeg"),
    ]
    result = []
    for kind, name, mime_type in values:
        path = directory / name
        path.write_bytes(name.encode())
        result.append(RecordingEvidenceArtifact(
            kind=kind, path=path,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            mime_type=mime_type, size=path.stat().st_size, mtime=path.stat().st_mtime,
        ))
    return tuple(result)


def _extract(db_path: Path, root: Path) -> None:
    _record_event(db_path, root)

    class Extractor:
        def extract(self, case):
            return _artifacts(root)

    worker = RecordingEvidenceWorker(
        ledger_factory=lambda: EventLedger(
            db_path, evidence_roots=[str(root)],
            recording_evidence=RecordingEvidenceConfig(enabled=True),
        ),
        extractor=Extractor(), worker_id="extractor", lease_ms=1000,
        max_retries=2, retry_delay_ms=0,
    )
    assert worker.run_once() is True


def test_evidence_rules_review_then_index_job_form_one_traceable_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "events.db"
    _extract(db_path, tmp_path)
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(tmp_path / "outputs"))
    calls: list[str] = []

    class Retriever:
        async def retrieve(self, query, *, top_k=5, filters=None):
            calls.append("retrieve")
            return RetrievalResult(
                query=query, backend="fake", chunks=[Chunk(
                    chunk_id="chunk-1", content="必须佩戴安全帽", score=0.9,
                    metadata={"source": "rules.md", "rule_id": "r1", "section": "5.2"},
                )],
            )

    def runner(context, rule_context):
        calls.append("runner")
        return json.dumps({
            "verdict": "violation", "confidence": 0.9,
            "evidence_status": "available",
            "evidence_ids": context.evidence_ids,
            "claims": [], "explanation": "证据和规则均可追溯",
            "rule_citations": [{
                "chunk_id": "chunk-1", "source": "rules.md",
                "rule_id": "r1", "section": "5.2",
            }],
        })

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path, evidence_roots=[str(tmp_path)]),
        runner=runner, rule_retriever=Retriever(), worker_id="reviewer",
        lease_ms=1000, max_retries=2, retry_delay_ms=0,
    )
    assert worker.run_once() is True
    assert calls == ["retrieve", "runner"]

    with EventLedger(db_path, evidence_roots=[str(tmp_path)]) as ledger:
        case = ledger.get_case("evt-1")
        index_job = ledger.claim_job(JobKind.INDEX, "indexer", 1000)
        review_job = ledger.claim_job(JobKind.REVIEW, "another", 1000)

    assert case is not None and len(case.evidence) == 4
    assert case.review is not None and case.review["verdict"] == "violation"
    assert index_job is not None
    assert review_job is None


def test_missing_rules_reject_deterministic_review_without_rolling_back_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "events.db"
    _extract(db_path, tmp_path)
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(tmp_path / "outputs"))

    class EmptyRetriever:
        async def retrieve(self, query, *, top_k=5, filters=None):
            return RetrievalResult(query=query, backend="fake", chunks=[])

    def runner(context, rule_context):
        return json.dumps({
            "verdict": "violation", "confidence": 0.9,
            "evidence_status": "available", "evidence_ids": context.evidence_ids,
            "claims": [], "explanation": "没有规则引用", "rule_citations": [],
        })

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path, evidence_roots=[str(tmp_path)]),
        runner=runner, rule_retriever=EmptyRetriever(), worker_id="reviewer",
        lease_ms=1000, max_retries=2, retry_delay_ms=0,
    )
    assert worker.run_once() is True

    with EventLedger(db_path, evidence_roots=[str(tmp_path)]) as ledger:
        case = ledger.get_case("evt-1")
        retry = ledger.claim_job(JobKind.REVIEW, "retry", 1000)

    assert case is not None and len(case.evidence) == 4
    assert case.review is None
    assert retry is not None
