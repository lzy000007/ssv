"""Runtime configuration needed by CLI service commands."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .context import ProjectContext
from .output import CliError

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on minimal hosts
    yaml = None


@dataclass(frozen=True)
class RedisSettings:
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    stream_key: str = "ssv:events"
    consumer_group: str = "ssv-agent"
    password: str | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    redis: RedisSettings
    event_db_path: Path
    source_path: Path | None = None
    qdrant_url: str | None = None


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CliError(f"配置字段 {name} 必须是 mapping")
    return value


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise CliError(f"配置字段 {name} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CliError(f"配置字段 {name} 必须是整数") from exc


def _validate_redis(settings: RedisSettings) -> RedisSettings:
    if not settings.host.strip():
        raise CliError("Redis host 不能为空")
    if not 1 <= settings.port <= 65535:
        raise CliError(f"Redis port 超出范围: {settings.port}")
    if settings.db < 0:
        raise CliError(f"Redis db 不能为负数: {settings.db}")
    if not settings.stream_key.strip():
        raise CliError("Redis stream_key 不能为空")
    if not settings.consumer_group.strip():
        raise CliError("Redis consumer_group 不能为空")
    return settings


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise CliError("Python CLI 读取 YAML 需要 PyYAML；请安装 PyYAML")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise CliError(f"无法读取配置文件: {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise CliError(f"配置文件 YAML 无效: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CliError(f"配置文件顶层必须是 mapping: {path}")
    return data


def load_runtime_config(
    context: ProjectContext,
    *,
    path: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    db: int | None = None,
    stream: str | None = None,
    group: str | None = None,
) -> RuntimeConfig:
    """Load Redis settings, applying explicit CLI and supported env overrides."""

    source_path = context.resolve(path) if path is not None else context.config_path
    data: dict[str, Any] = {}
    if source_path is not None:
        if not source_path.is_file():
            raise CliError(f"配置文件不存在: {source_path}")
        data = _load_yaml(source_path)

    redis_data = _as_mapping(data.get("redis"), "redis")
    settings = RedisSettings(
        host=str(redis_data.get("host", "localhost")),
        port=_as_int(redis_data.get("port", 6379), "redis.port"),
        db=_as_int(redis_data.get("db", 0), "redis.db"),
        stream_key=str(redis_data.get("stream_key", "ssv:events")),
        consumer_group=str(redis_data.get("consumer_group", "ssv-agent")),
        password=(str(redis_data["password"]) if redis_data.get("password") is not None else None),
    )

    environment = context.environment
    if environment.get("REDIS_HOST"):
        settings = replace(settings, host=environment["REDIS_HOST"])
    if environment.get("REDIS_PORT"):
        settings = replace(settings, port=_as_int(environment["REDIS_PORT"], "REDIS_PORT"))
    if environment.get("REDIS_DB"):
        settings = replace(settings, db=_as_int(environment["REDIS_DB"], "REDIS_DB"))
    if environment.get("REDIS_PASSWORD"):
        settings = replace(settings, password=environment["REDIS_PASSWORD"])

    settings = replace(
        settings,
        host=host if host is not None else settings.host,
        port=port if port is not None else settings.port,
        db=db if db is not None else settings.db,
        stream_key=stream if stream is not None else settings.stream_key,
        consumer_group=group if group is not None else settings.consumer_group,
    )
    agent_data = _as_mapping(data.get("agent"), "agent")
    event_db_value = agent_data.get("event_db_path", "data/events.db")
    if not isinstance(event_db_value, str) or not event_db_value:
        raise CliError("配置字段 agent.event_db_path 必须是非空字符串")
    event_db_path = Path(event_db_value).expanduser()
    if not event_db_path.is_absolute():
        event_db_path = context.root / "agent" / event_db_path
    knowledge_data = _as_mapping(agent_data.get("knowledge"), "agent.knowledge")
    qdrant_url = knowledge_data.get("qdrant_url")
    if qdrant_url is not None and (not isinstance(qdrant_url, str) or not qdrant_url):
        raise CliError("配置字段 agent.knowledge.qdrant_url 必须是非空字符串或 null")

    return RuntimeConfig(
        redis=_validate_redis(settings),
        event_db_path=event_db_path,
        source_path=source_path,
        qdrant_url=qdrant_url,
    )
