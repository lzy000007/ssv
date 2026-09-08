from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from threading import Event, Thread

import pytest
import yaml

from ssv_agent import service
from ssv_agent.config import SsvConfig


def test_run_owns_agent_service_lifecycle(monkeypatch) -> None:
    calls: list[str] = []

    class FakeRuntime:
        def start(self) -> None:
            calls.append("start")

        def wait(self) -> None:
            calls.append("wait")

        def stop(self) -> None:
            calls.append("stop")

    runtime = FakeRuntime()
    monkeypatch.setattr(service, "AgentService", lambda _cfg: runtime)
    cfg = SsvConfig()

    service.run(cfg)

    assert calls == ["start", "wait", "stop"]


def test_agent_service_starts_and_stops_consumer_and_enabled_workers() -> None:
    class FakeConsumer:
        def __init__(self) -> None:
            self.started = Event()
            self.stopped = Event()

        def start(self) -> None:
            self.started.set()
            self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

    class FakeWorker:
        def __init__(self) -> None:
            self.started = Event()

        def run(self, stopping: Event) -> None:
            self.started.set()
            stopping.wait()

    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    consumer = FakeConsumer()
    review_worker = FakeWorker()
    index_worker = FakeWorker()
    client = FakeClient()
    cfg = SsvConfig.model_validate(
        {"agent": {"review": {"enabled": True}, "indexing": {"enabled": True}}}
    )
    runtime = service.AgentService(
        cfg,
        consumer_factory=lambda _: consumer,
        review_worker_factory=lambda **_: review_worker,
        index_worker_factory=lambda **_: index_worker,
        client_factory=lambda **_: client,
        embedding_factory=lambda *_args, **_kwargs: object(),
    )

    runtime.start()
    assert consumer.started.wait(1)
    assert review_worker.started.wait(1)
    assert index_worker.started.wait(1)

    runtime.stop(join_timeout_seconds=1)

    assert consumer.stopped.is_set()
    assert client.closed is True


def test_review_client_is_constructed_with_only_read_only_event_tools() -> None:
    class FakeConsumer:
        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

    class FakeWorker:
        def run(self, stopping: Event) -> None:
            stopping.wait()

    observed: dict[str, object] = {}
    cfg = SsvConfig.model_validate({"agent": {"review": {"enabled": True}}})
    runtime = service.AgentService(
        cfg,
        consumer_factory=lambda _: FakeConsumer(),
        review_worker_factory=lambda **_: FakeWorker(),
        client_factory=lambda **kwargs: observed.update(kwargs) or object(),
    )

    runtime.start()
    config_path = observed["config_path"]
    with open(config_path) as handle:
        generated = yaml.safe_load(handle)
    runtime.stop(join_timeout_seconds=1)

    assert {item["name"] for item in generated["tools"]} == {
        "get_event",
        "evidence_reader",
        "rule_retriever",
        "search_events",
    }
    assert observed["available_skills"] == set()
    assert observed["subagent_enabled"] is False
    assert observed["plan_mode"] is False
    assert generated["authorization"] == {
        "enabled": True,
        "fail_closed": True,
        "default_role": "ssv_review",
        "provider": {
            "use": "deerflow.authz.rbac:RbacAuthorizationProvider",
            "config": {
                "roles": {
                    "ssv_review": {
                        "tools": {
                            "allow": [
                                "evidence_reader",
                                "get_event",
                                "rule_retriever",
                                "search_events",
                                "view_image",
                            ]
                        }
                    }
                }
            },
        },
    }


def test_review_view_image_rejects_same_name_non_builtin(monkeypatch) -> None:
    from deerflow.tools import builtins as builtin_registry

    service._validate_review_view_image_tool(builtin_registry.view_image_tool)

    class FakeTool:
        name = "view_image"

        @staticmethod
        def func(_path: str) -> None:
            return None

    with pytest.raises(ValueError, match="canonical builtin"):
        service._validate_review_view_image_tool(FakeTool())

    def reject_builtin() -> None:
        raise ValueError("DeerFlow view_image registry entry is not canonical")

    monkeypatch.setattr(service, "_load_review_view_image_tool", reject_builtin)
    runtime = service.AgentService(SsvConfig())
    with pytest.raises(ValueError, match="not canonical"):
        runtime._create_review_config()
    runtime.stop()


