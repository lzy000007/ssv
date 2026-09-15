from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import ssv_agent.event_store.ledger as ledger_module
import ssv_agent.workers as workers_module
from ssv_agent.event_store import DurableJob, EventLedger, JobKind, JobState
from ssv_agent.review_context import ReviewContext
from ssv_agent.prompt import build_review_prompt
from ssv_agent.result import ReviewResult
from ssv_agent.knowledge.schema import Chunk, RetrievalResult
from ssv_agent.recording_evidence import RecordingEvidenceArtifact, RecordingEvidenceError
from ssv_agent.config import RecordingEvidenceConfig
from ssv_agent.workers import _LeaseHeartbeat, IndexWorker, ReviewWorker, RecordingEvidenceWorker


@pytest.fixture(autouse=True)
def _allow_test_evidence_roots(monkeypatch, tmp_path: Path) -> None:
    """每个 worker 测试显式允许自己的临时证据目录。"""
    monkeypatch.setenv("SSV_EVIDENCE_ROOTS", json.dumps([str(tmp_path)]))


def _record_case(db_path: Path, evidence_path: Path) -> str:
    evidence_path.write_bytes(b"frame")
    with EventLedger(db_path) as ledger:
        ledger.record(
            ReviewContext(
                event_id="case-1",
                ingress_id="1-0",
                source="camera-1",
                timestamp_ms=1000,
                frame_id=4,
                frame_path=str(evidence_path),
            )
        )
        case = ledger.get_case("case-1")
    assert case is not None
    return case.evidence[0].evidence_id


def _requeue_claim_for_a_stricter_retry_limit(db_path: Path, kind: JobKind) -> None:
    with EventLedger(db_path) as ledger:
        claimed = ledger.claim_job(kind, "previous-worker", lease_ms=1_000)
        assert claimed is not None
        ledger.fail_job(
            claimed.job_id,
            "previous-worker",
            claimed.attempts,
            "temporary failure",
            max_retries=3,
            retry_delay_ms=0,
        )


def _record_recording_case(db_path: Path, root: Path) -> None:
    with EventLedger(
        db_path,
        evidence_roots=[str(root)],
        recording_evidence=RecordingEvidenceConfig(enabled=True),
    ) as ledger:
        ledger.record(
            ReviewContext(
                event_id="case-1",
                ingress_id="1-0",
                source="camera-1",
                timestamp_ms=1000,
                frame_id=4,
            )
        )


def _recording_artifacts(root: Path) -> tuple[RecordingEvidenceArtifact, ...]:
    derived = root / "derived" / "case-1"
    derived.mkdir(parents=True)
    artifacts = []
    for kind, name, mime in [("clip", "context.mp4", "video/mp4"), ("frame", "frame-01.jpg", "image/jpeg"), ("frame", "frame-02.jpg", "image/jpeg"), ("frame", "frame-03.jpg", "image/jpeg")]:
        path = derived / name
        path.write_bytes(name.encode())
        artifacts.append(RecordingEvidenceArtifact(kind=kind, path=path, sha256=__import__("hashlib").sha256(path.read_bytes()).hexdigest(), mime_type=mime, size=path.stat().st_size, mtime=path.stat().st_mtime))
    return tuple(artifacts)


