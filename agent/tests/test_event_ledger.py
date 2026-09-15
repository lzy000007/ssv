from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest

import ssv_agent.event_store.ledger as ledger_module
from ssv_agent.event_store import EventLedger, JobKind, LeaseLostError
from ssv_agent.config import RecordingEvidenceConfig
from ssv_agent.recording_evidence import RecordingEvidenceArtifact
from ssv_agent.result import ReviewResult
from ssv_agent.review_context import ReviewContext


def _context() -> ReviewContext:
    return ReviewContext.model_validate(
        {
            "event_id": "evt-1",
            "source": "camera-1",
            "timestamp_ms": 1_700_000_000_000,
            "frame_id": 42,
            "event_type": "person_without_helmet",
            "severity": "high",
            "detections": [
                {
                    "class": "person",
                    "class_id": 0,
                    "confidence": 0.92,
                    "bbox": [1, 2, 3, 4],
                    "track_id": 7,
                }
            ],
        }
    )


def _enabled_config() -> RecordingEvidenceConfig:
    return RecordingEvidenceConfig(enabled=True, clip_after_ms=2500)


def _artifacts(root: Path) -> tuple[RecordingEvidenceArtifact, ...]:
    derived = root / "derived" / "evt-1"
    derived.mkdir(parents=True)
    values = [("clip", "context.mp4", "video/mp4"), ("frame", "frame-01.jpg", "image/jpeg"),
              ("frame", "frame-02.jpg", "image/jpeg"), ("frame", "frame-03.jpg", "image/jpeg")]
    result = []
    for kind, name, mime in values:
        path = derived / name
        path.write_bytes(name.encode())
        result.append(RecordingEvidenceArtifact(kind=kind, path=path,
            sha256=__import__("hashlib").sha256(path.read_bytes()).hexdigest(),
            mime_type=mime, size=path.stat().st_size, mtime=path.stat().st_mtime))
    return tuple(result)


def test_enabled_recording_evidence_creates_only_delayed_extract_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger_module, "_now_ms", lambda: 1_000)
    with EventLedger(tmp_path / "events.db", recording_evidence=_enabled_config()) as ledger:
        outcome = ledger.record(_context())
    assert {job.kind for job in outcome.jobs} == {JobKind.EVIDENCE_EXTRACT}
    assert outcome.jobs[0].available_at_ms == _context().timestamp_ms + 2500


