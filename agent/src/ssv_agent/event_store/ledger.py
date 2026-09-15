"""事件证据链的事务边界。"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ssv_agent.config import RecordingEvidenceConfig
from ssv_agent.event_store.sqlite_store import SsvEventStore

if TYPE_CHECKING:
    from ssv_agent.recording_evidence import RecordingEvidenceArtifact
from ssv_agent.review_context import ReviewContext


class JobKind(StrEnum):
    """账本可持久化的异步工作类型。"""

    REVIEW = "review"
    INDEX = "index"
    EVIDENCE_EXTRACT = "evidence_extract"


class JobState(StrEnum):
    """持久任务的可见状态。"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    DEAD = "dead"


class LeaseLostError(RuntimeError):
    """任务已被其他 worker 接管或本 worker 的 lease 已失效。"""


@dataclass(frozen=True)
class EvidenceRef:
    """只指向登记证据的稳定引用。"""

    evidence_id: str
    kind: str
    path: str
    mime_type: str | None
    available: bool
    size: int | None
    mtime: float | None
    sha256: str | None
    source_pts_start: int | None
    source_pts_end: int | None
    stream_generation: int | None


@dataclass(frozen=True)
class EventCase:
    """从账本重建出的权威案件快照。"""

    event_id: str
    ingress_id: str | None
    source: str
    timestamp_ms: int
    frame_id: int
    stream_generation: int | None
    source_pts: int | None
    event_type: str | None
    severity: str | None
    rule_id: str | None
    rule_version: str | None
    rule_facts: dict[str, Any]
    revision: int
    status: str
    verdict: str | None
    confidence: float | None
    detections: tuple[dict[str, Any], ...]
    evidence: tuple[EvidenceRef, ...]
    review: dict[str, Any] | None

    def to_review_context(self) -> ReviewContext:
        """从权威案件重建模型可读的输入，不暴露未登记的新路径。"""
        detections = []
        for detection in self.detections:
            copied = dict(detection)
            copied["bbox"] = _json_value(copied.pop("bbox_json", "[]"), [])
            detections.append(copied)
        frame = next((item.path for item in self.evidence if item.kind == "frame"), None)
        clip = next((item.path for item in self.evidence if item.kind == "clip"), None)
        return ReviewContext(
            event_id=self.event_id,
            ingress_id=self.ingress_id,
            source=self.source,
            timestamp_ms=self.timestamp_ms,
            frame_id=self.frame_id,
            stream_generation=self.stream_generation,
            source_pts=self.source_pts,
            event_type=self.event_type,
            severity=self.severity,
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            rule_facts=self.rule_facts,
            detections=detections,
            frame_path=frame,
            clip_path=clip,
            evidence_ids=[item.evidence_id for item in self.evidence if item.available],
        )


@dataclass(frozen=True)
class DurableJob:
    """一条可租约、可重试的持久任务。"""

    job_id: int
    kind: JobKind
    entity_id: str
    entity_revision: int
    state: JobState
    attempts: int
    lease_owner: str | None
    lease_expires_ms: int | None
    available_at_ms: int
    last_error: str | None