def test_recording_worker_completes_extract_before_review_is_claimable(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _record_recording_case(db_path, tmp_path)
    calls: list[str] = []

    class Extractor:
        def extract(self, case):
            calls.append(case.event_id)
            return _recording_artifacts(tmp_path)

    worker = RecordingEvidenceWorker(
        ledger_factory=lambda: EventLedger(db_path, evidence_roots=[str(tmp_path)], recording_evidence=RecordingEvidenceConfig(enabled=True)),
        extractor=Extractor(), worker_id="extract-test", lease_ms=1_000, max_retries=2, retry_delay_ms=0,
    )
    assert worker.run_once() is True
    assert calls == ["case-1"]
    with EventLedger(db_path, evidence_roots=[str(tmp_path)], recording_evidence=RecordingEvidenceConfig(enabled=True)) as ledger:
        assert ledger.claim_job(JobKind.REVIEW, "review", 1000) is not None


def test_recording_worker_marks_retryable_failure_and_does_not_extract_after_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _record_recording_case(db_path, tmp_path)
    calls = 0

    class Extractor:
        def extract(self, case):
            nonlocal calls
            calls += 1
            raise RecordingEvidenceError("window unavailable", retryable=True)

    worker = RecordingEvidenceWorker(
        ledger_factory=lambda: EventLedger(db_path, evidence_roots=[str(tmp_path)], recording_evidence=RecordingEvidenceConfig(enabled=True)),
        extractor=Extractor(), worker_id="extract-test", lease_ms=1_000, max_retries=1, retry_delay_ms=0,
    )
    assert worker.run_once() is True
    assert calls == 1


def test_recording_worker_nonretryable_failure_marks_event_manual_review(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _record_recording_case(db_path, tmp_path)

    class Extractor:
        def extract(self, case):
            raise RecordingEvidenceError("unsafe recording input", retryable=False)

    worker = RecordingEvidenceWorker(
        ledger_factory=lambda: EventLedger(db_path, evidence_roots=[str(tmp_path)], recording_evidence=RecordingEvidenceConfig(enabled=True)),
        extractor=Extractor(), worker_id="extract-test", lease_ms=1_000, max_retries=3, retry_delay_ms=0,
    )
    assert worker.run_once() is True
    with EventLedger(db_path, evidence_roots=[str(tmp_path)], recording_evidence=RecordingEvidenceConfig(enabled=True)) as ledger:
        case = ledger.get_case("case-1")
        job = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, "other", 1000)
    assert case is not None and case.status == "manual_review"
    assert job is None


def test_recording_worker_exhausted_reclaimed_job_marks_event_manual_review(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _record_recording_case(db_path, tmp_path)
    with EventLedger(db_path) as ledger:
        claimed = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, "old", 1000)
        assert claimed is not None
        ledger.fail_evidence_extract_job(claimed, "old", "temporary", max_retries=5, retry_delay_ms=0)

    calls = 0
    class Extractor:
        def extract(self, case):
            nonlocal calls
            calls += 1
            return _recording_artifacts(tmp_path)

    worker = RecordingEvidenceWorker(
        ledger_factory=lambda: EventLedger(db_path, evidence_roots=[str(tmp_path)], recording_evidence=RecordingEvidenceConfig(enabled=True)),
        extractor=Extractor(), worker_id="extract-test", lease_ms=1_000, max_retries=1, retry_delay_ms=0,
    )
    assert worker.run_once() is True
    assert calls == 0
    with EventLedger(db_path, evidence_roots=[str(tmp_path)], recording_evidence=RecordingEvidenceConfig(enabled=True)) as ledger:
        case = ledger.get_case("case-1")
    assert case is not None and case.status == "manual_review"


def test_recording_worker_stop_event_prevents_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _record_recording_case(db_path, tmp_path)
    stopping = threading.Event()
    stopping.set()
    calls = 0

    class Extractor:
        def extract(self, case):
            nonlocal calls
            calls += 1
            return _recording_artifacts(tmp_path)

    worker = RecordingEvidenceWorker(
        ledger_factory=lambda: EventLedger(db_path, evidence_roots=[str(tmp_path)], recording_evidence=RecordingEvidenceConfig(enabled=True)),
        extractor=Extractor(), worker_id="extract-test", lease_ms=1_000, max_retries=1, retry_delay_ms=0,
    )
    worker.run(stopping)
    assert calls == 0


def test_review_prompt_requires_evidence_reader_and_wall_clock_context() -> None:
    prompt = build_review_prompt(
        ReviewContext(event_id="case-1", source="camera-1", timestamp_ms=1000, frame_id=1)
    )
    assert "开始复核前必须先调用 evidence_reader" in prompt
    assert "wall_clock_approximate" in prompt
    assert "不得声称它们是检测帧" in prompt


def test_review_worker_appends_valid_result_and_completes_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "events.db"
    evidence_id = _record_case(db_path, tmp_path / "frame.jpg")
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(tmp_path / "outputs"))

    def runner(context: ReviewContext) -> str:
        assert context.event_id == "case-1"
        assert context.evidence_ids == [evidence_id]
        return json.dumps(
            {
                "verdict": "violation",
                "confidence": 0.91,
                "evidence_status": "available",
                "evidence_ids": [evidence_id],
                "claims": [
                    {"text": "A worker is visible without a helmet.", "evidence_ids": [evidence_id]}
                ],
                "explanation": "The registered frame supports the finding.",
            }
        )

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path),
        runner=runner,
        worker_id="review-test",
        lease_ms=1_000,
        max_retries=2,
        retry_delay_ms=0,
    )

    assert worker.run_once() is True
    with EventLedger(db_path) as ledger:
        case = ledger.get_case("case-1")
        remaining = ledger.claim_job(JobKind.REVIEW, "another-worker", lease_ms=1_000)

    assert case is not None
    assert case.revision == 1
    assert case.review is not None
    assert case.review["verdict"] == "violation"
    assert remaining is None
    assert list((tmp_path / "outputs" / "case-1").glob("result-*.json"))


