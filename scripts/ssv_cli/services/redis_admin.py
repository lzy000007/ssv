"""Redis-py adapter for SSV runtime-cache administration."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Protocol, Self

from ..config import RedisSettings

RedisValue = Any
AGENT_DEDUP_KEY_PATTERN = "ssv:agent:dedup:*"


class RedisError(RuntimeError):
    """Base class for connection and server errors."""


class RedisCommandError(RedisError):
    """Redis returned a command error."""


def _load_redis_module() -> Any:
    """仅在 Redis 命令实际连接时加载 redis-py。"""

    try:
        return importlib.import_module("redis")
    except ModuleNotFoundError as exc:
        raise RedisError(
            "Redis 管理命令需要 redis-py；请安装项目运行依赖或执行 pip install redis"
        ) from exc


class RedisConnectionProtocol(Protocol):
    def execute(self, *args: str | int) -> RedisValue:
        ...

    def close(self) -> None:
        ...


class RedisConnection:
    """供 ``RedisAdmin`` 使用的同步 redis-py 适配器。"""

    def __init__(self, settings: RedisSettings, timeout: float = 3.0) -> None:
        self._settings = settings
        self._timeout = timeout
        self._client: Any | None = None
        self._redis_module: Any | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def _client_or_create(self) -> Any:
        if self._client is None:
            self._redis_module = _load_redis_module()
            self._client = self._redis_module.Redis(
                host=self._settings.host,
                port=self._settings.port,
                db=self._settings.db,
                password=self._settings.password,
                socket_timeout=self._timeout,
                socket_connect_timeout=self._timeout,
                decode_responses=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._redis_module = None

    def execute(self, *args: str | int) -> RedisValue:
        client = self._client_or_create()
        assert self._redis_module is not None
        try:
            options: dict[str, bool] = {}
            if args and str(args[0]).upper() == "XPENDING" and len(args) > 3:
                options["parse_detail"] = True
            return client.execute_command(*args, **options)
        except self._redis_module.exceptions.ResponseError as exc:
            raise RedisCommandError(str(exc)) from exc
        except self._redis_module.exceptions.RedisError as exc:
            self.close()
            raise RedisError(f"Redis 命令失败: {args[0] if args else '<empty>'}: {exc}") from exc
        except OSError as exc:
            self.close()
            raise RedisError(f"Redis 命令失败: {args[0] if args else '<empty>'}: {exc}") from exc


def _as_int(value: object, field: str) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise RedisError(f"invalid Redis {field}: {value!r}") from exc


def _as_text(value: object) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


@dataclass(frozen=True)
class RedisStatus:
    stream_length: int
    pending: int
    group: str
    dedup_keys: int


@dataclass(frozen=True)
class RedisCleanupResult:
    stream_entries_before: int
    pending_before: int
    dedup_keys_before: int
    stream_deleted: int
    dedup_deleted: int
    dry_run: bool


@dataclass
class _CleanupProgress:
    stream_entries_before: int
    pending_before: int
    dedup_keys_before: int
    stream_deleted: int = 0
    dedup_deleted: int = 0
    dry_run: bool = False

    def result(self) -> RedisCleanupResult:
        return RedisCleanupResult(
            stream_entries_before=self.stream_entries_before,
            pending_before=self.pending_before,
            dedup_keys_before=self.dedup_keys_before,
            stream_deleted=self.stream_deleted,
            dedup_deleted=self.dedup_deleted,
            dry_run=self.dry_run,
        )


class RedisCleanupError(RedisError):
    """A cleanup command failed after some operations may have completed."""

    def __init__(self, message: str, *, result: RedisCleanupResult) -> None:
        super().__init__(message)
        self.result = result


class RedisAdmin:
    """Operations for one configured stream and the Agent dedup namespace."""

    def __init__(
        self,
        connection: RedisConnectionProtocol,
        *,
        stream_key: str,
        consumer_group: str,
        batch_size: int = 500,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._connection = connection
        self.stream_key = stream_key
        self.consumer_group = consumer_group
        self.batch_size = batch_size

    def ping(self) -> str:
        return _as_text(self._connection.execute("PING"))

    def stream_length(self) -> int:
        return _as_int(self._connection.execute("XLEN", self.stream_key), "stream length")

    def pending_total(self) -> int:
        try:
            response = self._connection.execute("XPENDING", self.stream_key, self.consumer_group)
        except RedisCommandError as exc:
            if "NOGROUP" in str(exc).upper():
                return 0
            raise
        if isinstance(response, dict):
            if "pending" not in response:
                raise RedisError("unexpected XPENDING summary response")
            return _as_int(response["pending"], "pending total")
        if not isinstance(response, (list, tuple)) or not response:
            raise RedisError("unexpected XPENDING summary response")
        return _as_int(response[0], "pending total")

    def _scan_keys(self, pattern: str) -> list[str]:
        cursor = "0"
        keys: list[str] = []
        while True:
            response = self._connection.execute(
                "SCAN",
                cursor,
                "MATCH",
                pattern,
                "COUNT",
                self.batch_size,
            )
            if not isinstance(response, (list, tuple)) or len(response) != 2:
                raise RedisError("unexpected SCAN response")
            cursor = _as_text(response[0])
            raw_keys = response[1]
            if raw_keys is None:
                raw_keys = []
            if not isinstance(raw_keys, (list, tuple)):
                raise RedisError("unexpected SCAN key list")
            keys.extend(_as_text(key) for key in raw_keys)
            if cursor == "0":
                return keys

    def dedup_key_count(self) -> int:
        return len(self._scan_keys(AGENT_DEDUP_KEY_PATTERN))

    def status(self) -> RedisStatus:
        return RedisStatus(
            stream_length=self.stream_length(),
            pending=self.pending_total(),
            group=self.consumer_group,
            dedup_keys=self.dedup_key_count(),
        )

    def reset_runtime_state(self, *, dry_run: bool = False) -> RedisCleanupResult:
        state = self.status()
        progress = _CleanupProgress(
            stream_entries_before=state.stream_length,
            pending_before=state.pending,
            dedup_keys_before=state.dedup_keys,
            dry_run=dry_run,
        )
        if dry_run:
            return progress.result()
        try:
            progress.stream_deleted = _as_int(
                self._connection.execute("DEL", self.stream_key),
                "stream delete count",
            )
            dedup_keys = self._scan_keys(AGENT_DEDUP_KEY_PATTERN)
            for start in range(0, len(dedup_keys), self.batch_size):
                batch = dedup_keys[start : start + self.batch_size]
                progress.dedup_deleted += _as_int(
                    self._connection.execute("DEL", *batch),
                    "dedup delete count",
                )
        except RedisError as exc:
            raise RedisCleanupError(str(exc), result=progress.result()) from exc
        return progress.result()


def redis_admin_from_settings(
    settings: RedisSettings,
    *,
    batch_size: int = 500,
) -> tuple[RedisConnection, RedisAdmin]:
    connection = RedisConnection(settings)
    return connection, RedisAdmin(
        connection,
        stream_key=settings.stream_key,
        consumer_group=settings.consumer_group,
        batch_size=batch_size,
    )
