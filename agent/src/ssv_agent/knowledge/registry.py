"""知识后端注册表与懒加载工厂。"""

from __future__ import annotations

import importlib

from ssv_agent.knowledge.ingester import Ingester
from ssv_agent.knowledge.retriever import Retriever


_registry: dict[str, tuple[type[Retriever], type[Ingester]]] = {}
_retriever_instances: dict[str, Retriever] = {}
_ingester_instances: dict[str, Ingester] = {}
_LAZY_BACKENDS = {
    "local_markdown": "ssv_agent.knowledge.backends.local_markdown",
    "mock": "ssv_agent.knowledge.backends.mock",
    "qdrant": "ssv_agent.knowledge.backends.qdrant",
}


def register_backend(
    name: str,
    retriever_cls: type[Retriever],
    ingester_cls: type[Ingester],
) -> None:
    """注册一个知识后端（检索 + 入库）。"""
    retriever_cls.backend_name = name
    ingester_cls.backend_name = name
    _registry[name] = (retriever_cls, ingester_cls)


def _ensure_loaded(name: str) -> None:
    if name not in _registry and name in _LAZY_BACKENDS:
        importlib.import_module(_LAZY_BACKENDS[name])
    if name not in _registry:
        raise ValueError(f"Unknown knowledge backend: {name!r}")


def get_retriever(name: str) -> Retriever:
    """按名称返回单例 retriever。"""
    _ensure_loaded(name)
    if name not in _retriever_instances:
        retriever_cls, _ = _registry[name]
        _retriever_instances[name] = retriever_cls()
    return _retriever_instances[name]


def get_ingester(name: str) -> Ingester:
    """按名称返回单例 ingester。"""
    _ensure_loaded(name)
    if name not in _ingester_instances:
        _, ingester_cls = _registry[name]
        _ingester_instances[name] = ingester_cls()
    return _ingester_instances[name]