def test_review_worker_retrieves_rules_before_runner_and_accepts_matching_citation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "events.db"
    evidence_id = _record_case(db_path, tmp_path / "frame.jpg")
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(tmp_path / "outputs"))
    calls: list[str] = []

    class Retriever:
        async def retrieve(self, query, *, top_k=5, filters=None):
            calls.append("retriever")
            return RetrievalResult(
                query=query,
                backend="fake",
                chunks=[Chunk(
                    chunk_id="chunk-1", content="必须佩戴安全帽", score=0.9,
                    metadata={"source": "rules.md", "rule_id": "r1", "section": "5.2"},
                )],
            )

    def runner(context, rule_context):
        calls.append("runner")
        assert rule_context is not None and rule_context.available
        return json.dumps({
            "verdict": "violation", "confidence": 0.9,
            "evidence_status": "available", "evidence_ids": [evidence_id],
            "claims": [], "explanation": "规则和证据支持结论",
            "rule_citations": [{
                "chunk_id": "chunk-1", "source": "rules.md",
                "rule_id": "r1", "section": "5.2",
            }],
        })

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path), runner=runner,
        rule_retriever=Retriever(), worker_id="review-test", lease_ms=1_000,
        max_retries=2, retry_delay_ms=0,
    )

    assert worker.run_once() is True
    assert calls == ["retriever", "runner"]


def test_review_worker_refreshes_evidence_before_constructing_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "events.db"
    frame = tmp_path / "late-frame.jpg"
    monkeypatch.setenv("SSV_OUTPUTS_DIR", str(tmp_path / "outputs"))
    with EventLedger(db_path) as ledger:
        ledger.record(
            ReviewContext(
                event_id="late-case",
                source="camera-1",
                timestamp_ms=1000,
                frame_id=4,
                frame_path=str(frame),
            )
        )
        case = ledger.get_case("late-case")
    assert case is not None
    evidence_id = case.evidence[0].evidence_id
    assert case.evidence[0].available is False
    frame.write_bytes(b"frame")

    contexts: list[list[str]] = []

    def runner(context: ReviewContext) -> str:
        contexts.append(context.evidence_ids)
        return json.dumps(
            {
                "verdict": "violation",
                "confidence": 0.91,
                "evidence_status": "available",
                "evidence_ids": [evidence_id],
                "claims": [],
                "explanation": "The delayed registered frame is now available.",
            }
        )

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path),
        runner=runner,
        worker_id="review-test",
        lease_ms=1_000,
        max_retries=2,
        retry_delay_ms=0,
    )

    assert worker.run_once() is True
    assert contexts == [[evidence_id]]