def test_review_runtime_uses_temporary_extensions_config_and_restores_environment(
    monkeypatch,
) -> None:
    class FakeConsumer:
        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

    class FakeWorker:
        def run(self, stopping: Event) -> None:
            stopping.wait()

    class FakeClient:
        def close(self) -> None:
            return None

    observed: dict[str, object] = {}
    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", "/operator/config.yaml")
    monkeypatch.setenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH", "/operator/extensions.json")
    runtime = service.AgentService(
        SsvConfig.model_validate({"agent": {"review": {"enabled": True}}}),
        consumer_factory=lambda _: FakeConsumer(),
        review_worker_factory=lambda **_: FakeWorker(),
        client_factory=lambda **kwargs: observed.update(
            {
                **kwargs,
                "extensions_path": Path(os.environ["DEER_FLOW_EXTENSIONS_CONFIG_PATH"]),
            }
        )
        or FakeClient(),
    )

    runtime.start()
    extensions_path = observed["extensions_path"]
    assert isinstance(extensions_path, Path)
    assert json.loads(extensions_path.read_text(encoding="utf-8")) == {}
    assert extensions_path.parent == runtime._review_config_dir
    assert os.environ["DEER_FLOW_CONFIG_PATH"] == observed["config_path"]

    runtime.stop(join_timeout_seconds=1)

    assert os.environ["DEER_FLOW_EXTENSIONS_CONFIG_PATH"] == "/operator/extensions.json"
    assert os.environ["DEER_FLOW_CONFIG_PATH"] == "/operator/config.yaml"
    assert not extensions_path.exists()


def test_review_client_loads_isolated_rbac_configuration(monkeypatch) -> None:
    class FakeConsumer:
        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

    class FakeWorker:
        def run(self, stopping: Event) -> None:
            stopping.wait()

    monkeypatch.setenv("SSV_AGENT_SUPPORTS_VISION", "true")
    runtime = service.AgentService(
        SsvConfig.model_validate(
            {"agent": {"model_name": "default", "review": {"enabled": True}}}
        ),
        consumer_factory=lambda _: FakeConsumer(),
        review_worker_factory=lambda **_: FakeWorker(),
    )

    runtime.start()
    client = runtime._deerflow_client
    assert client is not None
    assert client._app_config.authorization.enabled is True
    assert client._app_config.authorization.default_role == "ssv_review"
    assert [tool.name for tool in client._app_config.tools] == [
        "evidence_reader",
        "get_event",
        "search_events",
        "rule_retriever",
    ]

    runtime.stop(join_timeout_seconds=1)


def test_start_after_stop_does_not_create_resources() -> None:
    created: list[str] = []
    runtime = service.AgentService(
        SsvConfig(),
        consumer_factory=lambda _: created.append("consumer") or object(),
    )

    runtime.request_stop()
    runtime.start()
    runtime.stop()

    assert created == []
    assert runtime._started is False


def test_mock_embedding_ignores_configured_model() -> None:
    cfg = SsvConfig.model_validate(
        {
            "agent": {
                "indexing": {
                    "embedding_backend": "mock",
                    "embedding_model": "configured-but-ignored",
                }
            }
        }
    )

    embedding = service.AgentService(cfg)._build_embedding()

    vector = asyncio.run(embedding.embed_query("configured mock"))
    assert len(vector) == 64
    assert vector == asyncio.run(embedding.embed_query("configured mock"))