@dataclass(frozen=True)
class RecordOutcome:
    """一次幂等 record 的结果。"""

    case: EventCase
    jobs: tuple[DurableJob, ...]
    created: bool


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_value(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _evidence_id(event_id: str, kind: str, path: str) -> str:
    value = f"{event_id}\0{kind}\0{path}".encode()
    return hashlib.sha256(value).hexdigest()


def _normalise_evidence_roots(value: object, *, source: str) -> tuple[Path, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{source} must be a JSON string array" if source.startswith("SSV_") else f"{source} must be a list")

    roots: list[Path] = []
    for item in value:
        if not isinstance(item, str) or not Path(item).is_absolute():
            raise ValueError(f"{source} entries must be absolute paths")
        try:
            resolved = Path(item).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{source} contains an unresolvable path") from exc
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _load_evidence_roots(evidence_roots: list[str] | None) -> tuple[Path, ...]:
    if evidence_roots is not None:
        return _normalise_evidence_roots(evidence_roots, source="evidence_roots")

    raw = os.environ.get("SSV_EVIDENCE_ROOTS")
    if raw is None:
        return ()
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("SSV_EVIDENCE_ROOTS must be a JSON string array") from exc
    return _normalise_evidence_roots(value, source="SSV_EVIDENCE_ROOTS")


class EventLedger:
    """封装案件、证据和首批持久任务的权威 SQLite 事务。"""

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        evidence_roots: list[str] | None = None,
        recording_evidence: RecordingEvidenceConfig | None = None,
    ) -> None:
        self._evidence_roots = _load_evidence_roots(evidence_roots)
        self._recording_evidence = recording_evidence or RecordingEvidenceConfig()
        self._store = SsvEventStore(db_path)

    @property
    def evidence_roots(self) -> tuple[Path, ...]:
        """返回已规范化的可信证据根目录。"""
        return self._evidence_roots

    def resolve_evidence_path(self, path: str | os.PathLike[str]) -> Path | None:
        """解析并验证证据路径；越界、相对或无法解析的路径返回 None。"""
        try:
            candidate = Path(path)
        except (TypeError, ValueError):
            return None
        if not candidate.is_absolute():
            return None
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        if any(resolved == root or root in resolved.parents for root in self._evidence_roots):
            return resolved
        return None

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "EventLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record(self, context: ReviewContext) -> RecordOutcome:
        """原子登记一件案件及其首批 review/index 工作。"""
        now_ms = _now_ms()
        event_id = context.event_id
        with self._store.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO events (
                        event_id, ingress_id, source, timestamp_ms, frame_id,
                        stream_generation, source_pts, event_type, severity,
                        rule_id, rule_version, rule_facts_json, revision, status, created_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?)
                    """,
                    (
                        event_id,
                        getattr(context, "ingress_id", event_id),
                        context.source,
                        context.timestamp_ms,
                        context.frame_id,
                        getattr(context, "stream_generation", None),
                        getattr(context, "source_pts", None),
                        context.event_type,
                        context.severity,
                        getattr(context, "rule_id", None),
                        getattr(context, "rule_version", None),
                        json.dumps(getattr(context, "rule_facts", {}), ensure_ascii=False),
                        now_ms,
                    ),
                )
                self._record_detections(connection, event_id, context)
                self._record_evidence(connection, event_id, context)
                if self._recording_evidence.enabled:
                    jobs_to_create = (
                        (JobKind.EVIDENCE_EXTRACT, context.timestamp_ms + self._recording_evidence.clip_after_ms),
                    )
                else:
                    jobs_to_create = ((JobKind.REVIEW, now_ms), (JobKind.INDEX, now_ms))
                for job_kind, available_at_ms in jobs_to_create:
                    connection.execute(
                        """
                        INSERT INTO durable_jobs (
                            kind, entity_id, entity_revision, state, attempts,
                            available_at_ms, created_ms, updated_ms
                        ) VALUES (?, ?, 0, 'pending', 0, ?, ?, ?)
                        ON CONFLICT(kind, entity_id, entity_revision) DO NOTHING
                        """,
                        (job_kind.value, event_id, available_at_ms, now_ms, now_ms),
                    )

        case = self.get_case(event_id)
        if case is None:  # pragma: no cover - guarded by the transaction above
            raise RuntimeError(f"event ledger lost recorded case: {event_id}")
        return RecordOutcome(
            case=case,
            jobs=self._jobs_for_event(event_id),
            created=existing is None,
        )

    def get_case(self, event_id: str) -> EventCase | None:
        """返回可供 worker 和只读工具消费的案件快照。"""
        stored = self._store.get_event(event_id)
        if stored is None:
            return None
        event = stored["event"]
        result = self._store.get_result(event_id)
        if result is not None:
            result["evidence_ids"] = _json_value(result.pop("evidence_ids_json", "[]"), [])
            result["claims"] = _json_value(result.pop("claims_json", "[]"), [])
        evidence = tuple(
            EvidenceRef(
                evidence_id=row["evidence_id"] or f"legacy-{row['id']}",
                kind=row["kind"],
                path=row["path"],
                mime_type=row["mime_type"],
                available=bool(row["available"]),
                size=row["size"],
                mtime=row["mtime"],
                sha256=row["sha256"],
                source_pts_start=row["source_pts_start"],
                source_pts_end=row["source_pts_end"],
                stream_generation=row["stream_generation"],
            )
            for row in stored["evidence"]
        )
        return EventCase(
            event_id=event["event_id"],
            ingress_id=event["ingress_id"],
            source=event["source"],
            timestamp_ms=event["timestamp_ms"],
            frame_id=event["frame_id"],
            stream_generation=event["stream_generation"],
            source_pts=event["source_pts"],
            event_type=event["event_type"],
            severity=event["severity"],
            rule_id=event["rule_id"],
            rule_version=event["rule_version"],
            rule_facts=_json_value(event["rule_facts_json"], {}),
            revision=event["revision"],
            status=event["status"],
            verdict=event["verdict"],
            confidence=event["confidence"],
            detections=tuple(stored["detections"]),
            evidence=evidence,
            review=result,
        )

    def append_review(self, event_id: str, result: Any, result_path: str) -> int:
        """追加模型复核、更新兼容投影，并为新 revision 建立索引工作。"""
        now_ms = _now_ms()
        with self._store.transaction(immediate=True) as connection:
            event = connection.execute(
                "SELECT revision FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if event is None:
                raise KeyError(f"unknown event case: {event_id}")
            return self._append_review_in_transaction(
                connection,
                event_id,
                event,
                result,
                result_path,
                now_ms,
            )

    def refresh_evidence(self, event_id: str, evidence_id: str | None = None) -> int:
        """刷新已登记证据并返回匹配数；未知 event/evidence 返回 0。"""
        with self._store.transaction(immediate=True) as connection:
            return self._refresh_evidence_in_transaction(
                connection,
                event_id,
                evidence_id,
            )

    def complete_review_job(
        self,
        job: DurableJob,
        worker_id: str,
        result: Any,
        result_path: str,
    ) -> int:
        """以持有者 fence 原子提交 review、投影、索引任务和 job 完成状态。"""
        if job.kind != JobKind.REVIEW:
            raise ValueError("complete_review_job requires a review job")

        with self._store.transaction(immediate=True) as connection:
            now_ms = _now_ms()
            current_job = connection.execute(
                "SELECT * FROM durable_jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone()
            self._validate_review_lease(current_job, job, worker_id, now_ms)

            event = connection.execute(
                "SELECT revision FROM events WHERE event_id = ?", (job.entity_id,)
            ).fetchone()
            if event is None:
                raise KeyError(f"unknown event case: {job.entity_id}")
            if event["revision"] != job.entity_revision:
                raise ValueError(
                    "review job revision no longer matches the current event revision"
                )
            self._refresh_evidence_in_transaction(connection, job.entity_id)

            revision = self._append_review_in_transaction(
                connection,
                job.entity_id,
                event,
                result,
                result_path,
                now_ms,
            )
            self._complete_owned_job(connection, job, worker_id)
        return revision

    def complete_evidence_extract_job(
        self,
        job: DurableJob,
        worker_id: str,
        artifacts: tuple["RecordingEvidenceArtifact", ...],
    ) -> None:
        """原子登记完整取证集、创建 review，并完成 extract job。"""
        if job.kind != JobKind.EVIDENCE_EXTRACT:
            raise ValueError("complete_evidence_extract_job requires an evidence extract job")
        with self._store.transaction(immediate=True) as connection:
            now_ms = _now_ms()
            current = connection.execute(
                "SELECT * FROM durable_jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone()
            if current is not None and current["state"] == JobState.COMPLETED.value:
                return
            self._validate_extract_lease(current, job, worker_id, now_ms)
            event = connection.execute(
                "SELECT revision FROM events WHERE event_id = ?", (job.entity_id,)
            ).fetchone()
            if event is None:
                raise KeyError(f"unknown event case: {job.entity_id}")
            if event["revision"] != job.entity_revision:
                raise ValueError("evidence extract job revision no longer matches the current event revision")
            kinds = [artifact.kind for artifact in artifacts]
            if len(artifacts) != 4 or kinds.count("clip") != 1 or kinds.count("frame") != 3:
                raise ValueError("evidence extract requires exactly one clip and three frames")
            for artifact in artifacts:
                resolved = self.resolve_evidence_path(artifact.path)
                if resolved is None or not self._artifact_metadata_matches(resolved, artifact):
                    raise ValueError("evidence artifact path or metadata is invalid")
                canonical = str(resolved)
                evidence_id = _evidence_id(job.entity_id, artifact.kind, canonical)
                connection.execute(
                    """
                    INSERT INTO evidence (
                        event_id, evidence_id, kind, path, mime_type, available,
                        size, mtime, sha256, source_pts_start, source_pts_end, stream_generation
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, NULL, NULL)
                    ON CONFLICT(event_id, kind, path) DO UPDATE SET
                        evidence_id = excluded.evidence_id, mime_type = excluded.mime_type,
                        available = 1, size = excluded.size, mtime = excluded.mtime,
                        sha256 = excluded.sha256
                    """,
                    (job.entity_id, evidence_id, artifact.kind, canonical,
                     artifact.mime_type, artifact.size, artifact.mtime, artifact.sha256),
                )
            connection.execute(
                """
                INSERT INTO durable_jobs (
                    kind, entity_id, entity_revision, state, attempts,
                    available_at_ms, created_ms, updated_ms
                ) VALUES ('review', ?, ?, 'pending', 0, ?, ?, ?)
                ON CONFLICT(kind, entity_id, entity_revision) DO NOTHING
                """,
                (job.entity_id, job.entity_revision, now_ms, now_ms, now_ms),
            )
            self._complete_owned_job_kind(connection, job, worker_id, JobKind.EVIDENCE_EXTRACT)

    def fail_evidence_extract_job(
        self,
        job: DurableJob,
        worker_id: str,
        error: str,
        max_retries: int,
        retry_delay_ms: int,
    ) -> JobState:
        """记录取证失败；达到上限时与事件转人工复核同事务提交。"""
        if job.kind != JobKind.EVIDENCE_EXTRACT:
            raise ValueError("fail_evidence_extract_job requires an evidence extract job")
        with self._store.transaction(immediate=True) as connection:
            state = self._fail_job_in_transaction(
                connection, job.job_id, worker_id, job.attempts,
                error, max_retries, retry_delay_ms,
            )
            if state == JobState.DEAD:
                connection.execute(
                    "UPDATE events SET status = 'manual_review' WHERE event_id = ?",
                    (job.entity_id,),
                )
            return state

    def enqueue_all_index_jobs(self) -> int:
        """为当前案件投影重新入队 index job，供可重建的全量索引使用。"""
        now_ms = _now_ms()
        requeued = 0
        with self._store.transaction(immediate=True) as connection:
            rows = connection.execute("SELECT event_id, revision FROM events").fetchall()
            for row in rows:
                cursor = connection.execute(
                    """
                    INSERT INTO durable_jobs (
                        kind, entity_id, entity_revision, state, attempts,
                        available_at_ms, created_ms, updated_ms
                    ) VALUES ('index', ?, ?, 'pending', 0, ?, ?, ?)
                    ON CONFLICT(kind, entity_id, entity_revision) DO UPDATE SET
                        state = 'pending', lease_owner = NULL,
                        lease_expires_ms = NULL, available_at_ms = excluded.available_at_ms,
                        last_error = NULL, updated_ms = excluded.updated_ms
                    WHERE durable_jobs.state IN ('completed', 'dead')
                    """,
                    (row["event_id"], row["revision"], now_ms, now_ms, now_ms),
                )
                requeued += cursor.rowcount
        return requeued

    def renew_job(
        self,
        job_id: int,
        worker_id: str,
        attempts: int,
        lease_ms: int,
    ) -> None:
        """仅允许当前未过期 attempt 延长自己的 lease。"""
        with self._store.transaction(immediate=True) as connection:
            now_ms = _now_ms()
            cursor = connection.execute(
                """
                UPDATE durable_jobs
                SET lease_expires_ms = ?, updated_ms = ?
                WHERE job_id = ? AND state = 'processing' AND lease_owner = ?
                    AND attempts = ? AND lease_expires_ms > ?
                """,
                (
                    now_ms + max(lease_ms, 0),
                    now_ms,
                    job_id,
                    worker_id,
                    attempts,
                    now_ms,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"lost lease for durable job {job_id}")

    def claim_job(
        self,
        kind: JobKind,
        worker_id: str,
        lease_ms: int,
    ) -> DurableJob | None:
        """以短租约独占领取一条待处理或已过期的工作。"""
        with self._store.transaction(immediate=True) as connection:
            now_ms = _now_ms()
            row = connection.execute(
                """
                SELECT * FROM durable_jobs
                WHERE kind = ? AND (
                    (state = 'pending' AND available_at_ms <= ?)
                    OR (state = 'processing' AND lease_expires_ms <= ?)
                )
                ORDER BY job_id
                LIMIT 1
                """,
                (kind.value, now_ms, now_ms),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE durable_jobs
                SET state = 'processing', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_ms = ?, updated_ms = ?
                WHERE job_id = ?
                """,
                (worker_id, now_ms + max(lease_ms, 0), now_ms, row["job_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM durable_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
        return self._job_from_row(claimed)

    def complete_job(self, job_id: int, worker_id: str, attempts: int) -> None:
        """仅允许持有未过期 lease 的 worker 标记工作完成。"""
        with self._store.transaction(immediate=True) as connection:
            now_ms = _now_ms()
            cursor = connection.execute(
                """
                UPDATE durable_jobs
                SET state = 'completed', lease_owner = NULL, lease_expires_ms = NULL,
                    updated_ms = ?
                WHERE job_id = ? AND state = 'processing' AND lease_owner = ?
                    AND attempts = ? AND lease_expires_ms > ?
                """,
                (now_ms, job_id, worker_id, attempts, now_ms),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"lost lease for durable job {job_id}")

    def fail_job(
        self,
        job_id: int,
        worker_id: str,
        attempts: int,
        error: str,
        max_retries: int,
        retry_delay_ms: int,
    ) -> JobState:
        """仅允许持有未过期 lease 的 worker 记录失败或重试。"""
        with self._store.transaction(immediate=True) as connection:
            return self._fail_job_in_transaction(
                connection, job_id, worker_id, attempts, error,
                max_retries, retry_delay_ms,
            )

    @staticmethod
    def _fail_job_in_transaction(
        connection: Any,
        job_id: int,
        worker_id: str,
        attempts: int,
        error: str,
        max_retries: int,
        retry_delay_ms: int,
    ) -> JobState:
        now_ms = _now_ms()
        row = connection.execute(
            "SELECT attempts FROM durable_jobs WHERE job_id = ? AND state = 'processing' "
            "AND lease_owner = ? AND attempts = ? AND lease_expires_ms > ?",
            (job_id, worker_id, attempts, now_ms),
        ).fetchone()
        if row is None:
            raise LeaseLostError(f"lost lease for durable job {job_id}")
        next_state = JobState.DEAD if row["attempts"] >= max_retries else JobState.PENDING
        cursor = connection.execute(
            """UPDATE durable_jobs SET state = ?, lease_owner = NULL, lease_expires_ms = NULL,
                available_at_ms = ?, last_error = ?, updated_ms = ?
                WHERE job_id = ? AND state = 'processing' AND lease_owner = ?
                AND attempts = ? AND lease_expires_ms > ?""",
            (next_state.value, now_ms + max(retry_delay_ms, 0), error, now_ms,
             job_id, worker_id, attempts, now_ms),
        )
        if cursor.rowcount != 1:
            raise LeaseLostError(f"lost lease for durable job {job_id}")
        return next_state

    def _append_review_in_transaction(
        self,
        connection: Any,
        event_id: str,
        event: Any,
        result: Any,
        result_path: str,
        now_ms: int,
    ) -> int:
        evidence_ids = tuple(getattr(result, "evidence_ids", ()))
        claims = [
            claim.model_dump(mode="json") if hasattr(claim, "model_dump") else claim
            for claim in getattr(result, "claims", ())
        ]
        self._validate_evidence_references(connection, event_id, result, evidence_ids)
        revision = event["revision"] + 1
        policy_id = getattr(result, "policy_id", None)
        model_id = getattr(result, "model_id", None)
        connection.execute(
            """
            INSERT INTO reviews (
                review_id, event_id, revision, policy_id, model_id, verdict,
                confidence, evidence_status, explanation, evidence_ids_json,
                claims_json, result_path, created_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                event_id,
                revision,
                policy_id,
                model_id,
                result.verdict,
                result.confidence,
                result.evidence_status,
                result.explanation,
                json.dumps(evidence_ids, ensure_ascii=False),
                json.dumps(claims, ensure_ascii=False),
                result_path,
                now_ms,
            ),
        )
        connection.execute(
            """
            INSERT INTO review_results (
                event_id, verdict, confidence, evidence_status, explanation,
                evidence_ids_json, claims_json, policy_id, model_id, parsed_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                verdict = excluded.verdict,
                confidence = excluded.confidence,
                evidence_status = excluded.evidence_status,
                explanation = excluded.explanation,
                evidence_ids_json = excluded.evidence_ids_json,
                claims_json = excluded.claims_json,
                policy_id = excluded.policy_id,
                model_id = excluded.model_id,
                parsed_at_ms = excluded.parsed_at_ms
            """,
            (
                event_id,
                result.verdict,
                result.confidence,
                result.evidence_status,
                result.explanation,
                json.dumps(evidence_ids, ensure_ascii=False),
                json.dumps(claims, ensure_ascii=False),
                policy_id,
                model_id,
                now_ms,
            ),
        )
        connection.execute(
            """
            UPDATE events
            SET revision = ?, status = 'completed', verdict = ?, confidence = ?,
                result_path = ?
            WHERE event_id = ?
            """,
            (revision, result.verdict, result.confidence, result_path, event_id),
        )
        connection.execute(
            """
            INSERT INTO durable_jobs (
                kind, entity_id, entity_revision, state, attempts,
                available_at_ms, created_ms, updated_ms
            ) VALUES ('index', ?, ?, 'pending', 0, ?, ?, ?)
            ON CONFLICT(kind, entity_id, entity_revision) DO NOTHING
            """,
            (event_id, revision, now_ms, now_ms, now_ms),
        )
        return revision

    @staticmethod
    def _validate_review_lease(
        row: Any,
        job: DurableJob,
        worker_id: str,
        now_ms: int,
    ) -> None:
        if row is None:
            raise LeaseLostError(f"lost lease for durable job {job.job_id}")
        if (
            row["kind"] != JobKind.REVIEW.value
            or row["entity_id"] != job.entity_id
            or row["entity_revision"] != job.entity_revision
            or row["state"] != JobState.PROCESSING.value
            or row["lease_owner"] != worker_id
            or row["attempts"] != job.attempts
            or row["lease_expires_ms"] is None
            or row["lease_expires_ms"] <= now_ms
        ):
            raise LeaseLostError(f"lost lease for durable job {job.job_id}")

    @staticmethod
    def _validate_extract_lease(row: Any, job: DurableJob, worker_id: str, now_ms: int) -> None:
        if row is None or (
            row["kind"] != JobKind.EVIDENCE_EXTRACT.value
            or row["entity_id"] != job.entity_id
            or row["entity_revision"] != job.entity_revision
            or row["state"] != JobState.PROCESSING.value
            or row["lease_owner"] != worker_id
            or row["attempts"] != job.attempts
            or row["lease_expires_ms"] is None
            or row["lease_expires_ms"] <= now_ms
        ):
            raise LeaseLostError(f"lost lease for durable job {job.job_id}")
    @staticmethod
    def _complete_owned_job(
        connection: Any,
        job: DurableJob,
        worker_id: str,
    ) -> None:
        EventLedger._complete_owned_job_kind(connection, job, worker_id, JobKind.REVIEW)

    @staticmethod
    def _complete_owned_job_kind(
        connection: Any,
        job: DurableJob,
        worker_id: str,
        kind: JobKind,
    ) -> None:
        now_ms = _now_ms()
        cursor = connection.execute(
            """
            UPDATE durable_jobs
            SET state = 'completed', lease_owner = NULL, lease_expires_ms = NULL,
                updated_ms = ?
            WHERE job_id = ? AND kind = ? AND entity_id = ? AND entity_revision = ?
                AND state = 'processing' AND lease_owner = ? AND attempts = ?
                AND lease_expires_ms > ?
            """,
            (
                now_ms,
                job.job_id,
                kind.value,
                job.entity_id,
                job.entity_revision,
                worker_id,
                job.attempts,
                now_ms,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseLostError(f"lost lease for durable job {job.job_id}")

    def _artifact_metadata_matches(self, resolved: Path, artifact: "RecordingEvidenceArtifact") -> bool:
        try:
            info = resolved.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return False
            expected_mime = "video/mp4" if artifact.kind == "clip" else "image/jpeg"
            if artifact.mime_type != expected_mime:
                return False
            if info.st_size != artifact.size or info.st_mtime != artifact.mtime:
                return False
            if hashlib.sha256(resolved.read_bytes()).hexdigest() != artifact.sha256:
                return False
        except OSError:
            return False
        return True

    def _record_detections(
        self,
        connection: Any,
        event_id: str,
        context: ReviewContext,
    ) -> None:
        rows = [
            (
                event_id,
                detection.class_name,
                detection.class_id,
                detection.confidence,
                json.dumps(detection.bbox, ensure_ascii=False),
                detection.track_id,
                str(detection.track_state) if detection.track_state is not None else None,
                int(detection.occluded),
            )
            for detection in context.detections
        ]
        if rows:
            connection.executemany(
                """
                INSERT OR IGNORE INTO detections (
                    event_id, class_name, class_id, confidence, bbox_json,
                    track_id, track_state, occluded
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    @staticmethod
    def _validate_evidence_references(
        connection: Any,
        event_id: str,
        result: Any,
        evidence_ids: tuple[str, ...],
    ) -> None:
        known = {
            row["evidence_id"]
            for row in connection.execute(
                "SELECT evidence_id FROM evidence WHERE event_id = ? AND available = 1",
                (event_id,),
            )
        }
        claim_evidence_ids = {
            evidence_id
            for claim in getattr(result, "claims", ())
            for evidence_id in getattr(claim, "evidence_ids", ())
        }
        if any(evidence_id not in known for evidence_id in (*evidence_ids, *claim_evidence_ids)):
            raise ValueError("review references unavailable evidence")
        if result.verdict != "uncertain" and (
            result.evidence_status != "available" or not evidence_ids
        ):
            raise ValueError("non-uncertain review requires available evidence")

    def _refresh_evidence_in_transaction(
        self,
        connection: Any,
        event_id: str,
        evidence_id: str | None = None,
    ) -> int:
        query = "SELECT id, path FROM evidence WHERE event_id = ?"
        params: list[str] = [event_id]
        if evidence_id is not None:
            query += " AND evidence_id = ?"
            params.append(evidence_id)
        rows = connection.execute(query, params).fetchall()
        for row in rows:
            available, size, mtime = self._evidence_metadata(row["path"])
            connection.execute(
                """
                UPDATE evidence SET available = ?, size = ?, mtime = ?
                WHERE id = ?
                """,
                (int(available), size, mtime, row["id"]),
            )
        return len(rows)

    def _evidence_metadata(self, path: str) -> tuple[bool, int | None, float | None]:
        resolved = self.resolve_evidence_path(path)
        if resolved is None:
            return False, None, None
        try:
            metadata = resolved.stat()
        except OSError:
            return False, None, None
        if not stat.S_ISREG(metadata.st_mode):
            return False, None, None
        return True, metadata.st_size, metadata.st_mtime

    def _record_evidence(
        self,
        connection: Any,
        event_id: str,
        context: ReviewContext,
    ) -> None:
        for kind, path in (("frame", context.frame_path), ("clip", context.clip_path)):
            if not path:
                continue
            resolved = self.resolve_evidence_path(path)
            if resolved is None:
                continue
            canonical_path = str(resolved)
            available, size, mtime = self._evidence_metadata(canonical_path)
            connection.execute(
                """
                INSERT OR IGNORE INTO evidence (
                    event_id, evidence_id, kind, path, mime_type, available, size, mtime,
                    source_pts_start, source_pts_end, stream_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    _evidence_id(event_id, kind, canonical_path),
                    kind,
                    canonical_path,
                    mimetypes.guess_type(canonical_path)[0],
                    int(available),
                    size,
                    mtime,
                    getattr(context, "source_pts", None),
                    getattr(context, "source_pts", None),
                    getattr(context, "stream_generation", None),
                ),
            )

    def _jobs_for_event(self, event_id: str) -> tuple[DurableJob, ...]:
        rows = self._store._conn.execute(
            "SELECT * FROM durable_jobs WHERE entity_id = ? ORDER BY job_id", (event_id,)
        ).fetchall()
        return tuple(self._job_from_row(row) for row in rows)

    @staticmethod
    def _job_from_row(row: Any) -> DurableJob:
        return DurableJob(
            job_id=row["job_id"],
            kind=JobKind(row["kind"]),
            entity_id=row["entity_id"],
            entity_revision=row["entity_revision"],
            state=JobState(row["state"]),
            attempts=row["attempts"],
            lease_owner=row["lease_owner"],
            lease_expires_ms=row["lease_expires_ms"],
            available_at_ms=row["available_at_ms"],
            last_error=row["last_error"],
        )
