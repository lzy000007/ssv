"""账本持久任务的异步 worker。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

import structlog

from ssv_agent.event_store import DurableJob, EventLedger, JobKind, LeaseLostError
from ssv_agent.event_store.qdrant_store import SsvQdrantStore
from ssv_agent.embedding.provider import EmbeddingProvider
from ssv_agent.evidence_provider import (
    EvidenceCacheWindowUnavailable,
    EvidenceExtractionError,
    EvidenceExtractor,
)
from ssv_agent.review_context import ReviewContext
from ssv_agent.review_context import RuleRetrievalContext
from ssv_agent.report import (
    REPORT_TEMPLATE_VERSION,
    render_analysis_report,
    write_analysis_report,
)
from ssv_agent.result import (
    ResultParseError,
    ReviewResult,
    parse_review_result,
    validate_rule_citations,
    write_result_json,
)
from ssv_agent.runner import ModelCallError, classify_model_error
from ssv_agent.search.rule_query import build_rule_filters, build_rule_query
from ssv_agent.knowledge import Retriever
from ssv_agent.knowledge.schema import RetrievalResult
from ssv_agent.search.event_text import build_event_text

logger = structlog.get_logger()

LedgerFactory = Callable[[], EventLedger]
ReviewRunner = Callable[[ReviewContext], str]
ResultWriter = Callable[[str, ReviewResult], Path]
QdrantFactory = Callable[[], SsvQdrantStore]


def _retrieval_chunk_observation(chunk: object) -> dict[str, object]:
    metadata = getattr(chunk, "metadata", {}) or {}
    return {
        "chunk_id": str(getattr(chunk, "chunk_id", "")),
        "score": getattr(chunk, "score", None),
        "source": str(metadata.get("source", "")),
        "rule_id": str(metadata.get("rule_id", "")),
        "rule_version": str(metadata.get("rule_version", "")),
        "section": str(metadata.get("section", "")),
    }


def _retrieval_failure_code(result: RetrievalResult) -> str | None:
    if result.success:
        return None
    message = (result.error_message or "").lower()
    if "rule vector index does not exist" in message:
        return "rule_index_missing"
    if "rule vector index is empty" in message:
        return "rule_index_empty"
    return "retrieval_failed"


def _log_rule_retrieval(
    *,
    event_id: str,
    job_id: int,
    entity_revision: int,
    query: str,
    result: RetrievalResult,
    started_at: float,
    error_type: str | None = None,
) -> None:
    logger.info(
        "rule retrieval observed",
        event_id=event_id,
        job_id=job_id,
        entity_revision=entity_revision,
        query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
        query_length=len(query),
        backend=result.backend,
        success=result.success,
        available=result.success and bool(result.chunks),
        chunk_count=len(result.chunks),
        chunks=[_retrieval_chunk_observation(chunk) for chunk in result.chunks],
        retrieval_ms=max(0, int((monotonic() - started_at) * 1000)),
        error_type=error_type,
        failure_code=_retrieval_failure_code(result),
    )


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


def _evidence_failure_reason(exc: Exception) -> str:
    """取证实现的受控失败可保留原因，其他异常仅持久化类别。"""
    if isinstance(exc, EvidenceExtractionError):
        return str(exc)
    return type(exc).__name__


def _failure_details(exc: Exception) -> tuple[str, str]:
    """返回稳定错误码和脱敏用户消息，原始诊断不进入 Web 字段。"""
    if isinstance(exc, ModelCallError):
        return exc.code, exc.user_message
    if isinstance(exc, ResultParseError):
        return "review_result_invalid", "模型返回的复核结果无法通过格式校验。"
    return "job_failed", "持久任务执行失败，请稍后重试。"


def _failure_diagnostic(exc: Exception) -> str:
    """保留内部诊断；用户可见字段由 ``_failure_details`` 控制。"""
    if isinstance(exc, ModelCallError):
        return exc.diagnostic
    return str(exc)


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
            job,
            worker_id,
            error,
            max_retries,
            retry_delay_ms=0,
            error_code=job.error_code or "retry_limit_exceeded",
            error_message=job.error_message or "任务重试次数已用尽，请检查日志。",
        )
    else:
        state = ledger.fail_job(
            job.job_id,
            worker_id,
            job.attempts,
            error,
            max_retries,
            retry_delay_ms=0,
            error_code=job.error_code or "retry_limit_exceeded",
            error_message=job.error_message or "任务重试次数已用尽，请检查日志。",
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


class _PollingWorker:
    """承载持久 Worker 共用的停止、空闲等待和异常恢复骨架。"""

    def __init__(
        self,
        *,
        worker_id: str,
        poll_interval_seconds: float,
        job_kind: JobKind,
    ) -> None:
        self._worker_id = worker_id
        self._poll_interval_seconds = poll_interval_seconds
        self._job_kind = job_kind

    def run_once(self) -> bool:
        """处理一条任务；具体 Worker 负责实现领域流程。"""
        raise NotImplementedError

    def run(self, stopping: Event) -> None:
        """循环领取任务；停止后不再领取新任务。"""
        while not stopping.is_set():
            try:
                processed = self.run_once()
            except Exception as exc:
                logger.warning(
                    "durable worker run_once failed",
                    worker_id=self._worker_id,
                    job_kind=self._job_kind.value,
                    error=self._poll_error(exc),
                )
                stopping.wait(self._poll_interval_seconds)
                continue
            if not processed:
                stopping.wait(self._poll_interval_seconds)

    def _poll_error(self, exc: Exception) -> str:
        return type(exc).__name__


class ReviewWorker(_PollingWorker):
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
        super().__init__(
            worker_id=worker_id,
            poll_interval_seconds=poll_interval_seconds,
            job_kind=JobKind.REVIEW,
        )
        self._ledger_factory = ledger_factory
        self._runner = runner
        self._lease_ms = lease_ms
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms
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
                    filters = build_rule_filters(case)
                    retrieval_started = monotonic()
                    retrieval_error_type: str | None = None
                    try:
                        retrieval = self._rule_retriever.retrieve(
                            query,
                            top_k=5,
                            filters=filters or None,
                        )
                        if inspect.isawaitable(retrieval):
                            retrieval = asyncio.run(retrieval)
                    except Exception as exc:
                        retrieval_error_type = type(exc).__name__
                        retrieval = RetrievalResult(
                            query=query,
                            backend=getattr(self._rule_retriever, "backend_name", "unknown"),
                            success=False,
                            error_message=retrieval_error_type,
                        )
                    _log_rule_retrieval(
                        event_id=case.event_id,
                        job_id=job.job_id,
                        entity_revision=job.entity_revision,
                        query=query,
                        result=retrieval,
                        started_at=retrieval_started,
                        error_type=retrieval_error_type,
                    )
                    rule_context = RuleRetrievalContext.from_result(query, retrieval)
                try:
                    try:
                        text = self._runner(review_context, rule_context)
                    except TypeError:
                        text = self._runner(review_context)
                except ModelCallError:
                    raise
                except Exception as exc:
                    raise classify_model_error(exc) from exc
                if not isinstance(text, str) or not text.strip():
                    raise ModelCallError(
                        "model_no_response",
                        "模型服务未返回可解析结果。",
                    )
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
                error_code, error_message = _failure_details(exc)
                try:
                    state = ledger.fail_job(
                        job.job_id,
                        self._worker_id,
                        job.attempts,
                        _failure_diagnostic(exc),
                        self._max_retries,
                        self._retry_delay_ms,
                        error_code=error_code,
                        error_message=error_message,
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
                        error_type=type(exc).__name__,
                    )
            finally:
                _stop_heartbeat(heartbeat)
            return True

    def _add_provenance(self, result: ReviewResult) -> ReviewResult:
        updates: dict[str, str] = {}
        if self._policy_id and result.policy_id is None:
            updates["policy_id"] = self._policy_id
        if self._model_id and result.model_id is None:
            updates["model_id"] = self._model_id
        return result.model_copy(update=updates) if updates else result

class RecordingEvidenceWorker(_PollingWorker):
    """领取录像取证任务，并在账本事务中登记完整证据集。"""

    def __init__(
        self,
        *,
        ledger_factory: LedgerFactory,
        extractor: EvidenceExtractor,
        worker_id: str,
        lease_ms: int,
        max_retries: int,
        retry_delay_ms: int,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        super().__init__(
            worker_id=worker_id,
            poll_interval_seconds=poll_interval_seconds,
            job_kind=JobKind.EVIDENCE_EXTRACT,
        )
        self._ledger_factory = ledger_factory
        self._extractor = extractor
        self._lease_ms = lease_ms
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms

    def run_once(self) -> bool:
        """最多处理一条取证任务；无可领任务时返回 ``False``。"""
        with self._ledger_factory() as ledger:
            job = ledger.claim_job(JobKind.EVIDENCE_EXTRACT, self._worker_id, self._lease_ms)
            if job is None:
                return False
            heartbeat: _LeaseHeartbeat | None = None
            try:
                if job.attempts > self._max_retries:
                    recover = getattr(self._extractor, "existing_anchor_frame", None)
                    if callable(recover):
                        case = ledger.get_case(job.entity_id)
                        if case is not None:
                            existing = recover(case)
                            if existing is not None:
                                ledger.complete_partial_evidence_extract_job(
                                    job, self._worker_id, existing,
                                )
                                return True
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
                try:
                    artifacts = self._extractor.extract(case)
                except EvidenceCacheWindowUnavailable:
                    anchor = getattr(self._extractor, "extract_anchor_frame", None)
                    if not callable(anchor) or job.attempts < self._max_retries:
                        raise
                    artifacts = anchor(case)
                heartbeat.checkpoint()
                heartbeat.stop_and_join()
                heartbeat.checkpoint()
                if len(artifacts) == 1 and artifacts[0].coverage == "anchor_only":
                    ledger.complete_partial_evidence_extract_job(
                        job, self._worker_id, artifacts,
                    )
                else:
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
                error = _evidence_failure_reason(exc)
                error_code, error_message = _failure_details(exc)
                try:
                    state = ledger.fail_evidence_extract_job(
                        job,
                        self._worker_id,
                        error,
                        self._max_retries if retryable else job.attempts,
                        self._retry_delay_ms if retryable else 0,
                        error_code=error_code,
                        error_message=error_message,
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
                        error_type=type(exc).__name__,
                    )
            finally:
                _stop_heartbeat(heartbeat)
            return True

    def _poll_error(self, exc: Exception) -> str:
        return type(exc).__name__


class IndexWorker(_PollingWorker):
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
        super().__init__(
            worker_id=worker_id,
            poll_interval_seconds=poll_interval_seconds,
            job_kind=JobKind.INDEX,
        )
        self._ledger_factory = ledger_factory
        self._embedding = embedding
        self._qdrant_factory = qdrant_factory
        self._lease_ms = lease_ms
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms

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
                error_code, error_message = _failure_details(exc)
                try:
                    state = ledger.fail_job(
                        job.job_id,
                        self._worker_id,
                        job.attempts,
                        _failure_diagnostic(exc),
                        self._max_retries,
                        self._retry_delay_ms,
                        error_code=error_code,
                        error_message=error_message,
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
                        error_type=type(exc).__name__,
                    )
            finally:
                _stop_heartbeat(heartbeat)
            return True

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


class ReportWorker(_PollingWorker):
    """将指定历史复核版本渲染为确定性 Markdown 报告。"""

    def __init__(
        self,
        *,
        ledger_factory: LedgerFactory,
        worker_id: str,
        lease_ms: int,
        max_retries: int,
        retry_delay_ms: int,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        super().__init__(
            worker_id=worker_id,
            poll_interval_seconds=poll_interval_seconds,
            job_kind=JobKind.REPORT,
        )
        self._ledger_factory = ledger_factory
        self._lease_ms = lease_ms
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms

    def run_once(self) -> bool:
        """领取并提交一份报告；报告失败不修改已提交的复核事实。"""
        with self._ledger_factory() as ledger:
            job = ledger.claim_job(JobKind.REPORT, self._worker_id, self._lease_ms)
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
                    raise KeyError(
                        f"report job references missing event: {job.entity_id}"
                    )
                review = ledger.get_review_record(job.entity_id, job.entity_revision)
                if review is None:
                    raise KeyError(
                        "report job references missing review: "
                        f"{job.entity_id}@{job.entity_revision}"
                    )

                heartbeat = _LeaseHeartbeat(
                    ledger_factory=self._ledger_factory,
                    job=job,
                    worker_id=self._worker_id,
                    lease_ms=self._lease_ms,
                )
                heartbeat.start()
                heartbeat.checkpoint()
                markdown = render_analysis_report(case, review)
                artifact = write_analysis_report(job.entity_id, review.review_id, markdown)
                heartbeat.checkpoint()
                heartbeat.stop_and_join()
                heartbeat.checkpoint()
                ledger.complete_report_job(
                    job,
                    self._worker_id,
                    artifact,
                    REPORT_TEMPLATE_VERSION,
                )
                logger.info(
                    "report job completed",
                    event_id=job.entity_id,
                    review_id=review.review_id,
                    revision=job.entity_revision,
                    job_id=job.job_id,
                    artifact_path=str(artifact.path),
                    sha256=artifact.sha256,
                )
            except LeaseLostError:
                _stop_heartbeat(heartbeat)
                logger.info(
                    "report job lease lost",
                    event_id=job.entity_id,
                    job_id=job.job_id,
                    worker_id=self._worker_id,
                )
            except Exception as exc:
                _stop_heartbeat(heartbeat)
                error_code, error_message = _failure_details(exc)
                try:
                    state = ledger.fail_job(
                        job.job_id,
                        self._worker_id,
                        job.attempts,
                        _failure_diagnostic(exc),
                        self._max_retries,
                        self._retry_delay_ms,
                        error_code=error_code,
                        error_message=error_message,
                    )
                except LeaseLostError:
                    logger.info(
                        "report job lease lost",
                        event_id=job.entity_id,
                        job_id=job.job_id,
                        worker_id=self._worker_id,
                    )
                else:
                    logger.warning(
                        "report job failed",
                        event_id=job.entity_id,
                        job_id=job.job_id,
                        state=state.value,
                        error_type=type(exc).__name__,
                    )
            finally:
                _stop_heartbeat(heartbeat)
            return True
