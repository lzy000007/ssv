"""基于 Qdrant 的向量存储。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from qdrant_client import QdrantClient, models

from ssv_agent.embedding.registry import EmbeddingIdentity, resolve_embedding_identity


DEFAULT_EVENT_COLLECTION = "ssv_events"
DEFAULT_RULE_COLLECTION = "ssv_rules"
_SSV_NAMESPACE = uuid.UUID("9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d")
EmbeddingIdentityInput = EmbeddingIdentity | Mapping[str, object]


def _default_qdrant_path() -> str:
    """返回默认 Qdrant 本地路径：环境变量优先，其次 Agent/data/qdrant。"""
    configured = os.getenv("SSV_QDRANT_PATH")
    if configured:
        return configured
    return str(Path(__file__).resolve().parents[3] / "data" / "qdrant")


def _build_filter(filters: dict[str, Any] | None) -> models.Filter | None:
    """把简单的字段等值条件转成 Qdrant Filter。"""
    if not filters:
        return None
    conditions: list[models.FieldCondition] = [
        models.FieldCondition(key=key, match=models.MatchValue(value=value))
        for key, value in filters.items()
    ]
    return models.Filter(must=cast(Any, conditions))


def _point_id(domain_id: str) -> str:
    """把业务 id 映射为确定性 UUID（Qdrant 本地模式要求 UUID 或整数）。"""
    return str(uuid.uuid5(_SSV_NAMESPACE, domain_id))


def _coerce_embedding_identity(identity: EmbeddingIdentityInput) -> EmbeddingIdentity:
    if isinstance(identity, EmbeddingIdentity):
        return identity
    if not isinstance(identity, Mapping):
        raise TypeError("embedding_identity must be an EmbeddingIdentity or mapping")

    schema_version = identity.get(
        "schema_version",
        identity.get("adapter_identity_schema_version"),
    )
    backend = identity.get("backend")
    model = identity.get("model")
    if not isinstance(schema_version, int):
        raise ValueError("embedding_identity requires an integer schema_version")
    if not isinstance(backend, str) or not backend:
        raise ValueError("embedding_identity requires a non-empty backend")
    if not isinstance(model, str) or not model:
        raise ValueError("embedding_identity requires a non-empty model")

    algorithm = identity.get("algorithm")
    dimensions = identity.get("dimensions")
    endpoint = identity.get("endpoint")
    if algorithm is not None and not isinstance(algorithm, str):
        raise ValueError("embedding_identity algorithm must be a string")
    if dimensions is not None and (
        not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0
    ):
        raise ValueError("embedding_identity dimensions must be a positive integer")
    if endpoint is not None and not isinstance(endpoint, str):
        raise ValueError("embedding_identity endpoint must be a string")
    return EmbeddingIdentity(
        schema_version=schema_version,
        backend=backend,
        model=model,
        algorithm=algorithm,
        dimensions=dimensions,
        endpoint=endpoint,
    )


def derive_physical_collection_name(
    base_collection: str,
    embedding_identity: EmbeddingIdentityInput,
) -> str:
    """由逻辑 collection 名与 embedding 身份派生安全、稳定的物理名。"""
    identity = _coerce_embedding_identity(embedding_identity)
    encoded_identity = json.dumps(
        identity.as_dict(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity_hash = hashlib.sha256(encoded_identity).hexdigest()[:16]
    return f"{base_collection}__embedding_{identity_hash}"


class SsvQdrantStore:
    """事件与规则向量的 Qdrant 读写；本地嵌入模式起步，可切服务模式。"""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        url: str | None = None,
        api_key: str | None = None,
        event_collection: str = DEFAULT_EVENT_COLLECTION,
        rule_collection: str = DEFAULT_RULE_COLLECTION,
        embedding_identity: EmbeddingIdentityInput | None = None,
    ) -> None:
        self._embedding_identity = _coerce_embedding_identity(
            embedding_identity
            if embedding_identity is not None
            else resolve_embedding_identity()
        )
        self._event_collection = derive_physical_collection_name(
            event_collection,
            self._embedding_identity,
        )
        self._rule_collection = derive_physical_collection_name(
            rule_collection,
            self._embedding_identity,
        )
        environment_url = os.getenv("SSV_QDRANT_URL")
        resolved_api_key = (
            api_key if api_key is not None else os.getenv("SSV_QDRANT_API_KEY")
        )
        if url is not None:
            self._client = QdrantClient(url=url, api_key=resolved_api_key)
        elif path is not None:
            local_path = Path(path)
            local_path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(local_path))
        elif environment_url:
            self._client = QdrantClient(
                url=environment_url,
                api_key=resolved_api_key,
            )
        else:
            local_path = Path(_default_qdrant_path())
            local_path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(local_path))

    @property
    def event_collection(self) -> str:
        return self._event_collection

    @property
    def rule_collection(self) -> str:
        return self._rule_collection

    @property
    def embedding_identity(self) -> EmbeddingIdentity:
        return self._embedding_identity

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SsvQdrantStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ensure_collections(self, vector_size: int) -> None:
        """幂等创建事件与规则集合。"""
        for name in (self._event_collection, self._rule_collection):
            self.ensure_collection(name, vector_size)

    def ensure_collection(self, collection_name: str, vector_size: int) -> None:
        """幂等创建单个 collection，并检查向量维度。"""
        if self._client.collection_exists(collection_name):
            actual_size = self._existing_vector_size(collection_name)
            if actual_size != vector_size:
                raise ValueError(
                    f"Qdrant collection {collection_name!r} vector size mismatch: "
                    f"expected={vector_size}, actual={actual_size}"
                )
            return
        self._client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=vector_size,
                distance=models.Distance.COSINE,
            ),
        )

    def collection_exists(self, collection_name: str) -> bool:
        """返回 collection 是否已创建。"""
        return self._client.collection_exists(collection_name)

    def count_points(self, collection_name: str) -> int:
        """返回 collection 当前 point 数量；不存在时返回 0。"""
        if not self.collection_exists(collection_name):
            return 0
        info = self._client.get_collection(collection_name)
        return int(info.points_count or 0)

    def _existing_vector_size(self, collection_name: str) -> int:
        collection = self._client.get_collection(collection_name)
        vectors = collection.config.params.vectors
        if isinstance(vectors, dict):
            raise ValueError(
                f"Qdrant collection {collection_name!r} uses named vectors; "
                "expected a single unnamed vector"
            )
        if not isinstance(vectors, models.VectorParams):
            raise ValueError(
                f"Qdrant collection {collection_name!r} has unsupported "
                f"vector configuration: {type(vectors).__name__}"
            )
        return vectors.size

    def upsert_event_vector(
        self,
        event_id: str,
        vector: list[float],
        payload: dict[str, Any] | None = None,
    ) -> None:
        """写入一条事件向量。"""
        payload = {**(payload or {}), "event_id": event_id}
        self._upsert(self._event_collection, event_id, vector, payload)

    def upsert_rule_vector(
        self,
        chunk_id: str,
        vector: list[float],
        payload: dict[str, Any] | None = None,
    ) -> None:
        """写入一条规则 chunk 向量。"""
        payload = {**(payload or {}), "chunk_id": chunk_id}
        self._upsert(self._rule_collection, chunk_id, vector, payload)

    def replace_rule_vectors(
        self,
        points: Sequence[tuple[str, list[float], dict[str, Any]]],
    ) -> None:
        """写入当前规则投影并删除不属于当前投影的旧 point。"""
        if not points:
            if self.collection_exists(self._rule_collection):
                existing_ids = self._scroll_point_ids(self._rule_collection)
                self._delete_points(self._rule_collection, existing_ids)
            return

        vector_size = len(points[0][1])
        if vector_size <= 0:
            raise ValueError("rule embedding vectors must not be empty")
        if any(len(vector) != vector_size for _, vector, _ in points):
            raise ValueError("rule embedding vectors must have a consistent dimension")
        self.ensure_collection(self._rule_collection, vector_size)

        desired_ids = {_point_id(chunk_id) for chunk_id, _, _ in points}
        qdrant_points = [
            models.PointStruct(
                id=_point_id(chunk_id),
                vector=vector,
                payload={**payload, "chunk_id": chunk_id},
            )
            for chunk_id, vector, payload in points
        ]
        # 先写入完整的新投影，再删除旧 point；embedding 失败发生在调用本方法之前，
        # 因而不会触碰现有索引。
        self._client.upsert(
            collection_name=self._rule_collection,
            points=qdrant_points,
            wait=True,
        )
        existing_ids = self._scroll_point_ids(self._rule_collection)
        self._delete_points(
            self._rule_collection,
            [point_id for point_id in existing_ids if point_id not in desired_ids],
        )

    def search_events(
        self,
        query_vector: list[float],
        *,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """按向量检索事件，返回带 score 与 payload 的结果。"""
        return self._search(self._event_collection, query_vector, top_k, filters)

    def search_rules(
        self,
        query_vector: list[float],
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """按向量检索规则 chunk。"""
        return self._search(self._rule_collection, query_vector, top_k, filters)

    def _upsert(
        self,
        collection: str,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any] | None,
    ) -> None:
        self.ensure_collection(collection, len(vector))
        point = models.PointStruct(
            id=_point_id(point_id),
            vector=vector,
            payload=payload or {},
        )
        self._client.upsert(
            collection_name=collection,
            points=[point],
        )

    def _search(
        self,
        collection: str,
        query_vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not self.collection_exists(collection):
            return []
        self.ensure_collection(collection, len(query_vector))
        response = self._client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=top_k,
            query_filter=_build_filter(filters),
        )
        hits: list[dict[str, Any]] = []
        for hit in response.points:
            item = dict(hit.payload or {})
            item["id"] = hit.id
            item["score"] = hit.score
            hits.append(item)
        return hits

    def _scroll_point_ids(self, collection_name: str) -> list[str | int | uuid.UUID]:
        point_ids: list[str | int | uuid.UUID] = []
        offset: str | int | uuid.UUID | None = None
        while True:
            records, offset = self._client.scroll(
                collection_name=collection_name,
                limit=1000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            point_ids.extend(cast(str | int | uuid.UUID, record.id) for record in records)
            if offset is None:
                return point_ids

    def _delete_points(
        self,
        collection_name: str,
        point_ids: Sequence[str | int | uuid.UUID],
    ) -> None:
        if not point_ids:
            return
        self._client.delete(
            collection_name=collection_name,
            points_selector=models.PointIdsList(points=list(point_ids)),
            wait=True,
        )