def test_indexing_uses_same_embedding_settings_as_search_environment(monkeypatch) -> None:
    class FakeConsumer:
        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

    class FakeWorker:
        def run(self, stopping: Event) -> None:
            stopping.wait()

    observed: dict[str, object] = {}
    monkeypatch.setenv("SSV_EMBEDDING_BACKEND", "operator-backend")
    monkeypatch.setenv("SSV_EMBEDDING_MODEL", "operator-model")
    monkeypatch.setenv("SSV_EVIDENCE_ROOTS", '["/operator/evidence"]')
    monkeypatch.delenv("SSV_KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.delenv("SSV_QDRANT_PATH", raising=False)
    monkeypatch.delenv("SSV_KNOWLEDGE_MIN_SCORE", raising=False)
    cfg = SsvConfig.model_validate(
        {
            "agent": {
                "evidence_roots": ["/configured/evidence"],
                "indexing": {
                    "enabled": True,
                    "embedding_backend": "bge_m3",
                    "embedding_model": "/models/bge-m3",
                },
                "knowledge": {
                    "backend": "qdrant",
                    "qdrant_path": "custom/qdrant",
                    "min_score": 0.65,
                },
            }
        }
    )
    runtime = service.AgentService(
        cfg,
        consumer_factory=lambda _: FakeConsumer(),
        embedding_factory=lambda backend, **kwargs: observed.update(
            {"embedding": (backend, kwargs)}
        )
        or object(),
        index_worker_factory=lambda **_: observed.update(
            {
                "search_environment": (
                    os.environ["SSV_EMBEDDING_BACKEND"],
                    os.environ["SSV_EMBEDDING_MODEL"],
                    os.environ["SSV_EVIDENCE_ROOTS"],
                    os.environ["SSV_KNOWLEDGE_BACKEND"],
                    os.environ["SSV_QDRANT_PATH"],
                    os.environ["SSV_KNOWLEDGE_MIN_SCORE"],
                )
            }
        )
        or FakeWorker(),
    )

    runtime.start()
    runtime.stop(join_timeout_seconds=1)

    assert observed["embedding"] == ("bge_m3", {"model_name": "/models/bge-m3"})
    assert observed["search_environment"] == (
        "bge_m3",
        "/models/bge-m3",
        '["/configured/evidence"]',
        "qdrant",
        str(service._agent_root() / "custom/qdrant"),
        "0.65",
    )
    assert os.environ["SSV_EMBEDDING_BACKEND"] == "operator-backend"
    assert os.environ["SSV_EMBEDDING_MODEL"] == "operator-model"
    assert os.environ["SSV_EVIDENCE_ROOTS"] == '["/operator/evidence"]'


def test_run_requests_stop_on_sigterm_and_restores_previous_handlers(monkeypatch) -> None:
    calls: list[str] = []
    previous_handlers = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    active_handlers = dict(previous_handlers)
    signal_calls: list[tuple[signal.Signals, object]] = []

    def fake_signal(signum: signal.Signals, handler: object) -> object:
        previous = active_handlers[signum]
        active_handlers[signum] = handler
        signal_calls.append((signum, handler))
        return previous

    class FakeRuntime:
        def start(self) -> None:
            calls.append("start")

        def wait(self) -> None:
            calls.append("wait")
            active_handlers[signal.SIGTERM](signal.SIGTERM, None)

        def request_stop(self) -> None:
            calls.append("request_stop")

        def stop(self) -> None:
            calls.append("stop")

    runtime = FakeRuntime()
    monkeypatch.setattr(service.signal, "signal", fake_signal)
    monkeypatch.setattr(service, "AgentService", lambda _cfg: runtime)

    service.run(SsvConfig())

    assert calls == ["start", "wait", "request_stop", "stop"]
    assert active_handlers == previous_handlers
    assert [signum for signum, _handler in signal_calls] == [
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGTERM,
    ]


def test_run_restores_handlers_when_stop_raises(monkeypatch) -> None:
    previous_handlers = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    active_handlers = dict(previous_handlers)

    def fake_signal(signum: signal.Signals, handler: object) -> object:
        previous = active_handlers[signum]
        active_handlers[signum] = handler
        return previous

    class FailingRuntime:
        def start(self) -> None:
            return None

        def wait(self) -> None:
            return None

        def stop(self) -> None:
            raise RuntimeError("close failed")

    monkeypatch.setattr(service.signal, "signal", fake_signal)
    monkeypatch.setattr(service, "AgentService", lambda _cfg: FailingRuntime())

    with pytest.raises(RuntimeError, match="close failed"):
        service.run(SsvConfig())

    assert active_handlers == previous_handlers


def test_stop_defers_resource_cleanup_until_timed_out_worker_exits() -> None:
    class FakeConsumer:
        def __init__(self) -> None:
            self.started = Event()
            self.stop_requested = Event()
            self.closed = False

        def start(self) -> None:
            self.started.set()
            self.stop_requested.wait()

        def stop(self) -> None:
            self.stop_requested.set()

        def close(self) -> None:
            self.closed = True

    class BlockingWorker:
        def __init__(self) -> None:
            self.started = Event()
            self.release = Event()

        def run(self, _: Event) -> None:
            self.started.set()
            self.release.wait()

    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    consumer = FakeConsumer()
    worker = BlockingWorker()
    client = FakeClient()
    cfg = SsvConfig.model_validate({"agent": {"review": {"enabled": True}}})
    runtime = service.AgentService(
        cfg,
        consumer_factory=lambda _: consumer,
        review_worker_factory=lambda **_: worker,
        client_factory=lambda **_: client,
    )

    runtime.start()
    assert consumer.started.wait(1)
    assert worker.started.wait(1)
    review_config_dir = runtime._review_config_dir
    assert review_config_dir is not None
    assert review_config_dir.exists()

    runtime.stop(join_timeout_seconds=0.01)

    assert consumer.stop_requested.is_set()
    assert consumer.closed is False
    assert client.closed is False
    assert review_config_dir.exists()
    assert runtime._deerflow_client is client
    assert runtime._review_config_dir == review_config_dir

    worker.release.set()
    runtime.stop(join_timeout_seconds=1)

    assert consumer.closed is True
    assert client.closed is True
    assert not review_config_dir.exists()
    assert runtime._deerflow_client is None
    assert runtime._review_config_dir is None


def test_signal_stop_during_consumer_creation_does_not_deadlock_or_start_ingress() -> None:
    class FakeConsumer:
        def __init__(self) -> None:
            self.started = Event()
            self.stop_calls = 0

        def start(self) -> None:
            self.started.set()

        def stop(self) -> None:
            self.stop_calls += 1

    consumer = FakeConsumer()

    def consumer_factory(_: SsvConfig) -> FakeConsumer:
        # Signal handlers run synchronously in the thread that is currently
        # holding AgentService's lifecycle lock.
        runtime.request_stop()
        return consumer

    runtime = service.AgentService(SsvConfig(), consumer_factory=consumer_factory)
    start_thread = Thread(target=runtime.start, daemon=True)

    start_thread.start()
    start_thread.join(timeout=1)

    assert not start_thread.is_alive()
    assert runtime._stopping.is_set()
    assert consumer.stop_calls == 1
    assert not consumer.started.is_set()

    runtime.stop(join_timeout_seconds=1)
