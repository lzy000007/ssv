from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Self

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
        "evidence_cache",
        "agent",
    }
)
_AGENT_CONFIG_KEYS = frozenset(
    {"version", "logging", "redis", "sources", "evidence_cache", "agent"}
)

_DEFAULT_EVIDENCE_DIRECTORY = "/var/lib/ssv/evidence-cache"


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


class PollingWorkerConfig(_StrictConfigModel):
    """持久轮询 worker 的领取与重试策略。"""

    poll_interval_ms: int = Field(default=1000, gt=0)
    lease_ms: int = Field(default=30_000, gt=0)
    max_retries: int = Field(default=3, ge=1)
    retry_delay_ms: int = Field(default=1000, ge=0)


class WorkerConfig(PollingWorkerConfig):
    """可按部署需要启停的持久 worker 配置。"""

    enabled: bool = False


class ReviewWorkerConfig(WorkerConfig):
    policy_id: str = "ssv-review.v1"
    model_id: str | None = None
    recursion_limit: int = Field(default=256, ge=1)


class IndexWorkerConfig(WorkerConfig):
    embedding_backend: Literal["mock", "openai_compatible", "bge_m3"] = "mock"
    embedding_model: str | None = None
    embedding_base_url: str | None = Field(default=None, min_length=1)
    query_text_type: str | None = None


class ReportWorkerConfig(WorkerConfig):
    """把已提交的复核结果整理为可追溯报告。"""

    enabled: bool = True


class AgentSourceConfig(_StrictConfigModel):
    """Agent 所需的 source 身份和录像路径来源。"""

    id: str = Field(min_length=1)
    uri: str = Field(min_length=1)


class AgentWebConfig(_StrictConfigModel):
    """独立只读 Web API 的监听地址。"""

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8765, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("agent.web.host must not be blank")
        return value


class RecordingEvidenceConfig(PollingWorkerConfig):
    """由 Agent 持久 worker 读取 SSV cache 并生成上下文证据的配置。"""

    retry_delay_ms: int = Field(default=2_000, ge=0)
    clip_before_ms: int = Field(default=2500, ge=1000)
    clip_after_ms: int = Field(default=2500, ge=1000)
    merge_gap_ms: int = Field(default=3_000, gt=0)
    lost_grace_ms: int = Field(default=3_000, ge=0)
    silence_timeout_ms: int = Field(default=30_000, gt=0)
    max_episode_ms: int = Field(default=30_000, gt=0)

    @model_validator(mode="after")
    def validate_episode_window(self) -> Self:
        if self.max_episode_ms <= self.merge_gap_ms:
            raise ValueError("recording_evidence.max_episode_ms must exceed merge_gap_ms")
        return self


