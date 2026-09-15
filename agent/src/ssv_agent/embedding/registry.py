"""Embedding provider 注册表与懒加载工厂。"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass

from ssv_agent.embedding.provider import EmbeddingProvider


EMBEDDING_IDENTITY_SCHEMA_VERSION = 1
MOCK_EMBEDDING_ALGORITHM = "sha256-normalized-v1"
MOCK_EMBEDDING_DIMENSIONS = 64
MOCK_EMBEDDING_MODEL = "mock-sha256-normalized-v1-d64"


@dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    """描述会影响向量兼容性的 embedding 身份。"""

    schema_version: int
    backend: str
    model: str
    algorithm: str | None = None
    dimensions: int | None = None
    endpoint: str | None = None

    @property
    def adapter_identity_schema_version(self) -> int:
        """返回兼容性身份 schema 版本的长名称别名。"""
        return self.schema_version

    def as_dict(self) -> dict[str, object]:
        """返回用于哈希与测试断言的稳定字段。"""
        identity: dict[str, object] = {
            "schema_version": self.schema_version,
            "backend": self.backend,
            "model": self.model,
        }
        if self.algorithm is not None:
            identity["algorithm"] = self.algorithm
        if self.dimensions is not None:
            identity["dimensions"] = self.dimensions
        if self.endpoint is not None:
            identity["endpoint"] = self.endpoint
        return identity


_registry: dict[str, type[EmbeddingProvider]] = {}
_instances: dict[str, EmbeddingProvider] = {}
_configured_instances: dict[tuple[str, str | None, str | None], EmbeddingProvider] = {}
_LAZY_BACKENDS = {
    "bge_m3": "ssv_agent.embedding.backends.bge_m3",
    "mock": "ssv_agent.embedding.backends.mock",
    "openai_compatible": "ssv_agent.embedding.backends.openai_compatible",
}
_MODEL_ARGUMENTS = {
    "bge_m3": "model_name",
    "openai_compatible": "model",
}


def _resolve_model(backend: str, model: str | None) -> str | None:
    """解析 provider 的有效模型身份，保证环境切换不会命中旧缓存。"""
    if backend == "mock":
        return MOCK_EMBEDDING_MODEL
    if model:
        return model
    if configured := os.getenv("SSV_EMBEDDING_MODEL"):
        return configured
    if backend == "bge_m3":
        # 兼容早期变量；新配置统一使用 SSV_EMBEDDING_MODEL。
        return os.getenv("SSV_BGE_M3_MODEL") or "BAAI/bge-m3"
    if backend == "openai_compatible":
        return "text-embedding-3-small"
    return None


def resolve_embedding_identity(
    backend: str | None = None,
    model: str | None = None,
) -> EmbeddingIdentity:
    """解析 embedding 的稳定身份；未显式传参时读取 SSV 环境配置。"""
    effective_backend = backend or os.getenv("SSV_EMBEDDING_BACKEND", "mock") or "mock"
    effective_model = _resolve_model(effective_backend, model) or "default"
    if effective_backend == "mock":
        return EmbeddingIdentity(
            schema_version=EMBEDDING_IDENTITY_SCHEMA_VERSION,
            backend=effective_backend,
            model=MOCK_EMBEDDING_MODEL,
            algorithm=MOCK_EMBEDDING_ALGORITHM,
            dimensions=MOCK_EMBEDDING_DIMENSIONS,
        )
    endpoint = os.getenv("SSV_EMBEDDING_BASE_URL") if effective_backend == "openai_compatible" else None
    return EmbeddingIdentity(
        schema_version=EMBEDDING_IDENTITY_SCHEMA_VERSION,
        backend=effective_backend,
        model=effective_model,
        endpoint=endpoint,
    )


def register_backend(name: str, provider_cls: type[EmbeddingProvider]) -> None:
    """注册一个 embedding 后端类。"""
    provider_cls.backend_name = name
    _registry[name] = provider_cls


def _ensure_loaded(name: str) -> None:
    if name not in _registry and name in _LAZY_BACKENDS:
        importlib.import_module(_LAZY_BACKENDS[name])
    if name not in _registry:
        raise ValueError(f"Unknown embedding backend: {name!r}")


def get_provider(name: str) -> EmbeddingProvider:
    """按名称返回单例 provider。"""
    _ensure_loaded(name)
    if name not in _instances:
        _instances[name] = create_provider(name)
    return _instances[name]


def get_configured_provider(
    backend: str,
    model: str | None = None,
) -> EmbeddingProvider:
    """按 backend 与模型身份返回缓存 provider。"""
    _ensure_loaded(backend)
    model_argument = _MODEL_ARGUMENTS.get(backend)
    cache_model = _resolve_model(backend, model) if model_argument is not None else None
    cache_endpoint = (
        os.getenv("SSV_EMBEDDING_BASE_URL")
        if backend == "openai_compatible"
        else None
    )
    cache_key = (backend, cache_model, cache_endpoint)
    if cache_key not in _configured_instances:
        kwargs = {model_argument: cache_model} if model_argument and cache_model else {}
        _configured_instances[cache_key] = create_provider(backend, **kwargs)
    return _configured_instances[cache_key]


def create_provider(name: str, **kwargs: object) -> EmbeddingProvider:
    """按显式配置创建独立 provider，避免把模型路径藏入全局单例。"""
    _ensure_loaded(name)
    return _registry[name](**kwargs)
