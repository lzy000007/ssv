from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest

from ssv_agent.embedding.registry import (
    MOCK_EMBEDDING_ALGORITHM,
    MOCK_EMBEDDING_DIMENSIONS,
    MOCK_EMBEDDING_MODEL,
    EmbeddingIdentity,
    get_configured_provider,
    get_provider,
    resolve_embedding_identity,
)


def test_embedding_identity_uses_effective_backend_defaults(monkeypatch) -> None:
    monkeypatch.delenv("SSV_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("SSV_BGE_M3_MODEL", raising=False)

    bge = resolve_embedding_identity("bge_m3")
    openai = resolve_embedding_identity("openai_compatible")

    assert isinstance(bge, EmbeddingIdentity)
    assert bge.schema_version == 1
    assert bge.backend == "bge_m3"
    assert bge.model == "BAAI/bge-m3"
    assert openai.backend == "openai_compatible"
    assert openai.model == "text-embedding-3-small"


def test_openai_embedding_identity_includes_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("SSV_EMBEDDING_MODEL", raising=False)
    monkeypatch.setenv("SSV_EMBEDDING_BASE_URL", "https://one.example/v1")
    first = resolve_embedding_identity("openai_compatible")

    monkeypatch.setenv("SSV_EMBEDDING_BASE_URL", "https://two.example/v1")
    second = resolve_embedding_identity("openai_compatible")

    assert first.endpoint == "https://one.example/v1"
    assert second.endpoint == "https://two.example/v1"
    assert first != second


def test_mapping_embedding_identity_preserves_endpoint() -> None:
    from ssv_agent.event_store.qdrant_store import derive_physical_collection_name

    common = {"schema_version": 1, "backend": "openai_compatible", "model": "model"}
    first = derive_physical_collection_name(
        "rules",
        {**common, "endpoint": "https://one.example/v1"},
    )
    second = derive_physical_collection_name(
        "rules",
        {**common, "endpoint": "https://two.example/v1"},
    )

    assert first != second


def test_mock_embedding_identity_is_versioned_and_ignores_model_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SSV_EMBEDDING_MODEL", "/opt/models/invalid-for-mock")

    from_environment = resolve_embedding_identity("mock")
    from_argument = resolve_embedding_identity("mock", "/another/invalid-model")

    assert from_environment == from_argument
    assert from_environment.model == MOCK_EMBEDDING_MODEL
    assert from_environment.algorithm == MOCK_EMBEDDING_ALGORITHM
    assert from_environment.dimensions == MOCK_EMBEDDING_DIMENSIONS


def test_bge_m3_provider_defers_model_load_until_embedding() -> None:
    from ssv_agent.embedding.backends.bge_m3 import BgeM3EmbeddingProvider

    loaded: list[str] = []

    class FakeModel:
        def encode(self, texts, normalize_embeddings: bool):
            assert normalize_embeddings is True
            return [[0.3, 0.4] for _ in texts]

    provider = BgeM3EmbeddingProvider(
        model_name="/opt/models/bge-m3",
        model_factory=lambda model_name: loaded.append(model_name) or FakeModel(),
    )

    assert loaded == []
    vectors = asyncio.run(provider.embed_texts(["安全帽"]))

    assert loaded == ["/opt/models/bge-m3"]
    assert vectors == [[0.3, 0.4]]
    assert asyncio.run(provider.embed_query("谁没戴安全帽")) == [0.3, 0.4]


def test_bge_m3_registry_lookup_does_not_download_a_model() -> None:
    provider = get_provider("bge_m3")

    assert provider.backend_name == "bge_m3"


def test_configured_bge_provider_cache_is_model_aware_and_lazy(monkeypatch) -> None:
    from ssv_agent.embedding.backends import bge_m3

    loaded: list[tuple[str, dict[str, object]]] = []

    class FakeModel:
        def __init__(self, model_name: str, **kwargs: object) -> None:
            loaded.append((model_name, kwargs))

        def encode(self, texts, normalize_embeddings: bool):
            assert normalize_embeddings is True
            return [[0.3, 0.4] for _ in texts]

    monkeypatch.setattr(
        bge_m3.importlib,
        "import_module",
        lambda _name: SimpleNamespace(SentenceTransformer=FakeModel),
    )

    first = get_configured_provider("bge_m3", "/models/bge-m3-a")
    same = get_configured_provider("bge_m3", "/models/bge-m3-a")
    second = get_configured_provider("bge_m3", "/models/bge-m3-b")

    assert first is same
    assert first is not second
    assert loaded == []

    assert asyncio.run(first.embed_query("first")) == [0.3, 0.4]
    assert asyncio.run(second.embed_query("second")) == [0.3, 0.4]
    assert loaded == [
        ("/models/bge-m3-a", {"local_files_only": True}),
        ("/models/bge-m3-b", {"local_files_only": True}),
    ]


def test_configured_bge_provider_tracks_shared_model_environment(monkeypatch) -> None:
    monkeypatch.delenv("SSV_BGE_M3_MODEL", raising=False)
    monkeypatch.setenv("SSV_EMBEDDING_MODEL", "/models/bge-m3-env-a")
    first = get_configured_provider("bge_m3")

    monkeypatch.setenv("SSV_EMBEDDING_MODEL", "/models/bge-m3-env-b")
    second = get_configured_provider("bge_m3")

    assert first is not second
    assert first._model_name == "/models/bge-m3-env-a"
    assert second._model_name == "/models/bge-m3-env-b"


def test_bge_m3_allows_explicit_online_loading_override(monkeypatch) -> None:
    from ssv_agent.embedding.backends import bge_m3

    loaded: list[dict[str, object]] = []

    class FakeModel:
        def __init__(self, _model_name: str, **kwargs: object) -> None:
            loaded.append(kwargs)

        def encode(self, texts, normalize_embeddings: bool):
            return [[0.3, 0.4] for _ in texts]

    monkeypatch.setattr(
        bge_m3.importlib,
        "import_module",
        lambda _name: SimpleNamespace(SentenceTransformer=FakeModel),
    )
    provider = bge_m3.BgeM3EmbeddingProvider(
        model_name="remote-model",
        local_files_only=False,
    )

    assert loaded == []
    asyncio.run(provider.embed_query("query"))

    assert loaded == [{"local_files_only": False}]


def test_configured_openai_provider_maps_model(monkeypatch) -> None:
    from ssv_agent.embedding.backends import openai_compatible

    calls: list[tuple[str, list[str]]] = []

    class FakeEmbeddings:
        async def create(self, *, model: str, input: list[str], extra_body=None):
            calls.append((model, input, extra_body))
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in input]
            )

    class FakeClient:
        embeddings = FakeEmbeddings()

    monkeypatch.setattr(
        openai_compatible,
        "AsyncOpenAI",
        lambda **_kwargs: FakeClient(),
    )

    provider = get_configured_provider("openai_compatible", "embedding-model-v2")

    assert asyncio.run(provider.embed_query("query")) == [0.1, 0.2]
    assert calls == [
        ("embedding-model-v2", ["query"], {"text_type": "query"}),
    ]


def test_configured_mock_provider_does_not_receive_model_parameter() -> None:
    provider = get_configured_provider("mock", "ignored-model")

    assert len(asyncio.run(provider.embed_query("query"))) == 64


def test_mock_provider_is_deterministic() -> None:
    provider = get_provider("mock")

    vectors = asyncio.run(provider.embed_texts(["安全帽", "安全帽"]))
    assert vectors[0] == vectors[1]
    assert len(vectors[0]) == 64

    query = asyncio.run(provider.embed_query("谁没戴安全帽"))
    assert len(query) == 64
    norm = math.sqrt(sum(value * value for value in query))
    assert abs(norm - 1.0) < 1e-6


def test_mock_provider_shared_singleton() -> None:
    assert get_provider("mock") is get_provider("mock")


def test_unknown_backend_raises() -> None:
    with pytest.raises(ValueError):
        get_provider("not-exist")
