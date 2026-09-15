"""从受控 MediaMTX 录像分段生成事件上下文证据。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlparse

from ssv_agent.config import AgentSourceConfig, RecordingEvidenceConfig
from ssv_agent.event_store import EventCase


_SEGMENT_NAME = re.compile(r"^(?P<seconds>[0-9]+)-(?P<microseconds>[0-9]{1,6})\.mp4$")
_COMMAND_TIMEOUT_SECONDS = 30
# 帧相对事件时间点的偏移；三帧依次为 T-1000ms、T、T+1000ms。
_FRAME_OFFSETS_MS: tuple[int, ...] = (-1000, 0, 1000)


@dataclass(frozen=True)
class RecordingEvidenceArtifact:
    """一个已经完整落盘的派生证据文件。"""

    kind: Literal["clip", "frame"]
    path: Path
    sha256: str
    mime_type: str
    size: int
    mtime: float


class RecordingEvidenceError(RuntimeError):
    """录像取证失败，``retryable`` 指示 worker 是否应重试。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class CommandRunner(Protocol):
    """可替换的命令执行边界，命令参数永远是数组。"""

    def run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class _Segment:
    path: Path
    start_ms: int
    end_ms: int


class RecordingEvidenceExtractor:
    """从一个 source 的本地录像目录构造完整的 clip 与三张帧。"""

    def __init__(
        self,
        sources: Mapping[str, AgentSourceConfig],
        config: RecordingEvidenceConfig,
        evidence_roots: Sequence[str],
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self._sources = sources
        self._config = config
        self._evidence_roots = evidence_roots
        self._runner = runner or _SubprocessRunner()

    def extract(self, case: EventCase) -> tuple[RecordingEvidenceArtifact, ...]:
        """生成完整证据集；没有完整窗口时返回可重试错误。"""
        root = self._evidence_root()
        source = self._sources.get(case.source)
        if source is None:
            raise RecordingEvidenceError("recording source is not configured", retryable=False)
        path_component = _safe_recording_path(source.uri)
        event_id = _safe_event_id(case.event_id)
        self._cleanup_recovery_temporary(root, event_id)
        existing = self._existing_destination(root, event_id)
        if existing is not None:
            return existing
        recordings_dir = self._recordings_dir(root, path_component)
        window_start_ms = case.timestamp_ms - self._config.clip_before_ms
        window_end_ms = case.timestamp_ms + self._config.clip_after_ms
        segments = self._select_segments(recordings_dir, window_start_ms, window_end_ms)

        derived_parent = root / "derived"
        try:
            derived_parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RecordingEvidenceError("derived output directory is unavailable", retryable=False) from exc
        if derived_parent.is_symlink() or not derived_parent.is_dir():
            raise RecordingEvidenceError("derived output directory is unavailable", retryable=False)

        temporary_dir = Path(tempfile.mkdtemp(dir=derived_parent, prefix=f".{event_id}."))
        try:
            artifacts = self._extract_to_temporary_dir(
                temporary_dir,
                segments,
                recordings_dir,
                window_start_ms,
                window_end_ms,
                case.timestamp_ms,
            )
            self._write_manifest(
                temporary_dir,
                segments,
                window_start_ms,
                window_end_ms,
                artifacts,
            )
            destination = derived_parent / event_id
            try:
                os.replace(temporary_dir, destination)
                _fsync_directory(derived_parent)
            except OSError as exc:
                raise RecordingEvidenceError("derived output could not be published", retryable=False) from exc
            return tuple(
                RecordingEvidenceArtifact(
                    kind=artifact.kind,
                    path=destination / artifact.path.name,
                    sha256=artifact.sha256,
                    mime_type=artifact.mime_type,
                    size=artifact.size,
                    mtime=(destination / artifact.path.name).stat().st_mtime,
                )
                for artifact in artifacts
            )
        except RecordingEvidenceError:
            raise
        except OSError as exc:
            raise RecordingEvidenceError("media extraction failed", retryable=True) from exc
        finally:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir, ignore_errors=True)

    @staticmethod
    def _existing_destination(root: Path, event_id: str) -> tuple[RecordingEvidenceArtifact, ...] | None:
        destination = root / "derived" / event_id
        try:
            destination_lstat = destination.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RecordingEvidenceError("derived output is unavailable", retryable=False) from exc
        try:
            destination_resolved = destination.resolve(strict=True)
        except OSError as exc:
            raise RecordingEvidenceError("derived output is unavailable", retryable=False) from exc
        derived_parent_resolved = RecordingEvidenceExtractor._validate_derived_parent(root)
        if (
            stat.S_ISLNK(destination_lstat.st_mode)
            or not stat.S_ISDIR(destination_lstat.st_mode)
            or not _is_within(destination_resolved, derived_parent_resolved)
        ):
            raise RecordingEvidenceError("derived output is unsafe", retryable=False)

        expected = (
            ("clip", "context.mp4", "video/mp4"),
            ("frame", "frame-01.jpg", "image/jpeg"),
            ("frame", "frame-02.jpg", "image/jpeg"),
            ("frame", "frame-03.jpg", "image/jpeg"),
        )
        artifacts: list[RecordingEvidenceArtifact] = []
        for kind, name, mime_type in expected:
            path = destination / name
            try:
                resolved = _validated_derived_file(path, destination_resolved)
            except RecordingEvidenceError:
                raise
            except OSError as exc:
                raise RecordingEvidenceError("derived output is incomplete", retryable=False) from exc
            artifacts.append(_artifact(kind, resolved, mime_type))
        manifest = destination / "manifest.json"
        _validated_derived_file(manifest, destination_resolved)
        try:
            with manifest.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecordingEvidenceError("derived manifest is invalid", retryable=False) from exc
        if not isinstance(payload, dict) or payload.get("time_basis") != "wall_clock_approximate":
            raise RecordingEvidenceError("derived manifest is invalid", retryable=False)
        entries = payload.get("artifacts")
        expected_names = [name for _, name, _ in expected]
        if not isinstance(entries, list) or len(entries) != 4:
            raise RecordingEvidenceError("derived manifest is invalid", retryable=False)
        by_name = {entry.get("name"): entry for entry in entries if isinstance(entry, dict)}
        if set(by_name) != set(expected_names):
            raise RecordingEvidenceError("derived manifest is invalid", retryable=False)
        for artifact, (kind, name, mime_type) in zip(artifacts, expected):
            entry = by_name[name]
            if (
                entry.get("kind") != kind
                or entry.get("mime_type") != mime_type
                or entry.get("sha256") != artifact.sha256
                or entry.get("size") != artifact.size
                or entry.get("mtime") != artifact.mtime
            ):
                raise RecordingEvidenceError("derived manifest is invalid", retryable=False)
        return tuple(artifacts)

    @staticmethod
    def _validate_derived_parent(root: Path) -> Path:
        derived_parent = root / "derived"
        try:
            details = derived_parent.lstat()
        except FileNotFoundError:
            return derived_parent
        except OSError as exc:
            raise RecordingEvidenceError("derived output is unavailable", retryable=False) from exc
        try:
            resolved = derived_parent.resolve(strict=True)
        except OSError as exc:
            raise RecordingEvidenceError("derived output is unsafe", retryable=False) from exc
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or not _is_within(resolved, root)
        ):
            raise RecordingEvidenceError("derived output is unsafe", retryable=False)
        return resolved

    @staticmethod
    def _cleanup_recovery_temporary(root: Path, event_id: str) -> None:
        derived_parent = root / "derived"
        if not derived_parent.exists() and not derived_parent.is_symlink():
            return
        parent_resolved = RecordingEvidenceExtractor._validate_derived_parent(root)
        try:
            entries = list(derived_parent.iterdir())
        except OSError as exc:
            raise RecordingEvidenceError("derived output is unavailable", retryable=False) from exc
        prefix = f".{event_id}."
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            try:
                details = entry.lstat()
                resolved = entry.resolve(strict=True)
            except OSError as exc:
                raise RecordingEvidenceError("derived output is unsafe", retryable=False) from exc
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISDIR(details.st_mode)
                or not _is_within(resolved, parent_resolved)
            ):
                raise RecordingEvidenceError("derived output is unsafe", retryable=False)
            try:
                shutil.rmtree(entry)
            except OSError as exc:
                raise RecordingEvidenceError("derived output cleanup failed", retryable=False) from exc

    def _evidence_root(self) -> Path:
        if not self._config.enabled:
            raise RecordingEvidenceError("recording evidence is disabled", retryable=False)
        if not self._evidence_roots:
            raise RecordingEvidenceError("recording evidence root is unavailable", retryable=False)
        root = Path(self._evidence_roots[0])
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise RecordingEvidenceError("recording evidence root is unavailable", retryable=False)
        try:
            return root.resolve(strict=True)
        except OSError as exc:
            raise RecordingEvidenceError("recording evidence root is unavailable", retryable=False) from exc

    @staticmethod
    def _recordings_dir(root: Path, path_component: str) -> Path:
        recordings_parent = root / "recordings"
        try:
            parent_lstat = recordings_parent.lstat()
            resolved_parent = recordings_parent.resolve(strict=True)
            if (
                stat.S_ISLNK(parent_lstat.st_mode)
                or not stat.S_ISDIR(parent_lstat.st_mode)
                or not _is_within(resolved_parent, root)
            ):
                raise RecordingEvidenceError("unsafe recording input", retryable=False)
        except RecordingEvidenceError:
            raise
        except OSError as exc:
            raise RecordingEvidenceError("recordings directory is unavailable", retryable=True) from exc

        recordings_dir = recordings_parent / path_component
        try:
            resolved = recordings_dir.resolve(strict=True)
        except OSError as exc:
            raise RecordingEvidenceError("recordings directory is unavailable", retryable=True) from exc
        if (
            recordings_dir.is_symlink()
            or not recordings_dir.is_dir()
            or not _is_within(resolved, resolved_parent)
        ):
            raise RecordingEvidenceError("unsafe recording input", retryable=False)
        return resolved

    def _select_segments(
        self,
        recordings_dir: Path,
        window_start_ms: int,
        window_end_ms: int,
    ) -> tuple[_Segment, ...]:
        candidates: list[tuple[Path, re.Match[str], int]] = []
        try:
            entries = list(recordings_dir.iterdir())
        except OSError as exc:
            raise RecordingEvidenceError("recording window unavailable", retryable=True) from exc
        for entry in entries:
            match = _SEGMENT_NAME.fullmatch(entry.name)
            if match is None:
                continue
            candidates.append((entry, match, _segment_start_ms(match)))

        selected: list[_Segment] = []
        cursor_ms = window_start_ms
        for entry, match, start_ms in sorted(candidates, key=lambda item: item[2]):
            if start_ms > cursor_ms:
                raise RecordingEvidenceError("recording window unavailable", retryable=True)
            segment = self._probe_segment(entry, recordings_dir, match)
            if segment.end_ms <= cursor_ms or segment.start_ms > cursor_ms:
                continue
            selected.append(segment)
            cursor_ms = max(cursor_ms, segment.end_ms)
            if cursor_ms >= window_end_ms:
                return tuple(selected)
        raise RecordingEvidenceError("recording window unavailable", retryable=True)

    def _probe_segment(
        self,
        entry: Path,
        recordings_dir: Path,
        match: re.Match[str],
    ) -> _Segment:
        resolved = _validated_recording_input(entry, recordings_dir)
        completed = self._run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(resolved),
            ],
            error_message="recording probe failed",
        )
        try:
            duration_ms = round(float(completed.stdout.strip()) * 1000)
        except (AttributeError, ValueError) as exc:
            raise RecordingEvidenceError("recording probe failed", retryable=True) from exc
        if duration_ms <= 0:
            raise RecordingEvidenceError("recording probe failed", retryable=True)
        start_ms = _segment_start_ms(match)
        return _Segment(path=resolved, start_ms=start_ms, end_ms=start_ms + duration_ms)

    def _extract_to_temporary_dir(
        self,
        temporary_dir: Path,
        segments: tuple[_Segment, ...],
        recordings_dir: Path,
        window_start_ms: int,
        window_end_ms: int,
        event_timestamp_ms: int,
    ) -> tuple[RecordingEvidenceArtifact, ...]:
        segments = tuple(
            _Segment(
                path=_validated_recording_input(segment.path, recordings_dir),
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
            )
            for segment in segments
        )
        concat_list = temporary_dir / "segments.txt"
        _write_concat_list(concat_list, segments)
        relative_window_start_s = (window_start_ms - segments[0].start_ms) / 1000
        clip_path = temporary_dir / "context.mp4"
        self._run_ffmpeg(
            concat_list,
            relative_window_start_s,
            (window_end_ms - window_start_ms) / 1000,
            clip_path,
            ["-c:v", "libx264", "-an"],
        )
        _fsync_file(clip_path)
        artifacts = [_artifact("clip", clip_path, "video/mp4")]
        for index, offset_ms in enumerate(_FRAME_OFFSETS_MS, start=1):
            frame_path = temporary_dir / f"frame-{index:02d}.jpg"
            relative_frame_s = (event_timestamp_ms + offset_ms - segments[0].start_ms) / 1000
            self._run_ffmpeg(
                concat_list,
                relative_frame_s,
                0,
                frame_path,
                ["-frames:v", "1", "-q:v", "2"],
            )
            _fsync_file(frame_path)
            artifacts.append(_artifact("frame", frame_path, "image/jpeg"))
        return tuple(artifacts)

    def _run_ffmpeg(
        self,
        concat_list: Path,
        seek_seconds: float,
        duration_seconds: float,
        output: Path,
        output_args: list[str],
    ) -> None:
        args = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-ss",
            f"{seek_seconds:.3f}",
        ]
        if duration_seconds:
            args.extend(["-t", f"{duration_seconds:.3f}"])
        args.extend([*output_args, str(output)])
        self._run(args, error_message="media extraction failed")

    def _run(self, args: list[str], *, error_message: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._runner.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RecordingEvidenceError(error_message, retryable=True) from exc
        if completed.returncode != 0:
            raise RecordingEvidenceError(error_message, retryable=True)
        return completed

    @staticmethod
    def _write_manifest(
        temporary_dir: Path,
        segments: tuple[_Segment, ...],
        window_start_ms: int,
        window_end_ms: int,
        artifacts: tuple[RecordingEvidenceArtifact, ...],
    ) -> None:
        payload = {
            "time_basis": "wall_clock_approximate",
            "inputs": [segment.path.name for segment in segments],
            "requested_interval_ms": [window_start_ms, window_end_ms],
            "actual_interval_ms": [segments[0].start_ms, segments[-1].end_ms],
            "artifacts": [
                {
                    "name": artifact.path.name,
                    "kind": artifact.kind,
                    "sha256": artifact.sha256,
                    "mime_type": artifact.mime_type,
                    "size": artifact.size,
                    "mtime": artifact.mtime,
                }
                for artifact in artifacts
            ],
        }
        manifest = temporary_dir / "manifest.json"
        with manifest.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())