def test_schema_migration_preserves_legacy_review_and_index_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE events (event_id TEXT PRIMARY KEY, source TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL, frame_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', created_ms INTEGER NOT NULL);
            CREATE TABLE durable_jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('review', 'index')),
                entity_id TEXT NOT NULL REFERENCES events(event_id), entity_revision INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'processing', 'completed', 'dead')),
                attempts INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires_ms INTEGER,
                available_at_ms INTEGER NOT NULL, last_error TEXT, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                UNIQUE (kind, entity_id, entity_revision));
            INSERT INTO events VALUES ('legacy', 'camera', 1, 1, 'pending', 1);
            INSERT INTO durable_jobs(kind, entity_id, entity_revision, state, available_at_ms, created_ms, updated_ms)
                VALUES ('review', 'legacy', 0, 'pending', 0, 1, 1), ('index', 'legacy', 0, 'pending', 0, 1, 1);
        """)
    with EventLedger(db_path) as ledger:
        assert ledger.claim_job(JobKind.REVIEW, "r", 1000) is not None
        assert ledger.claim_job(JobKind.INDEX, "i", 1000) is not None


def test_complete_evidence_extract_registers_four_refs_and_one_review(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    with EventLedger(tmp_path / "events.db", evidence_roots=[str(tmp_path)], recording_evidence=_enabled_config()) as ledger:
        ledger.record(_context())
        job = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, "extractor", 1000)
        assert job is not None
        ledger.complete_evidence_extract_job(job, "extractor", artifacts)
        case = ledger.get_case("evt-1")
        review = ledger.claim_job(JobKind.REVIEW, "reviewer", 1000)
    assert case is not None
    assert len(case.evidence) == 4
    assert review is not None


def test_complete_evidence_extract_is_idempotent(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    with EventLedger(tmp_path / "events.db", evidence_roots=[str(tmp_path)], recording_evidence=_enabled_config()) as ledger:
        ledger.record(_context())
        job = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, "extractor", 1000)
        assert job is not None
        ledger.complete_evidence_extract_job(job, "extractor", artifacts)
        ledger.complete_evidence_extract_job(job, "extractor", artifacts)
        rows = ledger._store._conn.execute("SELECT kind FROM durable_jobs WHERE entity_id = 'evt-1'").fetchall()
    assert [row["kind"] for row in rows].count("review") == 1


def test_failed_evidence_extract_dead_marks_manual_review_without_review_job(tmp_path: Path) -> None:
    with EventLedger(tmp_path / "events.db", recording_evidence=_enabled_config()) as ledger:
        ledger.record(_context())
        job = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, "extractor", 1000)
        assert job is not None
        retry_state = ledger.fail_evidence_extract_job(
            job, "extractor", "retryable failure", max_retries=2, retry_delay_ms=0
        )
        retry = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, "extractor", 1000)
        assert retry is not None
        state = ledger.fail_evidence_extract_job(
            retry, "extractor", "final failure", max_retries=2, retry_delay_ms=0
        )
        case = ledger.get_case("evt-1")
        review = ledger.claim_job(JobKind.REVIEW, "reviewer", 1000)
    assert retry_state == ledger_module.JobState.PENDING
    assert state == ledger_module.JobState.DEAD
    assert case is not None and case.status == "manual_review"
    assert review is None


def test_complete_evidence_extract_rejects_path_outside_root(tmp_path: Path) -> None:
    artifacts = list(_artifacts(tmp_path))
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"outside")
    artifacts[0] = RecordingEvidenceArtifact(kind="clip", path=outside,
        sha256="x", mime_type="video/mp4", size=7, mtime=outside.stat().st_mtime)
    with EventLedger(tmp_path / "events.db", evidence_roots=[str(tmp_path)], recording_evidence=_enabled_config()) as ledger:
        ledger.record(_context())
        job = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, "extractor", 1000)
        assert job is not None
        with pytest.raises(ValueError, match="evidence"):
            ledger.complete_evidence_extract_job(job, "extractor", tuple(artifacts))


def test_complete_evidence_extract_rejects_artifact_with_wrong_mime_type(tmp_path: Path) -> None:
    artifacts = list(_artifacts(tmp_path))
    clip = artifacts[0]
    artifacts[0] = RecordingEvidenceArtifact(
        kind=clip.kind,
        path=clip.path,
        sha256=clip.sha256,
        mime_type="image/jpeg",
        size=clip.size,
        mtime=clip.mtime,
    )
    with EventLedger(
        tmp_path / "events.db",
        evidence_roots=[str(tmp_path)],
        recording_evidence=_enabled_config(),
    ) as ledger:
        ledger.record(_context())
        job = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, "extractor", 1000)
        assert job is not None
        with pytest.raises(ValueError, match="evidence"):
            ledger.complete_evidence_extract_job(job, "extractor", tuple(artifacts))


def test_record_creates_one_authoritative_case_and_initial_jobs(tmp_path: Path) -> None:
    with EventLedger(tmp_path / "events.db") as ledger:
        first = ledger.record(_context())
        second = ledger.record(_context())
        case = ledger.get_case("evt-1")

    assert first.created is True
    assert second.created is False
    assert case is not None
    assert case.event_id == "evt-1"
    assert case.source == "camera-1"
    assert len(case.detections) == 1
    assert {job.kind for job in first.jobs} == {JobKind.REVIEW, JobKind.INDEX}
    assert {job.kind for job in second.jobs} == {JobKind.REVIEW, JobKind.INDEX}


def test_record_fails_closed_when_no_evidence_root_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSV_EVIDENCE_ROOTS", raising=False)
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    context = _context().model_copy(update={"frame_path": str(frame)})

    with EventLedger(tmp_path / "events.db") as ledger:
        outcome = ledger.record(context)
        case = ledger.get_case("evt-1")

    assert outcome.created is True
    assert case is not None
    assert case.evidence == ()
    assert {job.kind for job in outcome.jobs} == {JobKind.REVIEW, JobKind.INDEX}


def test_record_stores_canonical_path_for_a_file_inside_a_configured_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    frame = root / "nested" / "frame.jpg"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"frame")
    context = _context().model_copy(update={"frame_path": str(frame)})

    with EventLedger(tmp_path / "events.db", evidence_roots=[str(root)]) as ledger:
        ledger.record(context)
        case = ledger.get_case("evt-1")

    assert case is not None
    assert case.evidence[0].path == str(frame.resolve(strict=False))
    assert case.evidence[0].available is True


def test_record_rejects_a_parent_traversal_outside_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    path_with_traversal = root / ".." / outside.name
    context = _context().model_copy(update={"frame_path": str(path_with_traversal)})

    with EventLedger(tmp_path / "events.db", evidence_roots=[str(root)]) as ledger:
        ledger.record(context)
        case = ledger.get_case("evt-1")

    assert case is not None
    assert case.evidence == ()


def test_record_rejects_a_symlink_inside_root_that_targets_outside(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    link = root / "frame.jpg"
    link.symlink_to(outside)
    context = _context().model_copy(update={"frame_path": str(link)})

    with EventLedger(tmp_path / "events.db", evidence_roots=[str(root)]) as ledger:
        ledger.record(context)
        case = ledger.get_case("evt-1")

    assert case is not None
    assert case.evidence == ()


def test_event_ledger_reads_json_evidence_roots_and_fails_closed_on_malformed_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "evidence"
    monkeypatch.setenv("SSV_EVIDENCE_ROOTS", json.dumps([str(root)]))
    with EventLedger(tmp_path / "events.db") as ledger:
        assert ledger.evidence_roots == (root.resolve(strict=False),)

    monkeypatch.setenv("SSV_EVIDENCE_ROOTS", "{not-json")
    with pytest.raises(ValueError, match="SSV_EVIDENCE_ROOTS"):
        EventLedger(tmp_path / "malformed.db")


def test_explicit_evidence_roots_take_precedence_over_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_root = tmp_path / "env-root"
    explicit_root = tmp_path / "explicit-root"
    monkeypatch.setenv("SSV_EVIDENCE_ROOTS", json.dumps([str(env_root)]))

    with EventLedger(
        tmp_path / "events.db",
        evidence_roots=[str(explicit_root)],
    ) as ledger:
        assert ledger.evidence_roots == (explicit_root.resolve(strict=False),)


def test_claimed_job_is_exclusive_then_retries_to_dead(tmp_path: Path) -> None:
    with EventLedger(tmp_path / "events.db") as ledger:
        ledger.record(_context())
        first = ledger.claim_job(JobKind.REVIEW, "worker-a", lease_ms=10_000)
        blocked = ledger.claim_job(JobKind.REVIEW, "worker-b", lease_ms=10_000)
        assert first is not None
        state = ledger.fail_job(
            first.job_id,
            "worker-a",
            first.attempts,
            "model unavailable",
            max_retries=2,
            retry_delay_ms=0,
        )
        retry = ledger.claim_job(JobKind.REVIEW, "worker-b", lease_ms=10_000)
        assert retry is not None
        dead = ledger.fail_job(
            retry.job_id,
            "worker-b",
            retry.attempts,
            "model unavailable",
            max_retries=2,
            retry_delay_ms=0,
        )

    assert blocked is None
    assert state.value == "pending"
    assert retry.attempts == 2
    assert dead.value == "dead"


def test_append_review_advances_revision_and_enqueues_current_index(tmp_path: Path) -> None:
    result = ReviewResult(
        verdict="uncertain",
        confidence=0.4,
        evidence_status="missing",
        explanation="camera view is unavailable",
    )
    with EventLedger(tmp_path / "events.db") as ledger:
        ledger.record(_context())
        revision = ledger.append_review("evt-1", result, "outputs/evt-1.json")
        case = ledger.get_case("evt-1")
        original_index = ledger.claim_job(JobKind.INDEX, "indexer", lease_ms=1_000)
        assert original_index is not None
        ledger.complete_job(original_index.job_id, "indexer", original_index.attempts)
        review_index = ledger.claim_job(JobKind.INDEX, "indexer", lease_ms=1_000)

    assert revision == 1
    assert case is not None
    assert case.revision == 1
    assert case.review is not None
    assert case.review["verdict"] == "uncertain"
    assert review_index is not None
    assert review_index.entity_revision == 1


def test_expired_lease_can_be_reclaimed_after_process_restart(tmp_path: Path) -> None:
    with EventLedger(tmp_path / "events.db") as ledger:
        ledger.record(_context())
        abandoned = ledger.claim_job(JobKind.REVIEW, "stopped-worker", lease_ms=0)
        reclaimed = ledger.claim_job(JobKind.REVIEW, "new-worker", lease_ms=1_000)

    assert abandoned is not None
    assert reclaimed is not None
    assert reclaimed.job_id == abandoned.job_id
    assert reclaimed.lease_owner == "new-worker"


def test_record_upgrades_a_preexisting_events_database(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                frame_id INTEGER NOT NULL,
                event_type TEXT,
                severity TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                verdict TEXT,
                confidence REAL,
                result_path TEXT,
                created_ms INTEGER NOT NULL
            );
            """
        )

    with EventLedger(db_path) as ledger:
        outcome = ledger.record(_context())
        case = ledger.get_case("evt-1")

    assert outcome.created is True
    assert case is not None
    assert case.revision == 0


