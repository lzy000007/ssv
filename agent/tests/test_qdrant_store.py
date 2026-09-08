from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import ssv_agent.event_store.qdrant_store as qdrant_module
from ssv_agent.event_store.qdrant_store import (
    DEFAULT_EVENT_COLLECTION,
    DEFAULT_RULE_COLLECTION,
    SsvQdrantStore,
    derive_physical_collection_name,
)
from ssv_agent.embedding.registry import resolve_embedding_identity


class RecordingQdrantClient:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        self.calls.append(kwargs)

    def close(self) -> None:
        return None


def test_server_mode_reads_url_and_api_key_from_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    RecordingQdrantClient.calls.clear()
    monkeypatch.setattr(qdrant_module, "QdrantClient", RecordingQdrantClient)
    monkeypatch.setenv("SSV_QDRANT_URL", "https://qdrant.example.test")
    monkeypatch.setenv("SSV_QDRANT_API_KEY", "environment-secret")
    monkeypatch.setenv("SSV_QDRANT_PATH", str(tmp_path / "unused"))

    with SsvQdrantStore():
        pass

    assert RecordingQdrantClient.calls == [
        {
            "url": "https://qdrant.example.test",
            "api_key": "environment-secret",
        }
    ]


def test_explicit_local_path_wins_over_environment_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    RecordingQdrantClient.calls.clear()
    monkeypatch.setattr(qdrant_module, "QdrantClient", RecordingQdrantClient)
    monkeypatch.setenv("SSV_QDRANT_URL", "https://qdrant.example.test")
    local_path = tmp_path / "explicit-local"

    with SsvQdrantStore(local_path):
        pass

    assert RecordingQdrantClient.calls == [{"path": str(local_path)}]


def test_explicit_url_has_highest_precedence(tmp_path: Path, monkeypatch) -> None:
    RecordingQdrantClient.calls.clear()
    monkeypatch.setattr(qdrant_module, "QdrantClient", RecordingQdrantClient)
    monkeypatch.setenv("SSV_QDRANT_URL", "https://environment.example.test")
    monkeypatch.setenv("SSV_QDRANT_API_KEY", "environment-secret")

    with SsvQdrantStore(
        tmp_path / "unused",
        url="https://explicit.example.test",
        api_key="explicit-secret",
    ):
        pass

    assert RecordingQdrantClient.calls == [
        {"url": "https://explicit.example.test", "api_key": "explicit-secret"}
    ]


def test_ensure_collections_is_idempotent(tmp_path: Path) -> None:
    with SsvQdrantStore(tmp_path / "qdrant") as store:
        store.ensure_collections(64)
        store.ensure_collections(64)

        assert store.event_collection != DEFAULT_EVENT_COLLECTION
        assert store.rule_collection != DEFAULT_RULE_COLLECTION
        assert store._client.collection_exists(store.event_collection)
        assert store._client.collection_exists(store.rule_collection)
        assert not store._client.collection_exists(DEFAULT_EVENT_COLLECTION)
        assert not store._client.collection_exists(DEFAULT_RULE_COLLECTION)


def test_same_embedding_identity_has_stable_physical_collections(tmp_path: Path) -> None:
    identity = resolve_embedding_identity("bge_m3", "/models/bge-m3")

    with SsvQdrantStore(tmp_path / "first", embedding_identity=identity) as first:
        with SsvQdrantStore(tmp_path / "second", embedding_identity=identity) as second:
            assert first.event_collection == second.event_collection
            assert first.rule_collection == second.rule_collection
            assert "/models/bge-m3" not in first.event_collection
            assert "/models/bge-m3" not in first.rule_collection


def test_default_identity_uses_the_registry_environment_resolver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SSV_EMBEDDING_BACKEND", "bge_m3")
    monkeypatch.setenv("SSV_EMBEDDING_MODEL", "/models/bge-m3-env")
    expected = resolve_embedding_identity()

    with SsvQdrantStore(tmp_path / "qdrant") as store:
        assert store.embedding_identity == expected
        assert store.event_collection == derive_physical_collection_name(
            DEFAULT_EVENT_COLLECTION,
            expected,
        )


def test_different_embedding_models_isolate_event_and_rule_collections(
    tmp_path: Path,
) -> None:
    first_identity = resolve_embedding_identity("bge_m3", "/models/bge-m3-a")
    second_identity = resolve_embedding_identity("bge_m3", "/models/bge-m3-b")

    with SsvQdrantStore(tmp_path / "qdrant", embedding_identity=first_identity) as first:
        first.upsert_event_vector("same-event", [0.1] * 2)
        first.upsert_rule_vector("same-rule", [0.2] * 2)
        first_event_collection = first.event_collection
        first_rule_collection = first.rule_collection

    with SsvQdrantStore(tmp_path / "qdrant", embedding_identity=second_identity) as second:
        assert second.event_collection != first_event_collection
        assert second.rule_collection != first_rule_collection
        assert second.search_events([0.1] * 2) == []
        assert second.search_rules([0.2] * 2) == []


def test_derived_collection_name_is_deterministic() -> None:
    identity = resolve_embedding_identity("mock", "/ignored")

    assert derive_physical_collection_name(DEFAULT_EVENT_COLLECTION, identity) == (
        derive_physical_collection_name(DEFAULT_EVENT_COLLECTION, identity)
    )