class KnowledgeConfig(_StrictConfigModel):
    """规则知识检索与 Qdrant 投影配置。"""

    backend: Literal["local_markdown", "qdrant", "mock"] = "local_markdown"
    rules_dir: str = Field(default="knowledge/rules", min_length=1)
    qdrant_path: str = Field(default="data/qdrant", min_length=1)
    qdrant_url: str | None = Field(default=None, min_length=1)
    min_score: float = Field(default=0.5, ge=-1.0, le=1.0)

    @field_validator("rules_dir")
    @classmethod
    def validate_rules_dir(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("agent.knowledge.rules_dir must not be blank")
        return value


class EvidenceCacheConfig(_StrictConfigModel):
    """由 GStreamer 生成、供 Agent 取证的短时循环缓存配置。"""

    directory: str = _DEFAULT_EVIDENCE_DIRECTORY
    segment_duration_ms: int = Field(default=10_000, ge=1_000)
    retention_ms: int = Field(default=120_000, ge=1_000)
    max_bytes_mb: int = Field(default=512, ge=1)

    @field_validator("directory")
    @classmethod
    def validate_directory(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("evidence_cache.directory must be an absolute path")
        return value

    @model_validator(mode="after")
    def validate_retention(self) -> Self:
        if self.retention_ms < self.segment_duration_ms:
            raise ValueError(
                "evidence_cache.retention_ms must not be shorter than segment_duration_ms"
            )
        return self


class AgentConfig(_StrictConfigModel):
    state_machine_timeout: int = Field(default=300, gt=0)
    max_retries: int = Field(default=3, ge=0)
    model_name: str | None = None
    event_db_path: str = Field(default="data/events.db", min_length=1)
    output_dir: str = Field(default="outputs", min_length=1)
    evidence_roots: list[str] = Field(default_factory=lambda: [_DEFAULT_EVIDENCE_DIRECTORY])
    web: AgentWebConfig = Field(default_factory=AgentWebConfig)
    dedup_enabled: bool = True
    dedup_cooldown_seconds: float = Field(default=30.0, gt=0)
    review: ReviewWorkerConfig = Field(default_factory=ReviewWorkerConfig)
    indexing: IndexWorkerConfig = Field(default_factory=IndexWorkerConfig)
    reporting: ReportWorkerConfig = Field(default_factory=ReportWorkerConfig)
    recording_evidence: RecordingEvidenceConfig = Field(default_factory=RecordingEvidenceConfig)
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
    sources: list[AgentSourceConfig] = Field(default_factory=list)
    evidence_cache: EvidenceCacheConfig = Field(default_factory=EvidenceCacheConfig)
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

        # The Agent owns only these sections. Runner-only source fields are not part of
        # the Agent config contract, so retain only source identity and RTSP URI.
        agent_config = {key: item for key, item in value.items() if key in _AGENT_CONFIG_KEYS}
        sources = value.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and "wvp_channel_id" in source:
                    raise ValueError("unknown configuration key: sources[].wvp_channel_id")
            agent_config["sources"] = [
                {key: source.get(key) for key in ("id", "uri")}
                if isinstance(source, dict)
                else source
                for source in sources
            ]
        return agent_config

    @model_validator(mode="after")
    def validate_recording_evidence_cache(self) -> Self:
        cache_directory = Path(self.evidence_cache.directory).resolve(strict=False)
        if not self.agent.evidence_roots:
            default_root = Path(_DEFAULT_EVIDENCE_DIRECTORY).resolve(strict=False)
            if cache_directory == default_root or default_root in cache_directory.parents:
                return self
            raise ValueError(
                "custom evidence_cache.directory requires matching agent.evidence_roots"
            )
        allowed = any(
            cache_directory == root or root in cache_directory.parents
            for root in (Path(item).resolve(strict=False) for item in self.agent.evidence_roots)
        )
        if not allowed:
            raise ValueError(
                "recording_evidence requires evidence_cache.directory inside evidence_roots"
            )
        return self

    @model_validator(mode="after")
    def validate_source_ids(self) -> Self:
        source_ids = [source.id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("sources[].id values must be unique")
        return self


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
            with open(path, encoding="utf-8-sig") as f:
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
            with open(p, encoding="utf-8-sig") as f:
                data = yaml.safe_load(f) or {}
            cfg = _validate_loaded_config(data)
        else:
            raise FileNotFoundError(f"Config file not found: {p}")

    if cfg is None:
        env_path = os.environ.get("SSV_CONFIG_PATH")
        if env_path and Path(env_path).exists():
            with open(env_path, encoding="utf-8-sig") as f:
                data = yaml.safe_load(f) or {}
            cfg = _validate_loaded_config(data)

    if cfg is None:
        local = Path("ssv.yaml")
        if local.exists():
            with open(local, encoding="utf-8-sig") as f:
                data = yaml.safe_load(f) or {}
            cfg = _validate_loaded_config(data)

    if cfg is None:
        config_local = Path("config/ssv.yaml")
        if config_local.exists():
            with open(config_local, encoding="utf-8-sig") as f:
                data = yaml.safe_load(f) or {}
            cfg = _validate_loaded_config(data)

    if cfg is None:
        cfg = SsvConfig()

    _apply_env_overrides(cfg)
    return cfg
