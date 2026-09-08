from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import shutil
import tempfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from threading import Event, Lock, Thread, current_thread, main_thread
from time import monotonic
from typing import Any

import structlog
import yaml

from deerflow.client import DeerFlowClient

from ssv_agent.config import SsvConfig
from ssv_agent.embedding.registry import create_provider
from ssv_agent.event_consumer import EventConsumer
from ssv_agent.event_store import EventLedger
from ssv_agent.event_store.qdrant_store import SsvQdrantStore
from ssv_agent.runner import run_review
from ssv_agent.workers import IndexWorker, ReviewWorker

logger = structlog.get_logger()

_REVIEW_TOOL_NAMES = frozenset(
    {"get_event", "evidence_reader", "rule_retriever", "search_events", "view_image"}
)
_REVIEW_CONFIGURED_TOOL_NAMES = _REVIEW_TOOL_NAMES - {"view_image"}
_REVIEW_VIEW_IMAGE_MODULE = "deerflow.tools.builtins.view_image_tool"
_ENV_UNSET = object()
_DEERFLOW_ENV_KEYS = (
    "DEER_FLOW_CONFIG_PATH",
    "DEER_FLOW_PROJECT_ROOT",
    "DEER_FLOW_HOME",
    "SSV_AGENT_MODEL",
)


def _agent_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _set_deerflow_env(config: SsvConfig) -> None:
    """设置 DeerFlow 配置与项目根路径环境变量。"""
    root = _agent_root()
    os.environ.setdefault("DEER_FLOW_CONFIG_PATH", str(root / "config.yaml"))
    os.environ.setdefault("DEER_FLOW_PROJECT_ROOT", str(root))
    os.environ.setdefault("DEER_FLOW_HOME", str(root / ".deer-flow"))
    if config.agent.model_name:
        os.environ.setdefault("SSV_AGENT_MODEL", config.agent.model_name)


def _embedding_settings(config: SsvConfig) -> tuple[str, str | None]:
    """Return the single embedding seam shared with the search tool."""
    indexing = config.agent.indexing
    return indexing.embedding_backend, indexing.embedding_model


