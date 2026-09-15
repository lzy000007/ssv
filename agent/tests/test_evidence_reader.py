from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from ssv_agent.event_store import EventLedger, JobKind
from ssv_agent.config import RecordingEvidenceConfig
from ssv_agent.recording_evidence import RecordingEvidenceArtifact
from ssv_agent.review_context import ReviewContext
from ssv_agent.tools.evidence_reader import evidence_reader_tool


def _fake_thread_data(monkeypatch, tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "ssv_agent.tools.evidence_reader.get_thread_data",
        lambda _runtime: {"outputs_path": str(outputs)},
    )


def _registered_evidence(tmp_path: Path, monkeypatch) -> tuple[str, Path]:
    db_path = tmp_path / "events.db"
    monkeypatch.setenv("SSV_EVENT_DB_PATH", str(db_path))
    monkeypatch.setenv("SSV_EVIDENCE_ROOTS", json.dumps([str(tmp_path)]))
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")
    with EventLedger(db_path, evidence_roots=[str(tmp_path)]) as ledger:
        ledger.record(
            ReviewContext(
                event_id="case-1",
                source="camera-1",
                timestamp_ms=1000,
                frame_id=1,
                frame_path=str(frame),
            )
        )
        case = ledger.get_case("case-1")
    assert case is not None
    return case.evidence[0].evidence_id, frame


def _register_derived_context_evidence(tmp_path: Path, monkeypatch) -> list[str]:
    """登记一个 clip 与三张派生帧，返回四个稳定 evidence_id。"""
    db_path = tmp_path / "events.db"
    monkeypatch.setenv("SSV_EVENT_DB_PATH", str(db_path))
    monkeypatch.setenv("SSV_EVIDENCE_ROOTS", json.dumps([str(tmp_path)]))
    derived = tmp_path / "derived" / "case-1"
    derived.mkdir(parents=True)
    artifacts: list[RecordingEvidenceArtifact] = []
    for kind, name, mime_type in (
        ("clip", "context.mp4", "video/mp4"),
        ("frame", "frame-01.jpg", "image/jpeg"),
        ("frame", "frame-02.jpg", "image/jpeg"),
        ("frame", "frame-03.jpg", "image/jpeg"),
    ):
        path = derived / name
        path.write_bytes(name.encode("utf-8"))
        artifacts.append(
            RecordingEvidenceArtifact(
                kind=kind,
                path=path,
                mime_type=mime_type,
                size=path.stat().st_size,
                mtime=path.stat().st_mtime,
                sha256=sha256(path.read_bytes()).hexdigest(),
            )
        )

    with EventLedger(
        db_path,
        evidence_roots=[str(tmp_path)],
        recording_evidence=RecordingEvidenceConfig(enabled=True),
    ) as ledger:
        ledger.record(
            ReviewContext(
                event_id="case-1",
                source="camera-1",
                timestamp_ms=1000,
                frame_id=1,
            )
        )
        job = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, "extractor", lease_ms=1000)
        assert job is not None
        ledger.complete_evidence_extract_job(job, "extractor", tuple(artifacts))
        case = ledger.get_case("case-1")
    assert case is not None
    return [item.evidence_id for item in case.evidence]


def test_evidence_reader_resolves_only_registered_evidence_id(monkeypatch, tmp_path: Path) -> None:
    _fake_thread_data(monkeypatch, tmp_path)
    evidence_id, frame = _registered_evidence(tmp_path, monkeypatch)

    out = json.loads(
        evidence_reader_tool.func(None, "case-1", evidence_id=evidence_id)
    )

    assert out["available"] is True
    assert out["found"][0]["evidence_id"] == evidence_id
    assert str(frame) not in json.dumps(out)


def test_evidence_reader_unknown_event_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    _fake_thread_data(monkeypatch, tmp_path)
    out = json.loads(evidence_reader_tool.func(None, "1-0"))

    assert out["available"] is False
    assert out["reason"] == "event not found"


def test_evidence_reader_rejects_an_unregistered_evidence_id(monkeypatch, tmp_path: Path) -> None:
    _fake_thread_data(monkeypatch, tmp_path)
    _registered_evidence(tmp_path, monkeypatch)

    out = json.loads(evidence_reader_tool.func(None, "case-1", evidence_id="not-registered"))

    assert out["available"] is False
    assert out["reason"] == "registered evidence not found"


def test_evidence_reader_checks_registered_file_still_exists(monkeypatch, tmp_path: Path) -> None:
    _fake_thread_data(monkeypatch, tmp_path)
    evidence_id, frame = _registered_evidence(tmp_path, monkeypatch)
    frame.unlink()

    out = json.loads(
        evidence_reader_tool.func(None, "case-1", evidence_id=evidence_id)
    )

    assert out["available"] is False
    assert "file missing" in out["reason"]
    with EventLedger(tmp_path / "events.db") as ledger:
        case = ledger.get_case("case-1")
    assert case is not None
    assert case.evidence[0].available is False


def test_evidence_reader_refreshes_a_registered_file_that_appears_later(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _fake_thread_data(monkeypatch, tmp_path)
    db_path = tmp_path / "events.db"
    monkeypatch.setenv("SSV_EVENT_DB_PATH", str(db_path))
    monkeypatch.setenv("SSV_EVIDENCE_ROOTS", json.dumps([str(tmp_path)]))
    frame = tmp_path / "late-frame.png"
    with EventLedger(db_path, evidence_roots=[str(tmp_path)]) as ledger:
        ledger.record(
            ReviewContext(
                event_id="late-case",
                source="camera-1",
                timestamp_ms=1000,
                frame_id=1,
                frame_path=str(frame),
            )
        )
        case = ledger.get_case("late-case")
    assert case is not None
    evidence_id = case.evidence[0].evidence_id
    assert case.evidence[0].available is False
    frame.write_bytes(b"png")

    out = json.loads(
        evidence_reader_tool.func(None, "late-case", evidence_id=evidence_id)
    )

    assert out["available"] is True
    assert out["found"][0]["evidence_id"] == evidence_id
    with EventLedger(db_path) as ledger:
        refreshed = ledger.get_case("late-case")
    assert refreshed is not None
    assert refreshed.evidence[0].available is True


def test_evidence_reader_rejects_a_registered_file_replaced_by_external_symlink(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _fake_thread_data(monkeypatch, tmp_path)
    evidence_id, frame = _registered_evidence(tmp_path, monkeypatch)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    outside.write_bytes(b"outside")
    frame.unlink()
    frame.symlink_to(outside)

    out = json.loads(
        evidence_reader_tool.func(None, "case-1", evidence_id=evidence_id)
    )

    assert out["available"] is False
    assert out["found"] == []
    assert out["view_image_path"] is None
    assert str(outside) not in json.dumps(out)


def test_evidence_reader_copies_all_registered_context_frames_and_clip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_thread_data(monkeypatch, tmp_path)
    evidence_ids = _register_derived_context_evidence(tmp_path, monkeypatch)

    payload = json.loads(evidence_reader_tool.func(None, "case-1"))

    assert payload["available"] is True
    assert {item["evidence_id"] for item in payload["found"]} == set(evidence_ids)
    assert [item["kind"] for item in payload["found"]].count("clip") == 1
    assert [item["kind"] for item in payload["found"]].count("frame") == 3
    assert payload["view_image_path"] is not None
    serialised = json.dumps(payload)
    assert str(tmp_path) not in serialised
    assert "recordings" not in serialised
    assert all(
        item["virtual_path"].startswith(f"{VIRTUAL_PATH_PREFIX}/outputs/")
        for item in payload["found"]
    )
