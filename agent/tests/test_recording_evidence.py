from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ssv_agent.config import AgentSourceConfig, RecordingEvidenceConfig
from ssv_agent.event_store import EventCase
from ssv_agent.recording_evidence import RecordingEvidenceError, RecordingEvidenceExtractor


@dataclass(frozen=True)
class RecordedCall:
    args: tuple[str, ...]
    kwargs: dict[str, Any]


class FakeCommandRunner:
    def __init__(
        self,
        probe_durations: dict[str, float],
        *,
        fail_ffmpeg: bool = False,
    ) -> None:
        self.probe_durations = probe_durations
        self.fail_ffmpeg = fail_ffmpeg
        self.calls: list[RecordedCall] = []

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(RecordedCall(tuple(args), kwargs))
        if args[0] == "ffprobe":
            duration = self.probe_durations[Path(args[-1]).name]
            return subprocess.CompletedProcess(args, 0, stdout=f"{duration}\n", stderr="")
        if self.fail_ffmpeg:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="broken encoder")
        Path(args[-1]).write_bytes(b"derived-media")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _case(*, timestamp_ms: int = 1_700_000_002_500, event_id: str = "case-1") -> EventCase:
    return EventCase(
        event_id=event_id,
        ingress_id=None,
        source="camera-1",
        timestamp_ms=timestamp_ms,
        frame_id=1,
        stream_generation=None,
        source_pts=None,
        event_type=None,
        severity=None,
        rule_id=None,
        rule_version=None,
        rule_facts={},
        revision=1,
        status="new",
        verdict=None,
        confidence=None,
        detections=(),
        evidence=(),
        review=None,
    )


def _extractor(
    root: Path,
    *,
    uri: str = "rtsp://host/stream",
    runner: FakeCommandRunner | None = None,
) -> RecordingEvidenceExtractor:
    kwargs: dict[str, Any] = {}
    if runner is not None:
        kwargs["runner"] = runner
    return RecordingEvidenceExtractor(
        {"camera-1": AgentSourceConfig(id="camera-1", uri=uri)},
        RecordingEvidenceConfig(enabled=True),
        [str(root)],
        **kwargs,
    )


def _segment(root: Path, name: str = "1700000000-1.mp4") -> Path:
    recordings = root / "recordings" / "stream"
    recordings.mkdir(parents=True)
    path = recordings / name
    path.write_bytes(b"source-segment")
    return path


def test_extract_rejects_nested_rtsp_path_and_symlink_segment(tmp_path: Path) -> None:
    extractor = _extractor(tmp_path, uri="rtsp://host/a/b")

    with pytest.raises(RecordingEvidenceError, match="safe path") as error:
        extractor.extract(_case())

    assert error.value.retryable is False


def test_extract_rejects_symlink_segment_without_exposing_absolute_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"outside")
    recordings = tmp_path / "recordings" / "stream"
    recordings.mkdir(parents=True)
    (recordings / "1700000000-1.mp4").symlink_to(outside)

    with pytest.raises(RecordingEvidenceError, match="unsafe recording input") as error:
        _extractor(tmp_path).extract(_case())

    assert error.value.retryable is False
    assert str(tmp_path) not in str(error.value)
    assert str(outside) not in str(error.value)


def test_extract_rejects_root_escape_regular_file(tmp_path: Path) -> None:
    recording = _segment(tmp_path)
    other_root = tmp_path.parent / f"{tmp_path.name}-other"
    other_root.mkdir()
    escaped = other_root / recording.name
    recording.replace(escaped)
    recording.symlink_to(escaped)

    with pytest.raises(RecordingEvidenceError, match="unsafe recording input") as error:
        _extractor(tmp_path).extract(_case())

    assert error.value.retryable is False
    assert str(other_root) not in str(error.value)


def test_extract_rejects_regular_segment_reached_through_recordings_symlink(tmp_path: Path) -> None:
    outside_recordings = tmp_path.parent / f"{tmp_path.name}-recordings"
    stream = outside_recordings / "stream"
    stream.mkdir(parents=True)
    (stream / "1700000000-1.mp4").write_bytes(b"outside")
    (tmp_path / "recordings").symlink_to(outside_recordings, target_is_directory=True)

    with pytest.raises(RecordingEvidenceError, match="unsafe recording input") as error:
        _extractor(
            tmp_path,
            runner=FakeCommandRunner({"1700000000-1.mp4": 5.0}),
        ).extract(_case())

    assert error.value.retryable is False
    assert str(outside_recordings) not in str(error.value)