def test_enqueue_all_index_jobs_requeues_completed_current_projection(tmp_path: Path) -> None:
    with EventLedger(tmp_path / "events.db") as ledger:
        ledger.record(_context())
        completed = ledger.claim_job(JobKind.INDEX, "indexer", lease_ms=1_000)
        assert completed is not None
        ledger.complete_job(completed.job_id, "indexer", completed.attempts)

        requeued = ledger.enqueue_all_index_jobs()
        rebuilt = ledger.claim_job(JobKind.INDEX, "indexer", lease_ms=1_000)

    assert requeued == 1
    assert rebuilt is not None
    assert rebuilt.entity_id == "evt-1"
    assert rebuilt.attempts == completed.attempts + 1


def test_expired_worker_cannot_mutate_reclaimed_review_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "events.db"
    clock = [1_000]
    monkeypatch.setattr(ledger_module, "_now_ms", lambda: clock[0])
    result = ReviewResult(
        verdict="uncertain",
        confidence=0.4,
        evidence_status="missing",
        explanation="The evidence is unavailable.",
    )

    with EventLedger(db_path) as worker_a, EventLedger(db_path) as worker_b:
        worker_a.record(_context())
        claimed_by_a = worker_a.claim_job(JobKind.REVIEW, "worker-a", lease_ms=10)
        assert claimed_by_a is not None

        clock[0] += 11
        claimed_by_b = worker_b.claim_job(JobKind.REVIEW, "worker-b", lease_ms=1_000)
        assert claimed_by_b is not None
        assert claimed_by_b.job_id == claimed_by_a.job_id

        with pytest.raises(LeaseLostError):
            worker_a.complete_job(
                claimed_by_a.job_id,
                "worker-a",
                claimed_by_a.attempts,
            )
        with pytest.raises(LeaseLostError):
            worker_a.fail_job(
                claimed_by_a.job_id,
                "worker-a",
                claimed_by_a.attempts,
                "late failure",
                max_retries=3,
                retry_delay_ms=0,
            )
        with pytest.raises(LeaseLostError):
            worker_a.complete_review_job(
                claimed_by_a,
                "worker-a",
                result,
                "outputs/evt-1.json",
            )

        job = worker_b._store._conn.execute(
            "SELECT state, lease_owner, attempts FROM durable_jobs WHERE job_id = ?",
            (claimed_by_b.job_id,),
        ).fetchone()
        case = worker_b.get_case("evt-1")

    assert job is not None
    assert job["state"] == "processing"
    assert job["lease_owner"] == "worker-b"
    assert job["attempts"] == 2
    assert case is not None
    assert case.revision == 0
    assert case.review is None


