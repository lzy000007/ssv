from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ssv_agent.config import RecordingEvidenceConfig
from ssv_agent.event_store import EventLedger, JobKind
from ssv_agent.knowledge.backends.local_markdown import LocalMarkdownRetriever
from ssv_agent.knowledge.catalog import DEFAULT_RULES_DIR
from ssv_agent.knowledge.schema import Chunk, RetrievalResult
from ssv_agent.review_context import ReviewContext
from ssv_agent.evidence_provider import EvidenceArtifact
from ssv_agent.workers import RecordingEvidenceWorker, ReportWorker, ReviewWorker


def _record_event(
    db_path: Path,
    root: Path,
    *,
    rule_id: str | None = None,
    rule_facts: dict[str, str] | None = None,
) -> None:
    with EventLedger(
        db_path,
        evidence_roots=[str(root)],
        recording_evidence=RecordingEvidenceConfig(),
    ) as ledger:
        ledger.record(
            ReviewContext(
                event_id="evt-1",
                source="camera-1",
                timestamp_ms=1000,
                frame_id=1,
                event_type="person_without_helmet",
                severity="high",
                rule_id=rule_id,
                rule_version="v1" if rule_id else None,
                rule_facts=rule_facts if rule_facts is not None else {"helmet_area": "required"},
            )
        )


def _artifacts(root: Path) -> tuple[EvidenceArtifact, ...]:
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
        result.append(EvidenceArtifact(
            kind=kind, path=path,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            mime_type=mime_type, size=path.stat().st_size, mtime=path.stat().st_mtime,
        ))
    return tuple(result)


def _extract(
    db_path: Path,
    root: Path,
    *,
    rule_id: str | None = None,
    rule_facts: dict[str, str] | None = None,
) -> None:
    _record_event(db_path, root, rule_id=rule_id, rule_facts=rule_facts)

    class Extractor:
        def extract(self, case):
            return _artifacts(root)

    worker = RecordingEvidenceWorker(
        ledger_factory=lambda: EventLedger(
            db_path,
            evidence_roots=[str(root)],
            recording_evidence=RecordingEvidenceConfig(),
        ),
        extractor=Extractor(),
        worker_id="extractor",
        lease_ms=1000,
        max_retries=2,
        retry_delay_ms=0,
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
                query=query,
                backend="fake",
                chunks=[
                    Chunk(
                        chunk_id="chunk-1",
                        content="必须佩戴安全帽",
                        score=0.9,
                        metadata={
                            "source": "rules.md",
                            "rule_id": "r1",
                            "rule_version": "v1",
                            "section": "5.2",
                            "event_types": ["person_without_helmet"],
                        },
                    )
                ],
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
                "rule_id": "r1", "rule_version": "v1", "section": "5.2",
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


def test_missing_rules_downgrade_deterministic_review_without_rolling_back_evidence(
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
        review_job = ledger.claim_job(JobKind.REVIEW, "retry", 1000)

    assert case is not None and len(case.evidence) == 4
    assert case.review is not None
    assert case.review["verdict"] == "uncertain"
    assert case.review["confidence"] == 0.0
    assert review_job is None


@pytest.mark.parametrize("verdict", ["violation", "compliant", "uncertain"])
def test_site_markdown_review_completes_and_generates_report_without_policy(
    tmp_path, monkeypatch, verdict
) -> None:
    db_path = tmp_path / "events.db"
    _extract(
        db_path, tmp_path, rule_id="site-helmet", rule_facts={"helmet_required_area": "confirmed"}
    )
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(tmp_path / "outputs"))
    calls = []

    def runner(context, rule_context):
        calls.append(context.event_id)
        chunk = next(c for c in rule_context.chunks if c.metadata["section"] == "1.1")
        assert "review_policy" not in chunk.metadata
        evidence_id = context.evidence_ids[0]
        return json.dumps(
            {
                "verdict": verdict,
                "confidence": 0.9,
                "evidence_status": "available",
                "evidence_ids": [evidence_id],
                "claims": [{"text": "登记画面中的可核查观察", "evidence_ids": [evidence_id]}],
                "explanation": "按规则正文和实际证据判断，未使用策略或 checks",
                "rule_citations": [
                    {
                        "chunk_id": chunk.chunk_id,
                        **{
                            key: chunk.metadata[key]
                            for key in ("source", "rule_id", "rule_version", "section")
                        },
                    }
                ],
            }
        )

    def factory():
        return EventLedger(db_path, evidence_roots=[str(tmp_path)])

    worker = ReviewWorker(
        ledger_factory=factory,
        runner=runner,
        rule_retriever=LocalMarkdownRetriever(DEFAULT_RULES_DIR),
        worker_id="site-review",
        lease_ms=1000,
        max_retries=2,
        retry_delay_ms=0,
    )
    assert worker.run_once() is True and worker.run_once() is False
    assert calls == ["evt-1"]
    report_worker = ReportWorker(
        ledger_factory=factory,
        worker_id="site-report",
        lease_ms=1000,
        max_retries=2,
        retry_delay_ms=0,
    )
    assert report_worker.run_once() is True
    with factory() as ledger:
        case = ledger.get_case("evt-1")
        record = ledger.get_review_record("evt-1", 1)
        report = ledger.get_report_for_review("evt-1", record.review_id)
        assert case.review["verdict"] == verdict
        assert record.rule_citations[0]["rule_id"] == "site-helmet"
        assert "checks" not in record.rule_citations[0]
        assert record.evidence_ids
        assert ledger.claim_job(JobKind.INDEX, "indexer", 1000) is not None
        assert ledger.claim_job(JobKind.REVIEW, "retry", 1000) is None
        assert report is not None and Path(report.artifact_path).is_file()