def test_extract_revalidates_segment_before_passing_it_to_ffmpeg(tmp_path: Path) -> None:
    recording = _segment(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-replacement.mp4"
    outside.write_bytes(b"outside")
    runner = FakeCommandRunner({"1700000000-1.mp4": 5.0})
    fake_run = runner.run

    def swap_after_probe(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = fake_run(args, **kwargs)
        if args[0] == "ffprobe":
            recording.unlink()
            recording.symlink_to(outside)
        return result

    runner.run = swap_after_probe  # type: ignore[method-assign]
    with pytest.raises(RecordingEvidenceError, match="unsafe recording input") as error:
        _extractor(tmp_path, runner=runner).extract(_case())

    assert error.value.retryable is False
    assert [call.args[0] for call in runner.calls] == ["ffprobe"]


def test_extract_reports_uncovered_window_as_retryable(tmp_path: Path) -> None:
    _segment(tmp_path)
    runner = FakeCommandRunner({"1700000000-1.mp4": 2.0})

    with pytest.raises(RecordingEvidenceError, match="recording window unavailable") as error:
        _extractor(tmp_path, runner=runner).extract(_case())

    assert error.value.retryable is True


def test_extract_rejects_gap_even_when_later_segment_covers_window_end(tmp_path: Path) -> None:
    _segment(tmp_path, "1700000000-1.mp4")
    recordings = tmp_path / "recordings" / "stream"
    (recordings / "1700000003-1.mp4").write_bytes(b"later-segment")
    runner = FakeCommandRunner(
        {
            "1700000000-1.mp4": 1.0,
            "1700000003-1.mp4": 5.0,
        }
    )

    with pytest.raises(RecordingEvidenceError, match="recording window unavailable") as error:
        _extractor(tmp_path, runner=runner).extract(_case())

    assert error.value.retryable is True
    assert [Path(call.args[-1]).name for call in runner.calls] == ["1700000000-1.mp4"]


def test_extract_selects_covering_segments_and_commits_four_artifacts(tmp_path: Path) -> None:
    _segment(tmp_path)
    runner = FakeCommandRunner(probe_durations={"1700000000-1.mp4": 5.0})

    artifacts = _extractor(tmp_path, runner=runner).extract(_case())

    assert [item.kind for item in artifacts] == ["clip", "frame", "frame", "frame"]
    assert all(item.path.is_file() for item in artifacts)
    assert runner.calls[0].args[0] == "ffprobe"
    assert all("shell" not in call.kwargs for call in runner.calls)
    assert all(call.kwargs["timeout"] > 0 for call in runner.calls)
    assert [call.args[0] for call in runner.calls] == ["ffprobe", "ffmpeg", "ffmpeg", "ffmpeg", "ffmpeg"]
    assert all("-f" in call.args and "concat" in call.args for call in runner.calls[1:])
    seeks = [float(call.args[call.args.index("-ss") + 1]) for call in runner.calls[1:5]]
    assert [round(value - seeks[0], 3) for value in seeks] == [0.0, 1.5, 2.5, 3.5]

    manifest = json.loads((tmp_path / "derived" / "case-1" / "manifest.json").read_text())
    assert manifest["time_basis"] == "wall_clock_approximate"
    assert manifest["inputs"] == ["1700000000-1.mp4"]
    assert manifest["requested_interval_ms"] == [1_700_000_000_000, 1_700_000_005_000]
    assert manifest["actual_interval_ms"] == [1_700_000_000_000, 1_700_000_005_000]
    assert {item["name"] for item in manifest["artifacts"]} == {
        "context.mp4",
        "frame-01.jpg",
        "frame-02.jpg",
        "frame-03.jpg",
    }


def test_extract_reuses_complete_existing_destination_without_running_commands(tmp_path: Path) -> None:
    destination = tmp_path / "derived" / "case-1"
    destination.mkdir(parents=True)
    (destination / "context.mp4").write_bytes(b"existing-clip")
    for index in range(1, 4):
        (destination / f"frame-{index:02d}.jpg").write_bytes(f"frame-{index}".encode())
    _write_manifest_for_destination(destination)
    runner = FakeCommandRunner({})

    artifacts = _extractor(tmp_path, runner=runner).extract(_case())

    assert [item.kind for item in artifacts] == ["clip", "frame", "frame", "frame"]
    assert [item.path.name for item in artifacts] == [
        "context.mp4",
        "frame-01.jpg",
        "frame-02.jpg",
        "frame-03.jpg",
    ]
    assert all(item.path.parent == destination for item in artifacts)
    assert runner.calls == []


def test_extract_cleans_stale_recovery_temporary_directory(tmp_path: Path) -> None:
    _segment(tmp_path)
    stale = tmp_path / "derived" / ".case-1.stale"
    stale.mkdir(parents=True)
    (stale / "partial.mp4").write_bytes(b"partial")
    runner = FakeCommandRunner({"1700000000-1.mp4": 5.0})

    _extractor(tmp_path, runner=runner).extract(_case())

    assert not stale.exists()
    assert (tmp_path / "derived" / "case-1").is_dir()


def test_extract_rejects_unsafe_recovery_temporary_symlink(tmp_path: Path) -> None:
    _segment(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-stale-outside"
    outside.mkdir()
    (tmp_path / "derived").mkdir()
    stale = tmp_path / "derived" / ".case-1.stale"
    stale.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RecordingEvidenceError, match="derived output") as error:
        _extractor(tmp_path, runner=FakeCommandRunner({"1700000000-1.mp4": 5.0})).extract(_case())

    assert error.value.retryable is False
    assert stale.is_symlink()


def _write_manifest_for_destination(destination: Path) -> None:
    items = []
    for name, kind, mime in (
        ("context.mp4", "clip", "video/mp4"),
        ("frame-01.jpg", "frame", "image/jpeg"),
        ("frame-02.jpg", "frame", "image/jpeg"),
        ("frame-03.jpg", "frame", "image/jpeg"),
    ):
        path = destination / name
        data = path.read_bytes()
        items.append({
            "name": name,
            "kind": kind,
            "sha256": hashlib.sha256(data).hexdigest(),
            "mime_type": mime,
            "size": len(data),
            "mtime": path.stat().st_mtime,
        })
    (destination / "manifest.json").write_text(
        json.dumps({"time_basis": "wall_clock_approximate", "artifacts": items}),
        encoding="utf-8",
    )


def test_extract_rejects_derived_symlink_destination(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    derived = tmp_path / "derived"
    derived.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RecordingEvidenceError, match="derived output") as error:
        _extractor(tmp_path, runner=FakeCommandRunner({})).extract(_case())

    assert error.value.retryable is False


def test_extract_rejects_tampered_manifest_metadata(tmp_path: Path) -> None:
    destination = tmp_path / "derived" / "case-1"
    destination.mkdir(parents=True)
    (destination / "context.mp4").write_bytes(b"existing-clip")
    for index in range(1, 4):
        (destination / f"frame-{index:02d}.jpg").write_bytes(f"frame-{index}".encode())
    _write_manifest_for_destination(destination)
    payload = json.loads((destination / "manifest.json").read_text())
    payload["time_basis"] = "exact"
    payload["artifacts"][0]["sha256"] = "0" * 64
    (destination / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RecordingEvidenceError, match="manifest") as error:
        _extractor(tmp_path, runner=FakeCommandRunner({})).extract(_case())

    assert error.value.retryable is False


def test_extract_uses_stable_artifact_hashes(tmp_path: Path) -> None:
    _segment(tmp_path)
    artifacts = _extractor(
        tmp_path,
        runner=FakeCommandRunner({"1700000000-1.mp4": 5.0}),
    ).extract(_case())

    for artifact in artifacts:
        assert artifact.sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
        assert artifact.size == len(artifact.path.read_bytes())
        assert artifact.mime_type in {"video/mp4", "image/jpeg"}


def test_extract_cleans_temporary_directory_when_ffmpeg_fails(tmp_path: Path) -> None:
    _segment(tmp_path)

    with pytest.raises(RecordingEvidenceError, match="media extraction failed") as error:
        _extractor(
            tmp_path,
            runner=FakeCommandRunner({"1700000000-1.mp4": 5.0}, fail_ffmpeg=True),
        ).extract(_case())

    assert error.value.retryable is True
    assert not (tmp_path / "derived" / "case-1").exists()
    assert not list((tmp_path / "derived").glob(".case-1.*"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_extract_with_real_ffmpeg_writes_five_second_clip_and_three_jpegs(tmp_path: Path) -> None:
    recording = _segment(tmp_path)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=10",
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(recording),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    artifacts = _extractor(tmp_path).extract(_case())
    clip = artifacts[0].path
    duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(clip)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert float(duration.stdout) == pytest.approx(5.0, abs=0.15)
    assert [item.path.suffix for item in artifacts[1:]] == [".jpg", ".jpg", ".jpg"]
    assert all(item.path.stat().st_size > 0 for item in artifacts)