def _resolve_agent_path(value: str) -> Path:
    """把 Agent 配置中的路径解析到稳定的 Agent 项目目录。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else _agent_root() / path


def _close_resource(resource: object | None) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        asyncio.run(result)


def _validate_review_view_image_tool(tool: object) -> None:
    """拒绝被同名配置/MCP 工具替换的视觉 builtin。"""
    implementation = getattr(tool, "func", None)
    if not callable(implementation):
        implementation = getattr(tool, "coroutine", None)
    if (
        getattr(tool, "name", None) != "view_image"
        or not callable(implementation)
        or getattr(implementation, "__module__", None) != _REVIEW_VIEW_IMAGE_MODULE
        or getattr(implementation, "__name__", None) != "view_image_tool"
    ):
        raise ValueError(
            "review view_image must be DeerFlow's canonical builtin implementation"
        )


def _load_review_view_image_tool() -> object:
    """从 DeerFlow builtin registry 加载并验证 view_image。"""
    try:
        from deerflow.tools import builtins as builtin_registry
        from deerflow.tools.builtins.view_image_tool import (
            view_image_tool as canonical_tool,
        )
    except (ImportError, AttributeError) as exc:
        raise ValueError("DeerFlow builtin view_image is unavailable") from exc

    if getattr(builtin_registry, "view_image_tool", None) is not canonical_tool:
        raise ValueError("DeerFlow view_image registry entry is not canonical")
    _validate_review_view_image_tool(canonical_tool)
    return canonical_tool


class AgentService:
    """拥有 Redis ingress 与异步复核/索引 worker 的进程生命周期。"""

    def __init__(
        self,
        config: SsvConfig,
        *,
        consumer_factory: Callable[[SsvConfig], EventConsumer] = EventConsumer,
        review_worker_factory: Callable[..., ReviewWorker] = ReviewWorker,
        index_worker_factory: Callable[..., IndexWorker] = IndexWorker,
        client_factory: Callable[[], Any] = DeerFlowClient,
        embedding_factory: Callable[..., Any] = create_provider,
        qdrant_factory: Callable[[], SsvQdrantStore] = SsvQdrantStore,
    ) -> None:
        self._config = config
        self._consumer_factory = consumer_factory
        self._review_worker_factory = review_worker_factory
        self._index_worker_factory = index_worker_factory
        self._client_factory = client_factory
        self._embedding_factory = embedding_factory
        self._qdrant_factory = qdrant_factory
        self._stopping = Event()
        self._lifecycle_lock = Lock()
        self._consumer: EventConsumer | None = None
        self._consumer_thread: Thread | None = None
        self._worker_threads: list[Thread] = []
        self._deerflow_client: object | None = None
        self._review_config_dir: Path | None = None
        self._extensions_config_path: Path | None = None
        self._environment: dict[str, str | object] = {}
        self._started = False

    def start(self) -> None:
        """启动已启用的 worker，再开始从 Redis 接收新 ingress。"""
        with self._lifecycle_lock:
            if self._started or self._stopping.is_set():
                return
            self._started = True
            try:
                if self._stopping.is_set():
                    return
                self._start_review_worker()
                if self._stopping.is_set():
                    return
                self._start_index_worker()
                if self._stopping.is_set():
                    return
                consumer = self._consumer_factory(self._config)
                # request_stop() deliberately does not take _lifecycle_lock. If
                # it ran while the factory was executing, own and stop the
                # consumer before any ingress thread can be created.
                self._consumer = consumer
                if self._stopping.is_set():
                    consumer.stop()
                    return
                consumer_thread = Thread(
                    target=self._run_consumer,
                    args=(consumer,),
                    name="ssv-agent-consumer",
                    daemon=True,
                )
                self._consumer_thread = consumer_thread
                if self._stopping.is_set():
                    consumer.stop()
                    return
                consumer_thread.start()
            except BaseException:
                # Keep any partially created resources owned by this instance so
                # the caller's finally/stop path can release them safely.
                self._stopping.set()
                raise

    def wait(self) -> None:
        """等待消费线程结束；worker 的退出由 ``stop`` 统一管理。"""
        if self._consumer_thread is not None:
            self._consumer_thread.join()

    def request_stop(self) -> None:
        """停止新的 ingress 与任务领取，但不触碰仍可能被 worker 使用的资源。"""
        self._stopping.set()
        consumer = self._consumer
        if consumer is not None:
            consumer.stop()

    def stop(self, *, join_timeout_seconds: float = 10.0) -> None:
        """停止领取新工作，有限等待在途调用后关闭外部资源。"""
        errors: list[BaseException] = []
        try:
            self.request_stop()
        except BaseException as exc:
            errors.append(exc)
        deadline = monotonic() + max(join_timeout_seconds, 0.0)
        with self._lifecycle_lock:
            threads = [*self._worker_threads]
            if self._consumer_thread is not None:
                threads.append(self._consumer_thread)
        for thread in threads:
            if thread.is_alive():
                try:
                    thread.join(max(0.0, deadline - monotonic()))
                except BaseException as exc:
                    errors.append(exc)

        alive_threads = [thread.name for thread in threads if thread.is_alive()]
        if alive_threads:
            logger.warning(
                "agent service stop timed out; preserving shared resources",
                alive_threads=alive_threads,
            )
            for error in errors:
                logger.warning("agent service stop request failed", error=str(error))
            return

        consumer = self._consumer
        try:
            _close_resource(consumer)
        except BaseException as exc:
            errors.append(exc)
        finally:
            self._consumer = None

        client = self._deerflow_client
        try:
            _close_resource(client)
        except BaseException as exc:
            errors.append(exc)
        finally:
            self._deerflow_client = None

        self._restore_environment(errors)
        self._reset_deerflow_runtime_caches(errors)

        review_config_dir = self._review_config_dir
        try:
            if review_config_dir is not None:
                shutil.rmtree(review_config_dir, ignore_errors=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            self._review_config_dir = None
            self._extensions_config_path = None

        if errors:
            raise errors[0]

    def _start_review_worker(self) -> None:
        worker_config = self._config.agent.review
        if not worker_config.enabled or self._stopping.is_set():
            return
        self._configure_runtime_environment(include_deerflow=True)
        if self._stopping.is_set():
            return
        self._create_extensions_config()
        if self._stopping.is_set():
            return
        review_config_path = self._create_review_config()
        if self._stopping.is_set():
            return
        # DeerFlowClient reloads the explicit path and then calls get_app_config(),
        # which resolves DEER_FLOW_CONFIG_PATH again. Keep both resolutions on the
        # isolated config so the global cache cannot fall back to agent/config.yaml.
        self._set_owned_environment("DEER_FLOW_CONFIG_PATH", str(review_config_path))
        if self._stopping.is_set():
            return
        self._deerflow_client = self._client_factory(
            config_path=str(review_config_path),
            available_skills=set(),
            subagent_enabled=False,
            plan_mode=False,
        )
        if self._stopping.is_set():
            return
        worker = self._review_worker_factory(
            ledger_factory=self._ledger_factory,
            runner=partial(run_review, self._deerflow_client),
            worker_id="ssv-review-0",
            lease_ms=worker_config.lease_ms,
            max_retries=worker_config.max_retries,
            retry_delay_ms=worker_config.retry_delay_ms,
            poll_interval_seconds=worker_config.poll_interval_ms / 1000,
            policy_id=worker_config.policy_id,
            model_id=worker_config.model_id or self._config.agent.model_name,
        )
        if self._stopping.is_set():
            return
        self._start_worker_thread("ssv-agent-review", worker.run)

    def _start_index_worker(self) -> None:
        worker_config = self._config.agent.indexing
        if not worker_config.enabled or self._stopping.is_set():
            return
        self._configure_runtime_environment(include_deerflow=False)
        if self._stopping.is_set():
            return
        embedding = self._build_embedding()
        if self._stopping.is_set():
            return
        worker = self._index_worker_factory(
            ledger_factory=self._ledger_factory,
            embedding=embedding,
            qdrant_factory=self._qdrant_factory,
            worker_id="ssv-index-0",
            lease_ms=worker_config.lease_ms,
            max_retries=worker_config.max_retries,
            retry_delay_ms=worker_config.retry_delay_ms,
            poll_interval_seconds=worker_config.poll_interval_ms / 1000,
        )
        if self._stopping.is_set():
            return
        self._start_worker_thread("ssv-agent-index", worker.run)

    def _build_embedding(self) -> Any:
        backend, model = _embedding_settings(self._config)
        if model is None or backend == "mock":
            return self._embedding_factory(backend)
        key = "model_name" if backend == "bge_m3" else "model"
        return self._embedding_factory(backend, **{key: model})

    def _ledger_factory(self) -> EventLedger:
        """为每个 worker 调用创建带同一证据根策略的独立账本连接。"""
        return EventLedger(evidence_roots=self._config.agent.evidence_roots)

    def _configure_runtime_environment(self, *, include_deerflow: bool) -> None:
        if include_deerflow:
            previous = {
                name: os.environ.get(name, _ENV_UNSET)
                for name in _DEERFLOW_ENV_KEYS
            }
            _set_deerflow_env(self._config)
            for name, value in previous.items():
                if os.environ.get(name, _ENV_UNSET) != value:
                    self._environment.setdefault(name, value)

        backend, model = _embedding_settings(self._config)
        self._set_owned_environment("SSV_EMBEDDING_BACKEND", backend)
        self._set_owned_environment("SSV_EMBEDDING_MODEL", model)
        knowledge = self._config.agent.knowledge
        knowledge_backend = os.environ.get("SSV_KNOWLEDGE_BACKEND") or knowledge.backend
        knowledge_path = os.environ.get("SSV_QDRANT_PATH") or knowledge.qdrant_path
        knowledge_min_score = os.environ.get("SSV_KNOWLEDGE_MIN_SCORE") or str(
            knowledge.min_score
        )
        self._set_owned_environment("SSV_KNOWLEDGE_BACKEND", knowledge_backend)
        self._set_owned_environment(
            "SSV_QDRANT_PATH",
            str(_resolve_agent_path(knowledge_path).resolve()),
        )
        self._set_owned_environment("SSV_KNOWLEDGE_MIN_SCORE", knowledge_min_score)
        self._set_owned_environment(
            "SSV_EVIDENCE_ROOTS",
            json.dumps(self._config.agent.evidence_roots),
        )

    def _set_owned_environment(self, name: str, value: str | None) -> None:
        self._environment.setdefault(name, os.environ.get(name, _ENV_UNSET))
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def _create_extensions_config(self) -> Path:
        if self._extensions_config_path is not None:
            return self._extensions_config_path
        review_config_dir = self._ensure_review_config_dir()
        path = review_config_dir / "extensions_config.json"
        path.write_text(json.dumps({}) + "\n", encoding="utf-8")
        self._set_owned_environment("DEER_FLOW_EXTENSIONS_CONFIG_PATH", str(path))
        self._extensions_config_path = path
        return path

    def _ensure_review_config_dir(self) -> Path:
        if self._review_config_dir is None:
            self._review_config_dir = Path(tempfile.mkdtemp(prefix="ssv-review-"))
        return self._review_config_dir

    def _restore_environment(self, errors: list[BaseException]) -> None:
        environment = self._environment
        self._environment = {}
        for name, value in reversed(list(environment.items())):
            try:
                if value is _ENV_UNSET:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = str(value)
            except BaseException as exc:
                errors.append(exc)

    @staticmethod
    def _reset_deerflow_runtime_caches(errors: list[BaseException]) -> None:
        try:
            from deerflow.config.app_config import reset_app_config
            from deerflow.config.extensions_config import reset_extensions_config

            reset_extensions_config()
            reset_app_config()
        except BaseException as exc:
            errors.append(exc)

    def _create_review_config(self) -> Path:
        """为复核 client 创建配置工具和 RBAC 均受限的临时 DeerFlow 配置。"""
        _load_review_view_image_tool()
        agent_root = _agent_root()
        source_config = agent_root / "config.yaml"
        if not source_config.is_file():
            source_config = agent_root / "config.example.yaml"
        with open(source_config, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        if not isinstance(config, dict):
            raise ValueError("invalid DeerFlow configuration")
        configured_names = {
            item.get("name")
            for item in config.get("tools", [])
            if isinstance(item, dict)
        }
        if "view_image" in configured_names:
            raise ValueError("review config cannot shadow DeerFlow builtin view_image")
        tools = [
            item
            for item in config.get("tools", [])
            if isinstance(item, dict)
            and item.get("name") in _REVIEW_CONFIGURED_TOOL_NAMES
        ]
        if {item.get("name") for item in tools} != _REVIEW_CONFIGURED_TOOL_NAMES:
            raise ValueError("review tool configuration is incomplete")
        groups = {item.get("group") for item in tools}
        config["tools"] = tools
        config["tool_groups"] = [
            item
            for item in config.get("tool_groups", [])
            if isinstance(item, dict) and item.get("name") in groups
        ]
        config["authorization"] = {
            "enabled": True,
            "fail_closed": True,
            "default_role": "ssv_review",
            "provider": {
                "use": "deerflow.authz.rbac:RbacAuthorizationProvider",
                "config": {
                    "roles": {
                        "ssv_review": {
                            "tools": {"allow": sorted(_REVIEW_TOOL_NAMES)},
                        }
                    }
                },
            },
        }
        path = self._ensure_review_config_dir() / "config.yaml"
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
        return path

    def _start_worker_thread(self, name: str, run: Callable[[Event], None]) -> None:
        if self._stopping.is_set():
            return
        thread = Thread(target=run, args=(self._stopping,), name=name, daemon=True)
        if self._stopping.is_set():
            return
        self._worker_threads.append(thread)
        if self._stopping.is_set():
            return
        thread.start()

    def _run_consumer(self, consumer: EventConsumer) -> None:
        if not self._stopping.is_set():
            consumer.start()


def run(config: SsvConfig) -> None:
    """运行 Agent 进程，直到消费线程退出或收到外部停止请求。"""
    logger.info(
        "agent service starting",
        version=config.version,
        redis=f"{config.redis.host}:{config.redis.port}",
        stream=config.redis.stream_key,
    )

    runtime = AgentService(config)
    previous_handlers: dict[signal.Signals, Any] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        runtime.request_stop()

    try:
        if current_thread() is main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, request_stop)
        runtime.start()
        runtime.wait()
    finally:
        try:
            runtime.stop()
        finally:
            restore_errors: list[BaseException] = []
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except BaseException as exc:
                    restore_errors.append(exc)
            logger.info("agent service stopped")
            if restore_errors:
                raise restore_errors[0]
