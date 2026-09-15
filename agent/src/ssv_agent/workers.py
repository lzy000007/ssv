"""账本持久任务的异步 worker。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Protocol

import structlog

from ssv_agent.event_store import DurableJob, EventLedger, JobKind, LeaseLostError
from ssv_agent.event_store.qdrant_store import SsvQdrantStore
from ssv_agent.embedding.provider import EmbeddingProvider
from ssv_agent.review_context import ReviewContext
from ssv_agent.review_context import RuleRetrievalContext
from ssv_agent.result import (
    ReviewResult,
    parse_review_result,
    validate_rule_citations,
    write_result_json,
)
from ssv_agent.search.rule_query import build_rule_query
from ssv_agent.knowledge import Retriever
from ssv_agent.recording_evidence import RecordingEvidenceArtifact, RecordingEvidenceError
from ssv_agent.search.event_text import build_event_text

logger = structlog.get_logger()

LedgerFactory = Callable[[], EventLedger]
ReviewRunner = Callable[[ReviewContext], str]
ResultWriter = Callable[[str, ReviewResult], Path]
QdrantFactory = Callable[[], SsvQdrantStore]
class RecordingExtractor(Protocol):
    def extract(self, case: object) -> tuple[RecordingEvidenceArtifact, ...]: ...


class _HeartbeatError(RuntimeError):
    """后台续租无法确认当前 job 仍健康。"""


class _LeaseHeartbeat:
    """在独立 SQLite connection 上持续续租单个 claim。"""

    def __init__(
        self,
        *,
        ledger_factory: LedgerFactory,
        job: DurableJob,
        worker_id: str,
        lease_ms: int,
    ) -> None:
        self._ledger_factory = ledger_factory
        self._job_id = job.job_id
        self._worker_id = worker_id
        self._attempts = job.attempts
        self._lease_ms = lease_ms
        self._interval_seconds = lease_ms / 3_000
        self._stopping = Event()
        self._lost = Event()
        self._failure: BaseException | None = None
        self._failure_lock = Lock()
        self._started = False
        self._thread = Thread(
            target=self._run,
            name=f"ssv-job-heartbeat-{job.job_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def checkpoint(self) -> None:
        """在外部副作用边界同步确认 lease，避免只依赖异步观察。"""
        self._raise_if_unhealthy()
        try:
            self._renew_with_new_ledger()
        except LeaseLostError:
            self._lost.set()
            raise
        except BaseException as exc:
            self._set_failure(exc)
            raise _HeartbeatError(
                f"lease heartbeat failed for durable job {self._job_id}"
            ) from exc
        self._raise_if_unhealthy()

    def stop_and_join(self) -> None:
        self._stopping.set()
        if self._started:
            self._thread.join()

    def _run(self) -> None:
        while not self._stopping.wait(self._interval_seconds):
            try:
                self._renew_with_new_ledger()
            except LeaseLostError:
                self._lost.set()
                return
            except BaseException as exc:
                self._set_failure(exc)
                return

    def _renew_with_new_ledger(self) -> None:
        # The heartbeat owns this factory-created connection in its calling thread.
        with self._ledger_factory() as ledger:
            ledger.renew_job(
                self._job_id,
                self._worker_id,
                self._attempts,
                self._lease_ms,
            )

    def _raise_if_unhealthy(self) -> None:
        if self._lost.is_set():
            raise LeaseLostError(f"lost lease for durable job {self._job_id}")
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise _HeartbeatError(
                f"lease heartbeat failed for durable job {self._job_id}"
            ) from failure

    def _set_failure(self, exc: BaseException) -> None:
        with self._failure_lock:
            self._failure = exc


def _stop_heartbeat(heartbeat: _LeaseHeartbeat | None) -> None:
    if heartbeat is not None:
        heartbeat.stop_and_join()


def _recording_failure_reason(exc: Exception) -> str:
    """录像提取器的受控失败可保留原因，其他异常仅持久化类别。"""
    if isinstance(exc, RecordingEvidenceError):
        return str(exc)
    return type(exc).__name__


def _mark_exhausted_attempt_dead(
    ledger: EventLedger,
    job: DurableJob,
    worker_id: str,
    max_retries: int,
) -> bool:
    """把已超限的 claim 在任何外部调用前以当前 fence 转为 dead。"""
    if job.attempts <= max_retries:
        return False
    error = f"claim attempt {job.attempts} exceeds max_retries {max_retries}"
    if job.kind == JobKind.EVIDENCE_EXTRACT:
        state = ledger.fail_evidence_extract_job(
            job, worker_id, error, max_retries, retry_delay_ms=0
        )
    else:
        state = ledger.fail_job(
            job.job_id,
            worker_id,
            job.attempts,
            error,
            max_retries,
            retry_delay_ms=0,
        )
    logger.warning(
        "job attempt limit exceeded",
        job_id=job.job_id,
        event_id=job.entity_id,
        state=state.value,
        attempts=job.attempts,
        max_retries=max_retries,
    )
    return True


class ReviewWorker:
    """领取 review job，执行模型，并以账本追加结果为唯一提交点。"""

    def __init__(
        self,
        *,
        ledger_factory: LedgerFactory,
        runner: ReviewRunner,
        worker_id: str,
        lease_ms: int,
        max_retries: int,
        retry_delay_ms: int,
        poll_interval_seconds: float = 1.0,
        policy_id: str | None = None,
        model_id: str | None = None,
        result_writer: ResultWriter = write_result_json,
        rule_retriever: Retriever | None = None,
    ) -> None:
        self._ledger_factory = ledger_factory
        self._runner = runner
        self._worker_id = worker_id
        self._lease_ms = lease_ms
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms
        self._poll_interval_seconds = poll_interval_seconds
        self._policy_id = policy_id
        self._model_id = model_id
        self._result_writer = result_writer
        self._rule_retriever = rule_retriever

    def run_once(self) -> bool:
        """最多处理一条工作；无可领任务时返回 ``False``。"""
        with self._ledger_factory() as ledger:
            job = ledger.claim_job(JobKind.REVIEW, self._worker_id, self._lease_ms)
            if job is None:
                return False
            heartbeat: _LeaseHeartbeat | None = None
            try:
                if _mark_exhausted_attempt_dead(
                    ledger,
                    job,
                    self._worker_id,
                    self._max_retries,
                ):
                    return True
                ledger.refresh_evidence(job.entity_id)
                case = ledger.get_case(job.entity_id)
                if case is None:
                    raise KeyError(f"review job references missing event: {job.entity_id}")
                if case.revision != job.entity_revision:
                    ledger.complete_job(job.job_id, self._worker_id, job.attempts)
                    return True

                heartbeat = _LeaseHeartbeat(
                    ledger_factory=self._ledger_factory,
                    job=job,
                    worker_id=self._worker_id,
                    lease_ms=self._lease_ms,
                )
                heartbeat.start()
                heartbeat.checkpoint()
                review_context = case.to_review_context()
                rule_context = None
                if self._rule_retriever is not None:
                    query = build_rule_query(case)
                    try:
                        retrieval = self._rule_retriever.retrieve(query, top_k=5)
                        if inspect.isawaitable(retrieval):
                            retrieval = asyncio.run(retrieval)
                    except Exception as exc:
                        from ssv_agent.knowledge.schema import RetrievalResult

                        retrieval = RetrievalResult(
                            query=query,
                            backend="",
                            success=False,
                            error_message=type(exc).__name__,
                        )
                    rule_context = RuleRetrievalContext.from_result(query, retrieval)
                try:
                    text = self._runner(review_context, rule_context)
                except TypeError:
                    text = self._runner(review_context)
                heartbeat.checkpoint()
                result = parse_review_result(text)
                if rule_context is not None:
                    result = validate_rule_citations(result, rule_context)
                result = self._add_provenance(result)
                heartbeat.checkpoint()
                result_path = self._result_writer(case.event_id, result)
                heartbeat.checkpoint()
                heartbeat.stop_and_join()
                heartbeat.checkpoint()
                ledger.complete_review_job(
                    job,
                    self._worker_id,
                    result,
                    str(result_path),
                )
                logger.info(
                    "review job completed",
                    event_id=case.event_id,
                    job_id=job.job_id,
                )
            except LeaseLostError:
                _stop_heartbeat(heartbeat)
                logger.info(
                    "review job lease lost",
                    event_id=job.entity_id,
                    job_id=job.job_id,
                    worker_id=self._worker_id,
                )
            except Exception as exc:
                _stop_heartbeat(heartbeat)
                try:
                    state = ledger.fail_job(
                        job.job_id,
                        self._worker_id,
                        job.attempts,
                        str(exc),
                        self._max_retries,
                        self._retry_delay_ms,
                    )
                except LeaseLostError:
                    logger.info(
                        "review job lease lost",
                        event_id=job.entity_id,
                        job_id=job.job_id,
                        worker_id=self._worker_id,
                    )
                else:
                    logger.warning(
                        "review job failed",
                        event_id=job.entity_id,
                        job_id=job.job_id,
                        state=state.value,
                        error=str(exc),
                    )
            finally:
                _stop_heartbeat(heartbeat)
            return True

    def run(self, stopping: Event) -> None:
        """循环领取任务；停止后不再领取新任务。"""
        while not stopping.is_set():
            try:
                processed = self.run_once()
            except Exception as exc:
                logger.warning(
                    "durable worker run_once failed",
                    worker_id=self._worker_id,
                    job_kind=JobKind.REVIEW.value,
                    error=str(exc),
                )
                stopping.wait(self._poll_interval_seconds)
                continue
            if not processed:
                stopping.wait(self._poll_interval_seconds)

    def _add_provenance(self, result: ReviewResult) -> ReviewResult:
        updates: dict[str, str] = {}
        if self._policy_id and result.policy_id is None:
            updates["policy_id"] = self._policy_id
        if self._model_id and result.model_id is None:
            updates["model_id"] = self._model_id
        return result.model_copy(update=updates) if updates else result

class RecordingEvidenceWorker:
    """领取录像取证任务，并在账本事务中登记完整证据集。"""

    def __init__(
        self,
        *,
        ledger_factory: LedgerFactory,
        extractor: RecordingExtractor,
        worker_id: str,
        lease_ms: int,
        max_retries: int,
        retry_delay_ms: int,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._ledger_factory = ledger_factory
        self._extractor = extractor
        self._worker_id = worker_id
        self._lease_ms = lease_ms
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms
        self._poll_interval_seconds = poll_interval_seconds

    def run_once(self) -> bool:
        """最多处理一条取证任务；无可领任务时返回 ``False``。"""
        with self._ledger_factory() as ledger:
            job = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, self._worker_id, self._lease_ms)
            if job is None:
                return False
            heartbeat: _LeaseHeartbeat | None = None
            try:
                if _mark_exhausted_attempt_dead(
                    ledger, job, self._worker_id, self._max_retries
                ):
                    return True
                case = ledger.get_case(job.entity_id)
                if case is None:
                    raise KeyError(f"evidence extract job references missing event: {job.entity_id}")
                heartbeat = _LeaseHeartbeat(
                    ledger_factory=self._ledger_factory,
                    job=job,
                    worker_id=self._worker_id,
                    lease_ms=self._lease_ms,
                )
                heartbeat.start()
                heartbeat.checkpoint()
                artifacts = self._extractor.extract(case)
                heartbeat.checkpoint()
                heartbeat.stop_and_join()
                heartbeat.checkpoint()
                ledger.complete_evidence_extract_job(job, self._worker_id, artifacts)
                logger.info(
                    "recording evidence job completed",
                    event_id=case.event_id,
                    job_id=job.job_id,
                    source=case.source,
                )
            except LeaseLostError:
                _stop_heartbeat(heartbeat)
                logger.info(
                    "recording evidence job lease lost",
                    event_id=job.entity_id,
                    job_id=job.job_id,
                    worker_id=self._worker_id,
                )
            except Exception as exc:
                _stop_heartbeat(heartbeat)
                retryable = getattr(exc, "retryable", True)
                error = _recording_failure_reason(exc)
                try:
                    state = ledger.fail_evidence_extract_job(
                        job,
                        self._worker_id,
                        error,
                        self._max_retries if retryable else job.attempts,
                        self._retry_delay_ms if retryable else 0,
                    )
                except LeaseLostError:
                    logger.info(
                        "recording evidence job lease lost",
                        event_id=job.entity_id,
                        job_id=job.job_id,
                        worker_id=self._worker_id,
                    )
                else:
                    logger.warning(
                        "recording evidence job failed",
                        event_id=job.entity_id,
                        job_id=job.job_id,
                        state=state.value,
                        error=type(exc).__name__,
                    )
            finally:
                _stop_heartbeat(heartbeat)
            return True

    def run(self, stopping: Event) -> None:
        """循环领取取证任务；停止后不再领取新任务。"""
        while not stopping.is_set():
            try:
                processed = self.run_once()
            except Exception as exc:
                logger.warning(
                    "durable worker run_once failed",
                    worker_id=self._worker_id,
                    job_kind=JobKind.EVIDENCE_EXTRACT.value,
                    error=type(exc).__name__,
                )
                stopping.wait(self._poll_interval_seconds)
                continue
            if not processed:
                stopping.wait(self._poll_interval_seconds)


class IndexWorker:
    """将账本当前投影异步写入可重建的 Qdrant 事件索引。"""

    def __init__(
        self,
        *,
        ledger_factory: LedgerFactory,
        embedding: EmbeddingProvider,
        qdrant_factory: QdrantFactory,
        worker_id: str,
        lease_ms: int,
        max_retries: int,
        retry_delay_ms: int,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._ledger_factory = ledger_factory
        self._embedding = embedding
        self._qdrant_factory = qdrant_factory
        self._worker_id = worker_id
        self._lease_ms = lease_ms
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms
        self._poll_interval_seconds = poll_interval_seconds

    def run_once(self) -> bool:
        """最多索引一件案件；旧 revision 始终写入当前 SQLite 投影。"""
        with self._ledger_factory() as ledger:
            job = ledger.claim_job(JobKind.INDEX, self._worker_id, self._lease_ms)
            if job is None:
                return False
            heartbeat: _LeaseHeartbeat | None = None
            try:
                if _mark_exhausted_attempt_dead(
                    ledger,
                    job,
                    self._worker_id,
                    self._max_retries,
                ):
                    return True
                case = ledger.get_case(job.entity_id)
                if case is None:
                    raise KeyError(f"index job references missing event: {job.entity_id}")
                heartbeat = _LeaseHeartbeat(
                    ledger_factory=self._ledger_factory,
                    job=job,
                    worker_id=self._worker_id,
                    lease_ms=self._lease_ms,
                )
                heartbeat.start()
                heartbeat.checkpoint()
                vector = asyncio.run(self._embedding.embed_texts([build_event_text(case)]))[0]
                heartbeat.checkpoint()
                with self._qdrant_factory() as qdrant:
                    qdrant.upsert_event_vector(case.event_id, vector, self._payload(case))
                heartbeat.checkpoint()
                heartbeat.stop_and_join()
                heartbeat.checkpoint()
                ledger.complete_job(job.job_id, self._worker_id, job.attempts)
                logger.info(
                    "index job completed",
                    event_id=case.event_id,
                    revision=case.revision,
                    job_id=job.job_id,
                )
            except LeaseLostError:
                _stop_heartbeat(heartbeat)
                logger.info(
                    "index job lease lost",
                    event_id=job.entity_id,
                    job_id=job.job_id,
                    worker_id=self._worker_id,
                )
            except Exception as exc:
                _stop_heartbeat(heartbeat)
                try:
                    state = ledger.fail_job(
                        job.job_id,
                        self._worker_id,
                        job.attempts,
                        str(exc),
                        self._max_retries,
                        self._retry_delay_ms,
                    )
                except LeaseLostError:
                    logger.info(
                        "index job lease lost",
                        event_id=job.entity_id,
                        job_id=job.job_id,
                        worker_id=self._worker_id,
                    )
                else:
                    logger.warning(
                        "index job failed",
                        event_id=job.entity_id,
                        job_id=job.job_id,
                        state=state.value,
                        error=str(exc),
                    )
            finally:
                _stop_heartbeat(heartbeat)
            return True

    def run(self, stopping: Event) -> None:
        """循环领取 index job，停止后不再发起新的 Qdrant 写入。"""
        while not stopping.is_set():
            try:
                processed = self.run_once()
            except Exception as exc:
                logger.warning(
                    "durable worker run_once failed",
                    worker_id=self._worker_id,
                    job_kind=JobKind.INDEX.value,
                    error=str(exc),
                )
                stopping.wait(self._poll_interval_seconds)
                continue
            if not processed:
                stopping.wait(self._poll_interval_seconds)

    @staticmethod
    def _payload(case: object) -> dict[str, object]:
        review = getattr(case, "review") or {}
        return {
            "event_id": getattr(case, "event_id"),
            "revision": getattr(case, "revision"),
            "source": getattr(case, "source"),
            "timestamp_ms": getattr(case, "timestamp_ms"),
            "event_type": getattr(case, "event_type"),
            "rule_id": getattr(case, "rule_id"),
            "severity": getattr(case, "severity"),
            "verdict": review.get("verdict") or getattr(case, "verdict"),
            "confidence": review.get("confidence", getattr(case, "confidence")),
            "evidence_status": review.get("evidence_status"),
        }
