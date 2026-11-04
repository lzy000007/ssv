from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from redis.exceptions import ResponseError

from ssv_agent.config import SsvConfig
from ssv_agent.dedup import _track_key
from ssv_agent.event_consumer import EventConsumer
from ssv_agent.event_store import EventLedger, JobKind
from ssv_agent.review_context import ReviewContext


class FakeRedis:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.acked: list[tuple[str, str, str]] = []
        self.created: list[tuple[str, str, str, bool]] = []
        self.keys: dict[str, int] = {}
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int]] = []

    def xgroup_create(self, stream: str, group: str, id: str, mkstream: bool) -> None:
        self.created.append((stream, group, id, mkstream))

    def xack(self, stream: str, group: str, msg_id: str) -> None:
        self.acked.append((stream, group, msg_id))

    def set(self, key: str, value: str, nx: bool = False, px: int = 0) -> bool | None:
        self.set_calls.append((key, value, px))
        if nx and key in self.keys:
            return None
        self.keys[key] = px
        self.values[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)


@dataclass
class ClaimResponse:
    next_start_id: str
    messages: list[tuple[str, dict[str, str]]]


class LoopingRedis(FakeRedis):
    def __init__(
        self,
        *args: Any,
        claim_responses: list[object] | None = None,
        fail_first_claim: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.claim_responses = list(claim_responses or [])
        self.fail_first_claim = fail_first_claim
        self.claim_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.read_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.stop_callback: Any = None

    def xautoclaim(self, *args: Any, **kwargs: Any) -> object:
        self.claim_calls.append((args, kwargs))
        if self.fail_first_claim and len(self.claim_calls) == 1:
            raise ConnectionError("redis temporarily unavailable")
        if self.claim_responses:
            return self.claim_responses.pop(0)
        return ("0-0", [], [])

    def xreadgroup(self, *args: Any, **kwargs: Any) -> list[object]:
        self.read_calls.append((args, kwargs))
        if self.stop_callback is not None:
            self.stop_callback()
        return []


class RecordingLedger:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.records: list[ReviewContext] = []

    def __enter__(self) -> "RecordingLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def record(self, context: ReviewContext) -> None:
        if self.error is not None:
            raise self.error
        self.records.append(context)


def make_consumer(monkeypatch: Any) -> tuple[EventConsumer, FakeRedis]:
    fake = FakeRedis()
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    consumer = EventConsumer(SsvConfig())
    return consumer, fake


def test_default_ledger_factory_propagates_recording_evidence_config(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: FakeRedis())
    config = SsvConfig.model_validate(
        {
            "evidence_cache": {
                "directory": str(tmp_path),
            },
            "agent": {
                "evidence_roots": [str(tmp_path)],
                "recording_evidence": {},
            },
        }
    )
    consumer = EventConsumer(config)
    factory = consumer._ledger_factory
    assert (
        getattr(factory, "keywords", {}).get("recording_evidence")
        == config.agent.recording_evidence
    )


def test_valid_event_records_before_ack(monkeypatch: Any) -> None:
    fake_ledger = RecordingLedger()
    fake = FakeRedis()
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    consumer = EventConsumer(SsvConfig(), ledger_factory=lambda: fake_ledger)

    consumer.handle_event(
        "123-0",
        {
            "event": json.dumps(
                {
                    "event_id": "case-1",
                    "source": "camera-1",
                    "timestamp_ms": 1000,
                    "frame_id": 42,
                    "detections": [],
                }
            )
        },
    )

    assert [context.event_id for context in fake_ledger.records] == ["case-1"]
    assert fake.acked == [("ssv:events", "ssv-agent", "123-0")]


def test_out_of_bounds_evidence_is_ignored_while_event_is_recorded_and_acked(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    db_path = tmp_path / "events.db"
    monkeypatch.setenv("SSV_EVENT_DB_PATH", str(db_path))
    fake = FakeRedis()
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    config = SsvConfig.model_validate(
        {
            "evidence_cache": {"directory": str(root)},
            "agent": {
                "dedup_enabled": False,
                "evidence_roots": [str(root)],
            },
        }
    )
    consumer = EventConsumer(config)

    consumer.handle_event(
        "123-0",
        {
            "event": json.dumps(
                {
                    "event_id": "case-1",
                    "source": "camera-1",
                    "timestamp_ms": 1000,
                    "frame_id": 42,
                    "frame_path": str(outside),
                    "detections": [],
                }
            )
        },
    )

    assert fake.acked == [("ssv:events", "ssv-agent", "123-0")]
    with EventLedger(db_path, evidence_roots=[str(root)]) as ledger:
        case = ledger.get_case("case-1")
        jobs = ledger.jobs_for_event("case-1")
    assert case is not None
    assert case.evidence == ()
    assert jobs == ()


def test_ensure_group_creates_stream_group(monkeypatch: Any) -> None:
    consumer, fake = make_consumer(monkeypatch)

    consumer._ensure_group()

    assert fake.created == [("ssv:events", "ssv-agent", "0", True)]


def test_ensure_group_ignores_only_existing_group(monkeypatch: Any) -> None:
    fake = FakeRedis()
    fake.xgroup_create = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ResponseError("BUSYGROUP Consumer Group name already exists")
    )
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    consumer = EventConsumer(SsvConfig())

    consumer._ensure_group()


def test_ensure_group_propagates_non_busy_errors(monkeypatch: Any) -> None:
    fake = FakeRedis()
    fake.xgroup_create = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ResponseError("NOPERM command not allowed")
    )
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    consumer = EventConsumer(SsvConfig())

    with pytest.raises(ResponseError, match="NOPERM"):
        consumer._ensure_group()