def test_ensure_collections_rejects_existing_dimension_mismatch(
    tmp_path: Path,
) -> None:
    with SsvQdrantStore(tmp_path / "qdrant") as store:
        store.ensure_collections(64)

        with pytest.raises(
            ValueError,
            match=r"ssv_events.*expected=32.*actual=64",
        ):
            store.ensure_collections(32)


def test_ensure_collections_rejects_named_vectors(tmp_path: Path) -> None:
    qdrant_path = tmp_path / "qdrant"
    identity = resolve_embedding_identity("mock")
    event_collection = derive_physical_collection_name(DEFAULT_EVENT_COLLECTION, identity)
    client = qdrant_module.QdrantClient(path=str(qdrant_path))
    client.create_collection(
        collection_name=event_collection,
        vectors_config={
            "dense": qdrant_module.models.VectorParams(
                size=64,
                distance=qdrant_module.models.Distance.COSINE,
            )
        },
    )
    client.close()

    with SsvQdrantStore(qdrant_path, embedding_identity=identity) as store:
        with pytest.raises(
            ValueError,
            match=r"ssv_events.*named vectors",
        ):
            store.ensure_collections(64)


def test_upsert_and_search_event(tmp_path: Path) -> None:
    with SsvQdrantStore(tmp_path / "qdrant") as store:
        store.upsert_event_vector(
            "1-0",
            [0.1] * 64,
            {"source": "camera-1", "timestamp_ms": 1000},
        )

        hits = store.search_events(
            [0.1] * 64,
            top_k=5,
            filters={"source": "camera-1"},
        )
        assert len(hits) == 1
        assert hits[0]["event_id"] == "1-0"
        assert hits[0]["source"] == "camera-1"

        hits = store.search_events([0.1] * 64, filters={"source": "camera-2"})
        assert hits == []


def test_search_hit_identity_and_score_cannot_be_overridden_by_payload(
    tmp_path: Path,
) -> None:
    with SsvQdrantStore(tmp_path / "qdrant") as store:
        store.upsert_event_vector(
            "event-1",
            [0.1] * 64,
            {"id": "forged-id", "score": -123.0},
        )

        hit = store.search_events([0.1] * 64, top_k=1)[0]

    assert hit["id"] != "forged-id"
    assert hit["score"] > 0.0
    assert hit["event_id"] == "event-1"


def test_search_accepts_hit_without_payload(monkeypatch) -> None:
    class PayloadlessClient:
        def collection_exists(self, _name: str) -> bool:
            return True

        def get_collection(self, _name: str):
            return SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(
                        vectors=qdrant_module.models.VectorParams(
                            size=2,
                            distance=qdrant_module.models.Distance.COSINE,
                        )
                    )
                )
            )

        def query_points(self, **_kwargs: object):
            return SimpleNamespace(
                points=[SimpleNamespace(id=7, score=0.75, payload=None)]
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        qdrant_module,
        "QdrantClient",
        lambda **_kwargs: PayloadlessClient(),
    )

    with SsvQdrantStore(url="https://qdrant.example.test") as store:
        hits = store.search_events([0.1, 0.2], top_k=1)

    assert hits == [{"id": 7, "score": 0.75}]


def test_upsert_and_search_rule(tmp_path: Path) -> None:
    with SsvQdrantStore(tmp_path / "qdrant") as store:
        store.upsert_rule_vector(
            "rule-001:1",
            [0.2] * 64,
            {"rule_id": "helmet-001", "source": "安全管理制度 V2", "section": "总则"},
        )

        hits = store.search_rules([0.2] * 64, top_k=5)
        assert len(hits) == 1
        assert hits[0]["chunk_id"] == "rule-001:1"
        assert hits[0]["rule_id"] == "helmet-001"


def test_search_does_not_create_a_missing_collection(tmp_path: Path) -> None:
    with SsvQdrantStore(tmp_path / "qdrant") as store:
        assert store.search_rules([0.2] * 64, top_k=1) == []
        assert not store.collection_exists(store.rule_collection)


def test_replace_rule_vectors_removes_stale_points_and_can_clear_index(
    tmp_path: Path,
) -> None:
    with SsvQdrantStore(tmp_path / "qdrant") as store:
        store.replace_rule_vectors(
            [
                ("rule-old", [0.1] * 64, {"source": "rules", "content": "old"}),
                ("rule-keep", [0.2] * 64, {"source": "rules", "content": "keep"}),
            ]
        )
        store.replace_rule_vectors(
            [
                ("rule-keep", [0.2] * 64, {"source": "rules", "content": "keep"}),
                ("rule-new", [0.3] * 64, {"source": "rules", "content": "new"}),
            ]
        )

        assert store.count_points(store.rule_collection) == 2
        hits = store.search_rules([0.2] * 64, top_k=10)
        assert {hit["chunk_id"] for hit in hits} == {"rule-keep", "rule-new"}

        store.replace_rule_vectors([])
        assert store.count_points(store.rule_collection) == 0
        assert store.search_rules([0.2] * 64, top_k=10) == []
