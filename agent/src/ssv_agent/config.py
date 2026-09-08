from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


_CONFIG_KEYS = frozenset(
    {
        "version",
        "logging",
        "redis",
        "sources",
        "display",
        "inference",
        "tracking",
        "agent",
    }
)
_AGENT_CONFIG_KEYS = frozenset({"version", "logging", "redis", "agent"})


class _StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoggingConfig(_StrictConfigModel):
    cpp_debug_level: str = "ssv*:4"
    python_log_level: str = "INFO"


class RedisConfig(_StrictConfigModel):
    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0)
    stream_key: str = "ssv:events"
    consumer_group: str = "ssv-agent"
    reclaim_idle_ms: int = Field(default=60_000, gt=0)
    reclaim_batch_size: int = Field(default=10, ge=1, le=100)
    consumer_name: str | None = Field(default=None, min_length=1)


class WorkerConfig(_StrictConfigModel):
    """持久 worker 的领取与重试策略。"""

    enabled: bool = False
    poll_interval_ms: int = Field(default=1000, gt=0)
    lease_ms: int = Field(default=30_000, gt=0)
    max_retries: int = Field(default=3, ge=1)
    retry_delay_ms: int = Field(default=1000, ge=0)


class ReviewWorkerConfig(WorkerConfig):
    policy_id: str = "ssv-review.v1"
    model_id: str | None = None


class IndexWorkerConfig(WorkerConfig):
    embedding_backend: Literal["mock", "openai_compatible", "bge_m3"] = "mock"
    embedding_model: str | None = None


class KnowledgeConfig(_StrictConfigModel):
    """规则知识检索与 Qdrant 投影配置。"""

    backend: Literal["local_markdown", "qdrant", "mock"] = "local_markdown"
    qdrant_path: str = Field(default="data/qdrant", min_length=1)
    min_score: float = Field(default=0.5, ge=-1.0, le=1.0)


class AgentConfig(_StrictConfigModel):
    state_machine_timeout: int = Field(default=300, gt=0)
    max_retries: int = Field(default=3, ge=0)
    model_name: str | None = None
    output_dir: str = "outputs"
    evidence_roots: list[str] = Field(default_factory=list)
    dedup_enabled: bool = True
    dedup_cooldown_seconds: float = Field(default=30.0, gt=0)
    review: ReviewWorkerConfig = Field(default_factory=ReviewWorkerConfig)
    indexing: IndexWorkerConfig = Field(default_factory=IndexWorkerConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)

    @field_validator("evidence_roots")
    @classmethod
    def validate_evidence_roots(cls, roots: list[str]) -> list[str]:
        for root in roots:
            if not Path(root).is_absolute():
                raise ValueError("evidence_roots entries must be absolute paths")
        return roots


class SsvConfig(_StrictConfigModel):
    version: Literal["2.0"] = "2.0"
    logging: LoggingConfig = LoggingConfig()
    redis: RedisConfig = RedisConfig()
    agent: AgentConfig = AgentConfig()

    @model_validator(mode="before")
    @classmethod
    def validate_shared_root(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, dict):
            return value
        if info.context and info.context.get("require_version") and "version" not in value:
            raise ValueError("version is required")
        for key in value:
            if key not in _CONFIG_KEYS:
                raise ValueError(f"unknown configuration key: {key}")

        # The Agent owns only these sections; the runner validates the rest.
        return {key: item for key, item in value.items() if key in _AGENT_CONFIG_KEYS}


def _validate_loaded_config(data: object) -> SsvConfig:
    return SsvConfig.model_validate(data, context={"require_version": True})


def _apply_env_overrides(cfg: SsvConfig) -> None:
    """Override deployment-sensitive config fields from environment variables."""
    redis = cfg.redis.model_dump()
    if v := os.environ.get("REDIS_HOST"):
        redis["host"] = v
    if v := os.environ.get("REDIS_PORT"):
        redis["port"] = int(v)
    cfg.redis = RedisConfig.model_validate(redis)


def _config_search_paths() -> list[Path]:
    """返回配置搜索顺序（与 load_config 一致，缺失的显式路径跳过）。"""
    paths: list[Path] = []
    if env_path := os.environ.get("SSV_CONFIG_PATH"):
        paths.append(Path(env_path))
    paths.extend([Path("ssv.yaml"), Path("config/ssv.yaml"), Path("/etc/ssv/ssv.yaml")])
    return paths


def load_sources() -> list[dict[str, Any]]:
    """读取配置文件中的 sources 列表，供 source_list 工具使用。"""
    for path in _config_search_paths():
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            return list(data.get("sources", []))
    return []


def load_config(path: str | Path | None = None) -> SsvConfig:
    """Load configuration from YAML file.

    Search order: explicit path -> SSV_CONFIG_PATH env -> ssv.yaml -> config/ssv.yaml -> defaults.
    Environment variables REDIS_HOST and REDIS_PORT override corresponding YAML values.
    """
    cfg: SsvConfig | None = None

    if path is not None:
        p = Path(path)
        if p.exists():
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            cfg = _validate_loaded_config(data)
        else:
            raise FileNotFoundError(f"Config file not found: {p}")

    if cfg is None:
        env_path = os.environ.get("SSV_CONFIG_PATH")
        if env_path and Path(env_path).exists():
            with open(env_path) as f:
                data = yaml.safe_load(f) or {}
            cfg = _validate_loaded_config(data)

    if cfg is None:
        local = Path("ssv.yaml")
        if local.exists():
            with open(local) as f:
                data = yaml.safe_load(f) or {}
            cfg = _validate_loaded_config(data)

    if cfg is None:
        config_local = Path("config/ssv.yaml")
        if config_local.exists():
            with open(config_local) as f:
                data = yaml.safe_load(f) or {}
            cfg = _validate_loaded_config(data)

    if cfg is None:
        cfg = SsvConfig()

    _apply_env_overrides(cfg)
    return cfg