def test_handle_event_parses_detection_and_acks(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SSV_EVENT_DB_PATH", str(tmp_path / "events.db"))
    consumer, fake = make_consumer(monkeypatch)
    payload = {
        "source": "camera-1",
        "frame_id": 42,
        "detections": [
            {"class": "person", "class_id": 0, "confidence": 0.91, "track_id": 5},
        ],
    }

    consumer.handle_event("123-0", {"event": json.dumps(payload)})

    assert fake.acked == [("ssv:events", "ssv-agent", "123-0")]
    with EventLedger(tmp_path / "events.db") as ledger:
        case = ledger.get_case("123-0")
    assert case is not None
    assert case.detections[0]["class_name"] == "person"


def test_handle_event_acks_deterministic_duplicate_before_episode_anchor(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SSV_EVENT_DB_PATH", str(tmp_path / "events.db"))
    consumer, fake = make_consumer(monkeypatch)
    dedup_key = _track_key("ssv:agent:dedup", "camera-1", 5)
    fake.keys[dedup_key] = 30000

    consumer.handle_event(
        "123-0",
        {
            "event": json.dumps(
                {
                    "source": "camera-1",
                    "frame_id": 42,
                    "detections": [
                        {
                            "class": "person",
                            "class_id": 0,
                            "confidence": 0.91,
                            "track_id": 5,
                        },
                    ],
                }
            )
        },
    )

    assert fake.acked == [("ssv:events", "ssv-agent", "123-0")]
    with EventLedger(tmp_path / "events.db") as ledger:
        case = ledger.get_case("123-0")
        jobs = ledger.jobs_for_event("123-0")
    assert case is not None
    assert jobs == ()


def test_dedup_skip_updates_episode_and_terminal_event_creates_one_extract_job(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    db_path = tmp_path / "events.db"
    monkeypatch.setenv("SSV_EVENT_DB_PATH", str(db_path))
    fake = FakeRedis()
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    fake.keys[_track_key("ssv:agent:dedup", "camera-1", 5)] = 30_000
    config = SsvConfig.model_validate(
        {
            "evidence_cache": {"directory": str(root)},
            "agent": {
                "evidence_roots": [str(root)],
                "recording_evidence": {},
            },
        }
    )
    consumer = EventConsumer(config)
    base_timestamp_ms = int(time.time() * 1000)

    def publish(msg_id: str, pts: int, state: int, timestamp_ms: int) -> None:
        consumer.handle_event(
            msg_id,
            {
                "event": json.dumps(
                    {
                        "source": "camera-1",
                        "timestamp_ms": timestamp_ms,
                        "frame_id": state,
                        "stream_generation": 7,
                        "source_pts": pts,
                        "event_type": "person_without_helmet",
                        "detections": [
                            {
                                "class": "person",
                                "class_id": 0,
                                "confidence": 0.9,
                                "track_id": 5,
                                "track_state": state,
                            }
                        ],
                    }
                )
            },
        )

    publish("1-0", 1_000_000_000, 0, base_timestamp_ms)
    publish("2-0", 2_000_000_000, 1, base_timestamp_ms + 1_000)
    publish("3-0", 2_100_000_000, 3, base_timestamp_ms + 1_100)

    with EventLedger(
        db_path,
        evidence_roots=[str(root)],
        recording_evidence=config.agent.recording_evidence,
    ) as ledger:
        episode = ledger.get_episode("1-0")
        jobs = [job for job in ledger.jobs_for_event("1-0") if job.kind is JobKind.EVIDENCE_EXTRACT]

    assert fake.acked == [
        ("ssv:events", "ssv-agent", "1-0"),
        ("ssv:events", "ssv-agent", "2-0"),
        ("ssv:events", "ssv-agent", "3-0"),
    ]
    assert episode is not None
    assert episode.event_ids == ("1-0", "2-0", "3-0")
    assert len(jobs) == 1


def test_handle_event_acks_malformed_json_without_logging_payload(monkeypatch: Any) -> None:
    consumer, fake = make_consumer(monkeypatch)
    entries: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def warning(self, event: str, **fields: object) -> None:
            entries.append((event, fields))

    payload = '{"token":"sensitive-payload"'
    monkeypatch.setattr("ssv_agent.event_consumer.logger", FakeLogger())
    consumer.handle_event("123-0", {"event": payload})

    assert fake.acked == [("ssv:events", "ssv-agent", "123-0")]
    assert entries == [
        (
            "malformed event, acking",
            {"msg_id": "123-0", "payload_length": len(payload)},
        )
    ]
    assert "sensitive-payload" not in repr(entries)


def test_successful_record_acks_without_waiting_for_evidence_worker(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SSV_EVENT_DB_PATH", str(tmp_path / "events.db"))
    consumer, fake = make_consumer(monkeypatch)
    payload = {
        "source": "camera-1",
        "frame_id": 42,
        "detections": [
            {"class": "person", "class_id": 0, "confidence": 0.91, "track_id": 5},
        ],
    }

    consumer.handle_event("123-0", {"event": json.dumps(payload)})

    assert fake.acked == [("ssv:events", "ssv-agent", "123-0")]
    with EventLedger(tmp_path / "events.db") as ledger:
        case = ledger.get_case("123-0")
        jobs = ledger.jobs_for_event("123-0")
    assert case is not None
    assert jobs == ()


def test_ledger_failure_leaves_valid_message_pending(
    monkeypatch: Any,
) -> None:
    fake_ledger = RecordingLedger(OSError("private path /tmp/secret.db"))
    fake = FakeRedis()
    entries: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def error(self, event: str, **fields: object) -> None:
            entries.append((event, fields))

    monkeypatch.setattr("ssv_agent.event_consumer.logger", FakeLogger())
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    consumer = EventConsumer(SsvConfig(), ledger_factory=lambda: fake_ledger)

    payload = json.dumps(
        {
            "source": "camera-1",
            "frame_id": 42,
            "detections": [],
        }
    )
    consumer.handle_event("123-0", {"event": payload})

    assert fake_ledger.records == []
    assert fake.acked == []
    assert entries == [
        (
            "event ledger record failed; leaving Redis entry pending",
            {
                "event_id": "123-0",
                "ingress_id": "123-0",
                "payload_length": len(payload),
                "error_type": "OSError",
            },
        )
    ]
    assert "private path" not in repr(entries)


def test_invalid_event_logs_error_type_without_exception_text(monkeypatch: Any) -> None:
    consumer, fake = make_consumer(monkeypatch)
    entries: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def warning(self, event: str, **fields: object) -> None:
            entries.append((event, fields))

    def invalid_event(*_args: object, **_kwargs: object) -> None:
        raise ValueError("sensitive event details")

    monkeypatch.setattr("ssv_agent.event_consumer.logger", FakeLogger())
    monkeypatch.setattr(
        "ssv_agent.event_consumer.ReviewContext.from_event", invalid_event
    )
    payload = json.dumps({"source": "camera-1"})
    consumer.handle_event("123-0", {"event": payload})

    assert fake.acked == [("ssv:events", "ssv-agent", "123-0")]
    assert entries == [
        (
            "invalid event, acking",
            {
                "msg_id": "123-0",
                "payload_length": len(payload),
                "error_type": "ValueError",
            },
        )
    ]
    assert "sensitive event details" not in repr(entries)


def test_same_event_redelivery_after_ledger_failure_is_recorded_before_ack(
    monkeypatch: Any,
) -> None:
    fake_ledger = RecordingLedger(OSError("disk full"))
    fake = FakeRedis()
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    consumer = EventConsumer(SsvConfig(), ledger_factory=lambda: fake_ledger)
    fields = {
        "event": json.dumps(
            {
                "event_id": "case-1",
                "source": "camera-1",
                "timestamp_ms": 1000,
                "frame_id": 42,
                "detections": [
                    {
                        "class": "person",
                        "class_id": 0,
                        "confidence": 0.91,
                        "track_id": 5,
                    }
                ],
            }
        )
    }

    consumer.handle_event("123-0", fields)
    fake_ledger.error = None
    consumer.handle_event("123-0", fields)

    assert [context.event_id for context in fake_ledger.records] == ["case-1"]
    assert fake.acked == [("ssv:events", "ssv-agent", "123-0")]


def test_consumer_does_not_promote_transport_type_to_event_type(monkeypatch: Any) -> None:
    fake_ledger = RecordingLedger()
    fake = FakeRedis()
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    config = SsvConfig.model_validate({"agent": {"dedup_enabled": False}})
    consumer = EventConsumer(config, ledger_factory=lambda: fake_ledger)

    consumer.handle_event(
        "123-0",
        {
            "event": json.dumps(
                {
                    "event_id": "fallback",
                    "source": "camera-1",
                    "timestamp_ms": 1000,
                    "frame_id": 42,
                    "type": "legacy-alarm",
                }
            )
        },
    )
    consumer.handle_event(
        "124-0",
        {
            "event": json.dumps(
                {
                    "event_id": "explicit",
                    "source": "camera-1",
                    "timestamp_ms": 1000,
                    "frame_id": 42,
                    "type": "legacy-alarm",
                    "event_type": "new-alarm",
                }
            )
        },
    )

    assert [context.event_type for context in fake_ledger.records] == [None, "new-alarm"]


def test_start_reclaims_pending_entries_and_processes_them_through_handle_event(
    monkeypatch: Any,
) -> None:
    fake_ledger = RecordingLedger()
    fake = LoopingRedis(
        claim_responses=[
            ClaimResponse(
                next_start_id="0-0",
                messages=[
                    (
                        "123-0",
                        {
                            "event": json.dumps(
                                {
                                    "event_id": "reclaimed-case",
                                    "source": "camera-1",
                                    "timestamp_ms": 1000,
                                    "frame_id": 42,
                                }
                            )
                        },
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    config = SsvConfig.model_validate({"agent": {"dedup_enabled": False}})
    consumer = EventConsumer(config, ledger_factory=lambda: fake_ledger)
    fake.stop_callback = consumer.stop

    consumer.start()

    assert [context.event_id for context in fake_ledger.records] == ["reclaimed-case"]
    assert fake.acked == [("ssv:events", "ssv-agent", "123-0")]
    assert fake.claim_calls


def test_transient_autoclaim_failure_does_not_stop_the_consumer_loop(monkeypatch: Any) -> None:
    fake = LoopingRedis(fail_first_claim=True)
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    monkeypatch.setattr("ssv_agent.event_consumer.time.sleep", lambda _seconds: None)
    config = SsvConfig.model_validate({"agent": {"dedup_enabled": False}})
    consumer = EventConsumer(config)
    fake.stop_callback = consumer.stop

    consumer.start()

    assert fake.claim_calls
    assert fake.read_calls


def test_default_consumer_names_are_unique_per_process_instance(monkeypatch: Any) -> None:
    first = LoopingRedis()
    second = LoopingRedis()
    fakes = iter((first, second))
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: next(fakes))
    config = SsvConfig.model_validate({"agent": {"dedup_enabled": False}})
    consumer_a = EventConsumer(config)
    consumer_b = EventConsumer(config)
    first.stop_callback = consumer_a.stop
    second.stop_callback = consumer_b.stop

    consumer_a.start()
    consumer_b.start()

    def claim_consumer(fake: LoopingRedis) -> str:
        args, kwargs = fake.claim_calls[0]
        return str(kwargs.get("consumername") or kwargs.get("consumer") or args[2])

    assert claim_consumer(first) != claim_consumer(second)


def test_stop_before_start_never_reopens_the_consumer_loop(monkeypatch: Any) -> None:
    fake = LoopingRedis()
    monkeypatch.setattr("ssv_agent.event_consumer.Redis", lambda **_kwargs: fake)
    consumer = EventConsumer(SsvConfig.model_validate({"agent": {"dedup_enabled": False}}))
    fake.stop_callback = consumer.stop

    consumer.stop()
    consumer.start()

    assert fake.claim_calls == []
    assert fake.read_calls == []