def test_review_worker_retries_model_failure_then_marks_job_dead(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _record_case(db_path, tmp_path / "frame.jpg")
    calls = 0

    def runner(_: ReviewContext) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("model unavailable")

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path),
        runner=runner,
        worker_id="review-test",
        lease_ms=1_000,
        max_retries=2,
        retry_delay_ms=0,
    )

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert worker.run_once() is False
    with EventLedger(db_path) as ledger:
        case = ledger.get_case("case-1")

    assert calls == 2
    assert case is not None
    assert case.review is None


def test_review_worker_retries_invalid_json_and_unregistered_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _record_case(db_path, tmp_path / "frame.jpg")
    results = iter(
        [
            "not JSON",
            json.dumps(
                {
                    "verdict": "violation",
                    "confidence": 0.9,
                    "evidence_status": "available",
                    "evidence_ids": ["not-registered"],
                    "claims": [],
                    "explanation": "The model named an unknown evidence item.",
                }
            ),
        ]
    )

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path),
        runner=lambda _: next(results),
        worker_id="review-test",
        lease_ms=1_000,
        max_retries=3,
        retry_delay_ms=0,
    )

    assert worker.run_once() is True
    assert worker.run_once() is True
    with EventLedger(db_path) as ledger:
        case = ledger.get_case("case-1")

    assert case is not None
    assert case.review is None


class FakeEmbedding:
    def __init__(self) -> None:
        self.texts: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts.append(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeQdrant:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, list[float], dict]] = []

    def __enter__(self) -> "FakeQdrant":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def upsert_event_vector(
        self,
        event_id: str,
        vector: list[float],
        payload: dict,
    ) -> None:
        self.upserts.append((event_id, vector, payload))