def test_same_worker_id_cannot_mutate_a_reclaimed_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "events.db"
    clock = [1_000]
    monkeypatch.setattr(ledger_module, "_now_ms", lambda: clock[0])
    result = ReviewResult(
        verdict="uncertain",
        confidence=0.4,
        evidence_status="missing",
        explanation="The evidence is unavailable.",
    )

    with EventLedger(db_path) as worker_a, EventLedger(db_path) as worker_b:
        worker_a.record(_context())
        claimed_by_a = worker_a.claim_job(JobKind.REVIEW, "shared-worker", lease_ms=10)
        assert claimed_by_a is not None

        clock[0] += 11
        claimed_by_b = worker_b.claim_job(JobKind.REVIEW, "shared-worker", lease_ms=1_000)
        assert claimed_by_b is not None
        assert claimed_by_b.attempts == claimed_by_a.attempts + 1

        with pytest.raises(LeaseLostError):
            worker_a.complete_job(
                claimed_by_a.job_id,
                "shared-worker",
                claimed_by_a.attempts,
            )
        with pytest.raises(LeaseLostError):
            worker_a.fail_job(
                claimed_by_a.job_id,
                "shared-worker",
                claimed_by_a.attempts,
                "late failure",
                max_retries=3,
                retry_delay_ms=0,
            )
        with pytest.raises(LeaseLostError):
            worker_a.complete_review_job(
                claimed_by_a,
                "shared-worker",
                result,
                "outputs/evt-1.json",
            )

        job = worker_b._store._conn.execute(
            "SELECT state, lease_owner, attempts FROM durable_jobs WHERE job_id = ?",
            (claimed_by_b.job_id,),
        ).fetchone()
        case = worker_b.get_case("evt-1")

    assert job is not None
    assert job["state"] == "processing"
    assert job["lease_owner"] == "shared-worker"
    assert job["attempts"] == claimed_by_b.attempts
    assert case is not None
    assert case.revision == 0
    assert case.review is None


