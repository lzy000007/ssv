from __future__ import annotations

import json
import os
import signal
import time
import uuid
from collections.abc import Callable
from functools import partial
from threading import Event

import structlog
from redis import Redis
from redis.exceptions import ResponseError

from ssv_agent.config import SsvConfig
from ssv_agent.dedup import DedupDecision, EventDeduper
from ssv_agent.event_episode import EventEpisodeAggregator
from ssv_agent.event_store import EventLedger
from ssv_agent.review_context import ReviewContext

logger = structlog.get_logger()


class EventConsumer:
    """将 Redis 事件提交到权威账本后立即 ACK。"""

    def __init__(
        self,
        config: SsvConfig,
        ledger_factory: Callable[[], EventLedger] | None = None,
        deduper: EventDeduper | None = None,
        episode_aggregator: EventEpisodeAggregator | None = None,
    ) -> None:
        self._stream = config.redis.stream_key
        self._group = config.redis.consumer_group
        self._reclaim_idle_ms = config.redis.reclaim_idle_ms
        self._reclaim_batch_size = config.redis.reclaim_batch_size
        self._consumer_name = config.redis.consumer_name or self._new_consumer_name()
        self._claim_cursor = "0-0"
        self._stop_requested = Event()
        self._ledger_factory = ledger_factory or partial(
            EventLedger,
            evidence_roots=config.agent.evidence_roots,
            recording_evidence=config.agent.recording_evidence,
        )
        self._redis = Redis(
            host=config.redis.host,
            port=config.redis.port,
            db=config.redis.db,
            decode_responses=True,
        )
        self._deduper: EventDeduper | None
        if deduper is not None:
            self._deduper = deduper
        elif config.agent.dedup_enabled:
            self._deduper = EventDeduper(
                self._redis,
                cooldown_seconds=config.agent.dedup_cooldown_seconds,
            )
        else:
            self._deduper = None
        self._episode_aggregator = (
            episode_aggregator
            if episode_aggregator is not None
            else (
                EventEpisodeAggregator(config.agent.recording_evidence)
                if ledger_factory is None
                else None
            )
        )

    def _ensure_group(self) -> None:
        """Create the consumer group if it does not exist."""
        try:
            self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
            logger.info("created consumer group", group=self._group, stream=self._stream)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            logger.info(
                "consumer group already exists",
                group=self._group,
                stream=self._stream,
            )

    def start(self) -> None:
        """Blocking consumer loop.  Returns on SIGINT/SIGTERM."""
        if self._stop_requested.is_set():
            logger.info("event consumer start skipped after stop request")
            return
        self._ensure_group()
        if self._stop_requested.is_set():
            return

        logger.info(
            "event consumer started",
            stream=self._stream,
            group=self._group,
            consumer=self._consumer_name,
        )

        while not self._stop_requested.is_set():
            try:
                reclaimed = self._reclaim_pending()
            except Exception as exc:
                logger.warning(
                    "redis pending reclaim error",
                    error_type=type(exc).__name__,
                )
            else:
                self._handle_messages(reclaimed)
            self._flush_episode_timeouts()

            if self._stop_requested.is_set():
                break
            try:
                entries = self._redis.xreadgroup(
                    self._group,
                    self._consumer_name,
                    {self._stream: ">"},
                    count=10,
                    block=1000,  # 1 s
                )
            except Exception as exc:
                logger.warning("redis read error", error_type=type(exc).__name__)
                time.sleep(2)
                continue

            for _stream_name, messages in entries:
                self._handle_messages(messages)
            self._flush_episode_timeouts()

    def stop(self) -> None:
        self._stop_requested.set()

    @staticmethod
    def _new_consumer_name() -> str:
        return f"ssv-agent-{os.getpid()}-{uuid.uuid4().hex[:12]}"

    def _reclaim_pending(self) -> list[tuple[str, dict[str, str]]]:
        """通过 XAUTOCLAIM 收回超时 pending，再交给正常处理 seam。"""
        xautoclaim = getattr(self._redis, "xautoclaim", None)
        if not callable(xautoclaim):
            raise RuntimeError("Redis client does not support XAUTOCLAIM")
        response = xautoclaim(
            self._stream,
            self._group,
            self._consumer_name,
            self._reclaim_idle_ms,
            start_id=self._claim_cursor,
            count=self._reclaim_batch_size,
        )
        next_cursor, messages = self._parse_autoclaim_response(response)
        self._claim_cursor = next_cursor
        return messages

    @staticmethod
    def _parse_autoclaim_response(
        response: object,
    ) -> tuple[str, list[tuple[str, dict[str, str]]]]:
        """兼容 redis-py 5--7 的 tuple/list 和命名 XAUTOCLAIM 返回形态。"""
        next_cursor: object = "0-0"
        messages: object = []
        if isinstance(response, dict):
            next_cursor = response.get("next_start_id", response.get("next_id", "0-0"))
            messages = response.get("messages", response.get("entries", []))
        elif hasattr(response, "messages"):
            next_cursor = getattr(
                response,
                "next_start_id",
                getattr(response, "next_id", "0-0"),
            )
            messages = getattr(response, "messages")
        elif isinstance(response, (list, tuple)) and len(response) >= 2:
            next_cursor, messages = response[0], response[1]
        else:
            raise ValueError("unexpected XAUTOCLAIM response")

        if isinstance(next_cursor, bytes):
            next_cursor = next_cursor.decode("utf-8")
        if isinstance(messages, dict):
            iterable: object = messages.items()
        else:
            iterable = messages or []
        parsed: list[tuple[str, dict[str, str]]] = []
        for message in iterable:
            try:
                msg_id, fields = message
            except (TypeError, ValueError) as exc:
                raise ValueError("unexpected XAUTOCLAIM message") from exc
            if isinstance(msg_id, bytes):
                msg_id = msg_id.decode("utf-8")
            if not isinstance(fields, dict):
                raise ValueError("unexpected XAUTOCLAIM fields")
            parsed.append((str(msg_id), fields))
        return str(next_cursor), parsed

    def _handle_messages(self, messages: object) -> None:
        for msg_id, fields in messages:
            if self._stop_requested.is_set():
                return
            self.handle_event(msg_id, fields)

    def close(self) -> None:
        """关闭 Redis client；由 AgentService 在消费线程退出后拥有。"""
        close = getattr(self._redis, "close", None)
        if callable(close):
            close()

    def handle_event(self, msg_id: str, fields: dict[str, str]) -> None:
        """处理一条 ingress：poison/dedup ACK，账本失败则保留 pending。"""
        raw = fields.get("event")
        payload_length = len(raw) if isinstance(raw, (str, bytes)) else 0
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("event payload must be a JSON object")
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning(
                "malformed event, acking",
                msg_id=msg_id,
                payload_length=payload_length,
            )
            self._redis.xack(self._stream, self._group, msg_id)
            return

        try:
            context = ReviewContext.from_event(msg_id, event)
        except Exception as exc:
            logger.warning(
                "invalid event, acking",
                msg_id=msg_id,
                payload_length=payload_length,
                error_type=type(exc).__name__,
            )
            self._redis.xack(self._stream, self._group, msg_id)
            return

        decision = self._deduper.decide(context) if self._deduper is not None else DedupDecision.RUN
        if decision is DedupDecision.SKIP and self._episode_aggregator is None:
            logger.info(
                "duplicate event skipped",
                event_id=context.event_id,
                source=context.source,
                tracks=sorted({detection.track_id for detection in context.detections}),
            )
            self._redis.xack(self._stream, self._group, msg_id)
            return

        try:
            with self._ledger_factory() as ledger:
                if self._episode_aggregator is not None:
                    outcome = self._episode_aggregator.ingest(
                        ledger,
                        context,
                        decision,
                    )
                    if decision is DedupDecision.SKIP:
                        logger.info(
                            "duplicate event merged into episode",
                            event_id=context.event_id,
                            episode_id=(outcome.episode.episode_id if outcome.episode else None),
                        )
                else:
                    ledger.record(context)
        except Exception as exc:
            logger.error(
                "event ledger record failed; leaving Redis entry pending",
                event_id=context.event_id,
                ingress_id=msg_id,
                payload_length=payload_length,
                error_type=type(exc).__name__,
            )
            return
        logger.info(
            "event ledger committed",
            event_id=context.event_id,
            ingress_id=msg_id,
        )
        self._redis.xack(self._stream, self._group, msg_id)

    def _flush_episode_timeouts(self) -> None:
        """在 Consumer 空闲时关闭已到期 episode，触发唯一取证任务。"""
        if self._episode_aggregator is None:
            return
        try:
            with self._ledger_factory() as ledger:
                closed = self._episode_aggregator.flush(ledger)
        except Exception as exc:
            logger.warning(
                "episode timeout flush failed",
                error_type=type(exc).__name__,
            )
            return
        for episode in closed:
            logger.info(
                "event episode closed",
                episode_id=episode.episode_id,
                event_id=episode.canonical_event_id,
                close_reason=(episode.close_reason.value if episode.close_reason else None),
                evidence_window=(
                    [episode.evidence_window_start, episode.evidence_window_end]
                    if episode.evidence_window_start is not None
                    else None
                ),
            )

    def _handle_event(self, msg_id: str, fields: dict[str, str]) -> None:
        """兼容旧调用方；新代码应使用公开的 ``handle_event`` seam。"""
        self.handle_event(msg_id, fields)


def run_consumer(config: SsvConfig) -> None:
    """Run the event consumer with graceful shutdown."""
    consumer = EventConsumer(config)

    def _shutdown(sig: int, _frame: object) -> None:
        logger.info("received signal, stopping consumer", signal=sig)
        consumer.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    consumer.start()