class _SubprocessRunner:
    def run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, **kwargs)  # type: ignore[arg-type]


def _safe_recording_path(uri: str) -> str:
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if (
        parsed.scheme not in {"rtsp", "rtsps"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or not path.startswith("/")
    ):
        raise RecordingEvidenceError("source URI does not contain a safe path", retryable=False)
    components = path.split("/")
    if len(components) != 2 or not components[1] or components[1] in {".", ".."}:
        raise RecordingEvidenceError("source URI does not contain a safe path", retryable=False)
    component = components[1]
    if "/" in component or "\\" in component or "\x00" in component:
        raise RecordingEvidenceError("source URI does not contain a safe path", retryable=False)
    return component


def _safe_event_id(event_id: str) -> str:
    if not event_id or event_id in {".", ".."} or "/" in event_id or "\\" in event_id or "\x00" in event_id:
        raise RecordingEvidenceError("event identifier is unsafe", retryable=False)
    return event_id


def _segment_start_ms(match: re.Match[str]) -> int:
    return int(match["seconds"]) * 1000 + int(match["microseconds"]) // 1000


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _validated_recording_input(entry: Path, recordings_dir: Path) -> Path:
    try:
        entry_lstat = entry.lstat()
        resolved = entry.resolve(strict=True)
    except OSError as exc:
        raise RecordingEvidenceError("unsafe recording input", retryable=False) from exc
    if (
        stat.S_ISLNK(entry_lstat.st_mode)
        or not stat.S_ISREG(entry_lstat.st_mode)
        or not _is_within(resolved, recordings_dir)
    ):
        raise RecordingEvidenceError("unsafe recording input", retryable=False)
    return resolved


def _validated_derived_file(entry: Path, destination: Path) -> Path:
    try:
        details = entry.lstat()
        resolved = entry.resolve(strict=True)
    except OSError as exc:
        raise RecordingEvidenceError("derived output is incomplete", retryable=False) from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or not _is_within(resolved, destination)
    ):
        raise RecordingEvidenceError("derived output is unsafe", retryable=False)
    return resolved


def _write_concat_list(path: Path, segments: tuple[_Segment, ...]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for segment in segments:
            # Input paths were resolved and containment-checked before reaching this point.
            handle.write("file '" + str(segment.path).replace("'", "'\\\\''") + "'\n")
        handle.flush()
        os.fsync(handle.fileno())


def _artifact(kind: Literal["clip", "frame"], path: Path, mime_type: str) -> RecordingEvidenceArtifact:
    data = path.read_bytes()
    details = path.stat()
    return RecordingEvidenceArtifact(
        kind=kind,
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        mime_type=mime_type,
        size=details.st_size,
        mtime=details.st_mtime,
    )


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