def test_renew_job_extends_only_the_current_unexpired_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "events.db"
    clock = [1_000]
    monkeypatch.setattr(ledger_module, "_now_ms", lambda: clock[0])

    with EventLedger(db_path) as worker_a, EventLedger(db_path) as worker_b:
        worker_a.record(_context())
        claimed = worker_a.claim_job(JobKind.REVIEW, "shared-worker", lease_ms=10)
        assert claimed is not None

        clock[0] = 1_005
        worker_a.renew_job(
            claimed.job_id,
            "shared-worker",
            claimed.attempts,
            lease_ms=10,
        )

        clock[0] = 1_011
        assert worker_b.claim_job(JobKind.REVIEW, "worker-b", lease_ms=1_000) is None

        clock[0] = 1_016
        reclaimed = worker_b.claim_job(JobKind.REVIEW, "shared-worker", lease_ms=1_000)
        assert reclaimed is not None
        assert reclaimed.attempts == claimed.attempts + 1

        with pytest.raises(LeaseLostError):
            worker_a.renew_job(
                claimed.job_id,
                "shared-worker",
                claimed.attempts,
                lease_ms=10,
            )


def test_refresh_evidence_tracks_a_registered_file_appearing_and_disappearing(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "late-frame.jpg"
    context = _context().model_copy(update={"frame_path": str(frame)})

    with EventLedger(tmp_path / "events.db", evidence_roots=[str(tmp_path)]) as ledger:
        ledger.record(context)
        missing = ledger.get_case("evt-1")
        assert missing is not None
        evidence_id = missing.evidence[0].evidence_id
        assert missing.evidence[0].available is False

        frame.write_bytes(b"frame")
        assert ledger.refresh_evidence("evt-1", evidence_id) == 1
        available = ledger.get_case("evt-1")

        frame.unlink()
        assert ledger.refresh_evidence("evt-1") == 1
        unavailable = ledger.get_case("evt-1")
        assert ledger.refresh_evidence("unknown-event") == 0
        assert ledger.refresh_evidence("evt-1", "unknown-evidence") == 0

    assert available is not None
    assert available.evidence[0].available is True
    assert available.evidence[0].size == len(b"frame")
    assert unavailable is not None
    assert unavailable.evidence[0].available is False
    assert unavailable.evidence[0].size is None
    assert unavailable.evidence[0].mtime is None


def test_refresh_evidence_rejects_a_registered_path_replaced_by_external_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    frame = root / "frame.jpg"
    frame.write_bytes(b"inside")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    context = _context().model_copy(update={"frame_path": str(frame)})

    with EventLedger(tmp_path / "events.db", evidence_roots=[str(root)]) as ledger:
        ledger.record(context)
        registered = ledger.get_case("evt-1")
        assert registered is not None
        evidence_id = registered.evidence[0].evidence_id

        frame.unlink()
        frame.symlink_to(outside)
        assert ledger.refresh_evidence("evt-1", evidence_id) == 1
        refreshed = ledger.get_case("evt-1")

        assert refreshed is not None
        assert refreshed.evidence[0].available is False
        assert ledger.resolve_evidence_path(refreshed.evidence[0].path) is None


def test_complete_review_job_refreshes_evidence_before_accepting_a_reference(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "events.db"
    frame = tmp_path / "late-frame.jpg"
    context = _context().model_copy(update={"frame_path": str(frame)})

    with EventLedger(db_path, evidence_roots=[str(tmp_path)]) as ledger:
        ledger.record(context)
        case = ledger.get_case("evt-1")
        assert case is not None
        evidence_id = case.evidence[0].evidence_id
        result = ReviewResult(
            verdict="violation",
            confidence=0.9,
            evidence_status="available",
            evidence_ids=[evidence_id],
            explanation="The registered frame supports the finding.",
        )
        frame.write_bytes(b"frame")
        job = ledger.claim_job(JobKind.REVIEW, "reviewer", lease_ms=1_000)
        assert job is not None

        revision = ledger.complete_review_job(
            job,
            "reviewer",
            result,
            "outputs/result.json",
        )
        accepted = ledger.get_case("evt-1")

    assert revision == 1
    assert accepted is not None
    assert accepted.review is not None
    assert accepted.evidence[0].available is True


def test_complete_review_job_rejects_a_reference_when_the_registered_file_disappears(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "events.db"
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    context = _context().model_copy(update={"frame_path": str(frame)})

    with EventLedger(db_path, evidence_roots=[str(tmp_path)]) as ledger:
        ledger.record(context)
        case = ledger.get_case("evt-1")
        assert case is not None
        evidence_id = case.evidence[0].evidence_id
        job = ledger.claim_job(JobKind.REVIEW, "reviewer", lease_ms=1_000)
        assert job is not None
        frame.unlink()
        result = ReviewResult(
            verdict="violation",
            confidence=0.9,
            evidence_status="available",
            evidence_ids=[evidence_id],
            explanation="The no-longer-present frame was referenced.",
        )

        with pytest.raises(ValueError, match="unavailable evidence"):
            ledger.complete_review_job(
                job,
                "reviewer",
                result,
                "outputs/result.json",
            )
        unchanged = ledger.get_case("evt-1")

    assert unchanged is not None
    assert unchanged.revision == 0
    assert unchanged.review is None