def test_index_worker_upserts_current_case_for_an_old_revision_job(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _record_case(db_path, tmp_path / "frame.jpg")
    with EventLedger(db_path) as ledger:
        ledger.append_review(
            "case-1",
            ReviewResult(
                verdict="uncertain",
                confidence=0.4,
                evidence_status="missing",
                explanation="No usable conclusion yet.",
            ),
            "outputs/case-1/result.json",
        )

    embedding = FakeEmbedding()
    qdrant = FakeQdrant()
    worker = IndexWorker(
        ledger_factory=lambda: EventLedger(db_path),
        embedding=embedding,
        qdrant_factory=lambda: qdrant,
        worker_id="index-test",
        lease_ms=1_000,
        max_retries=2,
        retry_delay_ms=0,
    )

    assert worker.run_once() is True

    assert embedding.texts
    assert qdrant.upserts[0][0] == "case-1"
    assert qdrant.upserts[0][2]["revision"] == 1
    assert qdrant.upserts[0][2]["verdict"] == "uncertain"


def test_index_worker_retries_embedding_failure_without_touching_event_facts(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _record_case(db_path, tmp_path / "frame.jpg")

    class FailingEmbedding:
        async def embed_texts(self, _: list[str]) -> list[list[float]]:
            raise RuntimeError("embedding backend unavailable")

    qdrant = FakeQdrant()
    worker = IndexWorker(
        ledger_factory=lambda: EventLedger(db_path),
        embedding=FailingEmbedding(),
        qdrant_factory=lambda: qdrant,
        worker_id="index-test",
        lease_ms=1_000,
        max_retries=1,
        retry_delay_ms=0,
    )

    assert worker.run_once() is True
    assert worker.run_once() is False
    with EventLedger(db_path) as ledger:
        case = ledger.get_case("case-1")

    assert qdrant.upserts == []
    assert case is not None
    assert case.review is None


def test_lease_heartbeat_waits_before_first_renewal_and_is_daemon(
    tmp_path: Path,
    monkeypatch,
) -> None:
    heartbeat = _LeaseHeartbeat(
        ledger_factory=lambda: EventLedger(tmp_path / "events.db"),
        job=DurableJob(
            job_id=7,
            kind=JobKind.REVIEW,
            entity_id="case-1",
            entity_revision=0,
            state=JobState.PROCESSING,
            attempts=1,
            lease_owner="heartbeat-test",
            lease_expires_ms=1_000,
            available_at_ms=0,
            last_error=None,
        ),
        worker_id="heartbeat-test",
        lease_ms=60,
    )
    calls: list[str] = []
    wait_intervals: list[float] = []

    class StopBeforeFirstRenewal:
        def is_set(self) -> bool:
            calls.append("is_set")
            return False

        def wait(self, timeout: float) -> bool:
            calls.append("wait")
            wait_intervals.append(timeout)
            return True

    monkeypatch.setattr(heartbeat, "_stopping", StopBeforeFirstRenewal())
    monkeypatch.setattr(
        heartbeat,
        "_renew_with_new_ledger",
        lambda: calls.append("renew"),
    )

    heartbeat._run()

    assert heartbeat._thread.daemon is True
    assert calls == ["wait"]
    assert wait_intervals == [0.02]


def test_review_worker_heartbeats_a_long_runner_on_an_independent_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "events.db"
    evidence_id = _record_case(db_path, tmp_path / "frame.jpg")
    runner_started = threading.Event()
    release_runner = threading.Event()
    enough_heartbeats = threading.Event()
    factory_ledgers: list[int] = []
    heartbeat_ledgers: set[int] = set()
    heartbeat_threads: set[threading.Thread] = set()
    heartbeat_calls: list[tuple[object, ...]] = []
    renew_count = 0
    run_thread: threading.Thread | None = None

    def ledger_factory() -> EventLedger:
        ledger = EventLedger(db_path)
        factory_ledgers.append(id(ledger))
        return ledger

    original_renew = EventLedger.renew_job

    def observe_renew(
        self: EventLedger,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal renew_count
        original_renew(self, *args, **kwargs)
        if run_thread is not None and threading.current_thread() is not run_thread:
            heartbeat_ledgers.add(id(self))
            heartbeat_threads.add(threading.current_thread())
            heartbeat_calls.append(args)
            renew_count += 1
            if renew_count >= 4:
                enough_heartbeats.set()

    monkeypatch.setattr(EventLedger, "renew_job", observe_renew)

    def runner(_: ReviewContext) -> str:
        runner_started.set()
        release_runner.wait(timeout=2)
        return json.dumps(
            {
                "verdict": "violation",
                "confidence": 0.91,
                "evidence_status": "available",
                "evidence_ids": [evidence_id],
                "claims": [],
                "explanation": "The lease remained healthy during review.",
            }
        )

    worker = ReviewWorker(
        ledger_factory=ledger_factory,
        runner=runner,
        worker_id="review-heartbeat",
        lease_ms=60,
        max_retries=2,
        retry_delay_ms=0,
        result_writer=lambda _event_id, _result: tmp_path / "result.json",
    )
    outcomes: list[bool] = []
    run_thread = threading.Thread(target=lambda: outcomes.append(worker.run_once()))
    run_thread.start()
    try:
        assert runner_started.wait(timeout=1)
        assert enough_heartbeats.wait(timeout=1)
        with EventLedger(db_path) as rival:
            assert rival.claim_job(JobKind.REVIEW, "review-rival", lease_ms=1_000) is None
    finally:
        release_runner.set()
        run_thread.join(timeout=1)

    assert outcomes == [True]
    assert not run_thread.is_alive()
    assert factory_ledgers
    assert heartbeat_ledgers
    assert factory_ledgers[0] not in heartbeat_ledgers
    assert all(not thread.is_alive() for thread in heartbeat_threads)
    assert all(
        args[1:] == ("review-heartbeat", 1, 60) for args in heartbeat_calls
    )


def test_index_worker_heartbeats_a_long_embedding_and_releases_the_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "events.db"
    _record_case(db_path, tmp_path / "frame.jpg")
    embedding_started = threading.Event()
    release_embedding = threading.Event()
    enough_heartbeats = threading.Event()
    factory_ledgers: list[int] = []
    heartbeat_ledgers: set[int] = set()
    heartbeat_threads: set[threading.Thread] = set()
    renew_count = 0
    run_thread: threading.Thread | None = None

    def ledger_factory() -> EventLedger:
        ledger = EventLedger(db_path)
        factory_ledgers.append(id(ledger))
        return ledger

    original_renew = EventLedger.renew_job

    def observe_renew(
        self: EventLedger,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal renew_count
        original_renew(self, *args, **kwargs)
        if run_thread is not None and threading.current_thread() is not run_thread:
            heartbeat_ledgers.add(id(self))
            heartbeat_threads.add(threading.current_thread())
            renew_count += 1
            if renew_count >= 4:
                enough_heartbeats.set()

    monkeypatch.setattr(EventLedger, "renew_job", observe_renew)

    class BlockingEmbedding:
        async def embed_texts(self, _: list[str]) -> list[list[float]]:
            embedding_started.set()
            release_embedding.wait(timeout=2)
            return [[0.1, 0.2, 0.3]]

    qdrant = FakeQdrant()
    worker = IndexWorker(
        ledger_factory=ledger_factory,
        embedding=BlockingEmbedding(),
        qdrant_factory=lambda: qdrant,
        worker_id="index-heartbeat",
        lease_ms=60,
        max_retries=2,
        retry_delay_ms=0,
    )
    outcomes: list[bool] = []
    run_thread = threading.Thread(target=lambda: outcomes.append(worker.run_once()))
    run_thread.start()
    try:
        assert embedding_started.wait(timeout=1)
        assert enough_heartbeats.wait(timeout=1)
        with EventLedger(db_path) as rival:
            assert rival.claim_job(JobKind.INDEX, "index-rival", lease_ms=1_000) is None
    finally:
        release_embedding.set()
        run_thread.join(timeout=1)

    assert outcomes == [True]
    assert not run_thread.is_alive()
    assert qdrant.upserts
    assert factory_ledgers
    assert heartbeat_ledgers
    assert factory_ledgers[0] not in heartbeat_ledgers
    assert all(not thread.is_alive() for thread in heartbeat_threads)


def test_review_worker_exposes_heartbeat_failures_without_writing_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "events.db"
    evidence_id = _record_case(db_path, tmp_path / "frame.jpg")
    heartbeat_failed = threading.Event()
    heartbeat_threads: set[threading.Thread] = set()
    written: list[Path] = []
    original_renew = EventLedger.renew_job

    def fail_heartbeat_renew(
        self: EventLedger,
        *args: object,
        **kwargs: object,
    ) -> None:
        if threading.current_thread() is not threading.main_thread():
            heartbeat_threads.add(threading.current_thread())
            heartbeat_failed.set()
            raise OSError("heartbeat storage unavailable")
        original_renew(self, *args, **kwargs)

    monkeypatch.setattr(EventLedger, "renew_job", fail_heartbeat_renew)

    def runner(_: ReviewContext) -> str:
        assert heartbeat_failed.wait(timeout=1)
        return json.dumps(
            {
                "verdict": "violation",
                "confidence": 0.91,
                "evidence_status": "available",
                "evidence_ids": [evidence_id],
                "claims": [],
                "explanation": "The heartbeat failure must stop artifact writing.",
            }
        )

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path),
        runner=runner,
        worker_id="review-heartbeat-error",
        lease_ms=60,
        max_retries=2,
        retry_delay_ms=0,
        result_writer=lambda _event_id, _result: written.append(tmp_path / "result.json")
        or tmp_path / "result.json",
    )

    assert worker.run_once() is True
    with EventLedger(db_path) as ledger:
        row = ledger._store._conn.execute(
            "SELECT state, last_error FROM durable_jobs "
            "WHERE kind = 'review' AND entity_id = 'case-1'"
        ).fetchone()

    assert heartbeat_failed.is_set()
    assert heartbeat_threads
    assert all(not thread.is_alive() for thread in heartbeat_threads)
    assert written == []
    assert row is not None
    assert row["state"] == "pending"
    assert "lease heartbeat failed" in row["last_error"]


def test_review_worker_marks_an_over_limit_claim_dead_before_external_calls(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "events.db"
    _record_case(db_path, tmp_path / "frame.jpg")
    _requeue_claim_for_a_stricter_retry_limit(db_path, JobKind.REVIEW)
    runner_calls: list[ReviewContext] = []
    writer_calls: list[Path] = []

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path),
        runner=lambda context: runner_calls.append(context) or "{}",
        worker_id="review-limit",
        lease_ms=1_000,
        max_retries=1,
        retry_delay_ms=0,
        result_writer=lambda _event_id, _result: writer_calls.append(tmp_path / "result.json")
        or tmp_path / "result.json",
    )

    assert worker.run_once() is True
    with EventLedger(db_path) as ledger:
        row = ledger._store._conn.execute(
            "SELECT state, attempts, last_error FROM durable_jobs "
            "WHERE kind = 'review' AND entity_id = 'case-1'"
        ).fetchone()

    assert runner_calls == []
    assert writer_calls == []
    assert row is not None
    assert row["state"] == "dead"
    assert row["attempts"] == 2
    assert "exceeds max_retries 1" in row["last_error"]


def test_index_worker_marks_an_over_limit_claim_dead_before_external_calls(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "events.db"
    _record_case(db_path, tmp_path / "frame.jpg")
    _requeue_claim_for_a_stricter_retry_limit(db_path, JobKind.INDEX)

    class RecordingEmbedding:
        def __init__(self) -> None:
            self.calls = 0

        async def embed_texts(self, _: list[str]) -> list[list[float]]:
            self.calls += 1
            return [[0.1, 0.2, 0.3]]

    embedding = RecordingEmbedding()
    qdrant = FakeQdrant()
    worker = IndexWorker(
        ledger_factory=lambda: EventLedger(db_path),
        embedding=embedding,
        qdrant_factory=lambda: qdrant,
        worker_id="index-limit",
        lease_ms=1_000,
        max_retries=1,
        retry_delay_ms=0,
    )

    assert worker.run_once() is True
    with EventLedger(db_path) as ledger:
        row = ledger._store._conn.execute(
            "SELECT state, attempts, last_error FROM durable_jobs "
            "WHERE kind = 'index' AND entity_id = 'case-1'"
        ).fetchone()

    assert embedding.calls == 0
    assert qdrant.upserts == []
    assert row is not None
    assert row["state"] == "dead"
    assert row["attempts"] == 2
    assert "exceeds max_retries 1" in row["last_error"]


def test_review_worker_leaves_reclaimed_job_untouched_after_lease_loss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "events.db"
    clock = [1_000]
    monkeypatch.setattr(ledger_module, "_now_ms", lambda: clock[0])
    evidence_id = _record_case(db_path, tmp_path / "frame.jpg")
    written: list[Path] = []

    def runner(_: ReviewContext) -> str:
        clock[0] += 2
        with EventLedger(db_path) as rival:
            reclaimed = rival.claim_job(JobKind.REVIEW, "review-b", lease_ms=1_000)
        assert reclaimed is not None
        return json.dumps(
            {
                "verdict": "uncertain",
                "confidence": 0.4,
                "evidence_status": "missing",
                "evidence_ids": [evidence_id],
                "claims": [],
                "explanation": "The original lease expired.",
            }
        )

    worker = ReviewWorker(
        ledger_factory=lambda: EventLedger(db_path),
        runner=runner,
        worker_id="review-a",
        lease_ms=1,
        max_retries=3,
        retry_delay_ms=0,
        result_writer=lambda _event_id, _result: written.append(tmp_path / "result.json")
        or tmp_path / "result.json",
    )

    assert worker.run_once() is True
    with EventLedger(db_path) as ledger:
        row = ledger._store._conn.execute(
            "SELECT state, lease_owner, attempts FROM durable_jobs "
            "WHERE kind = 'review' AND entity_id = 'case-1'"
        ).fetchone()
        case = ledger.get_case("case-1")

    assert row is not None
    assert row["state"] == "processing"
    assert row["lease_owner"] == "review-b"
    assert row["attempts"] == 2
    assert case is not None
    assert case.revision == 0
    assert case.review is None
    assert written == []


def test_index_worker_leaves_reclaimed_job_untouched_after_lease_loss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "events.db"
    clock = [1_000]
    monkeypatch.setattr(ledger_module, "_now_ms", lambda: clock[0])
    _record_case(db_path, tmp_path / "frame.jpg")

    class ReclaimingEmbedding:
        async def embed_texts(self, _: list[str]) -> list[list[float]]:
            clock[0] += 2
            with EventLedger(db_path) as rival:
                reclaimed = rival.claim_job(JobKind.INDEX, "index-b", lease_ms=1_000)
            assert reclaimed is not None
            return [[0.1, 0.2, 0.3]]

    qdrant = FakeQdrant()
    worker = IndexWorker(
        ledger_factory=lambda: EventLedger(db_path),
        embedding=ReclaimingEmbedding(),
        qdrant_factory=lambda: qdrant,
        worker_id="index-a",
        lease_ms=1,
        max_retries=3,
        retry_delay_ms=0,
    )

    assert worker.run_once() is True
    with EventLedger(db_path) as ledger:
        row = ledger._store._conn.execute(
            "SELECT state, lease_owner, attempts FROM durable_jobs "
            "WHERE kind = 'index' AND entity_id = 'case-1'"
        ).fetchone()

    assert qdrant.upserts == []
    assert row is not None
    assert row["state"] == "processing"
    assert row["lease_owner"] == "index-b"
    assert row["attempts"] == 2


def test_review_worker_run_recovers_after_transient_ledger_factory_failure(monkeypatch) -> None:
    stopping = threading.Event()
    factory_calls = 0
    warnings: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

    class IdleLedger:
        def __enter__(self) -> "IdleLedger":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def claim_job(self, kind: JobKind, worker_id: str, lease_ms: int) -> None:
            assert kind is JobKind.REVIEW
            assert worker_id == "review-loop"
            assert lease_ms == 1_000
            stopping.set()
            return None

    def ledger_factory() -> IdleLedger:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise OSError("ledger factory unavailable")
        return IdleLedger()

    monkeypatch.setattr(workers_module, "logger", FakeLogger())
    worker = ReviewWorker(
        ledger_factory=ledger_factory,
        runner=lambda _: "{}",
        worker_id="review-loop",
        lease_ms=1_000,
        max_retries=1,
        retry_delay_ms=0,
        poll_interval_seconds=0.001,
    )

    worker.run(stopping)

    assert factory_calls == 2
    assert warnings == [
        (
            "durable worker run_once failed",
            {
                "worker_id": "review-loop",
                "job_kind": "review",
                "error": "ledger factory unavailable",
            },
        )
    ]


def test_index_worker_run_recovers_after_transient_ledger_context_failure(monkeypatch) -> None:
    stopping = threading.Event()
    factory_calls = 0
    warnings: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

    class FailingLedger:
        def __enter__(self) -> "FailingLedger":
            raise OSError("ledger context unavailable")

        def __exit__(self, *exc: object) -> None:
            return None

    class IdleLedger:
        def __enter__(self) -> "IdleLedger":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def claim_job(self, kind: JobKind, worker_id: str, lease_ms: int) -> None:
            assert kind is JobKind.INDEX
            assert worker_id == "index-loop"
            assert lease_ms == 1_000
            stopping.set()
            return None

    def ledger_factory() -> FailingLedger | IdleLedger:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            return FailingLedger()
        return IdleLedger()

    monkeypatch.setattr(workers_module, "logger", FakeLogger())
    worker = IndexWorker(
        ledger_factory=ledger_factory,
        embedding=object(),
        qdrant_factory=lambda: object(),
        worker_id="index-loop",
        lease_ms=1_000,
        max_retries=1,
        retry_delay_ms=0,
        poll_interval_seconds=0.001,
    )

    worker.run(stopping)

    assert factory_calls == 2
    assert warnings == [
        (
            "durable worker run_once failed",
            {
                "worker_id": "index-loop",
                "job_kind": "index",
                "error": "ledger context unavailable",
            },
        )
    ]
