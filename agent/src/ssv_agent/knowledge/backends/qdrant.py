"""Qdrant-backed vector retrieval for local rule documents."""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from ssv_agent.embedding.registry import get_configured_provider
from ssv_agent.event_store.qdrant_store import SsvQdrantStore
from ssv_agent.knowledge.backends.local_markdown import (
    DEFAULT_KNOWLEDGE_DIR,
    _read_text,
    _split_clauses,
)
from ssv_agent.knowledge.ingester import Ingester
from ssv_agent.knowledge.registry import register_backend
from ssv_agent.knowledge.retriever import Retriever
from ssv_agent.knowledge.schema import Chunk, IngestResult, RetrievalResult

MAX_EMBEDDING_BATCH_SIZE = 20
MAX_RULE_TOP_K = 100
DEFAULT_MIN_SCORE = 0.5


class _Embeddable(Protocol):
    @property
    def content(self) -> str: ...


def _embedding_provider():
    provider = get_configured_provider(
        os.getenv("SSV_EMBEDDING_BACKEND", "mock"),
        os.getenv("SSV_EMBEDDING_MODEL"),
    )
    if getattr(provider, "backend_name", None) == "mock":
        raise RuntimeError(
            "Qdrant rule RAG requires a real embedding backend; "
            "configure bge_m3 or openai_compatible"
        )
    return provider


def _chunk_id(source: str, section: str, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"{source}:{section}:{digest}"


def _expand_rule_query(query: str) -> str:
    """补充规则文档中的领域术语，降低中文简称造成的召回偏差。"""
    if "安全帽" in query and "绝缘安全帽" not in query:
        return f"{query} 绝缘安全帽"
    return query


async def _embed_passages(passages: Sequence[_Embeddable]) -> list[list[float]]:
    """按外部 API 的批量上限顺序生成规则分块向量。"""
    provider = _embedding_provider()
    vectors: list[list[float]] = []
    for start in range(0, len(passages), MAX_EMBEDDING_BATCH_SIZE):
        batch = passages[start : start + MAX_EMBEDDING_BATCH_SIZE]
        batch_vectors = await provider.embed_texts([passage.content for passage in batch])
        if len(batch_vectors) != len(batch):
            raise ValueError(
                "embedding provider returned an unexpected number of rule vectors: "
                f"expected={len(batch)}, actual={len(batch_vectors)}"
            )
        vectors.extend(batch_vectors)
    if not vectors:
        return []
    dimension = len(vectors[0])
    if dimension <= 0 or any(
        len(vector) != dimension
        or any(not math.isfinite(value) for value in vector)
        for vector in vectors
    ):
        raise ValueError("rule embedding vectors must be non-empty, finite, and same-sized")
    return vectors


async def ingest_rules(knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR) -> IngestResult:
    """切分本地规则文件、生成向量并写入可重建的规则 collection。"""
    if not knowledge_dir.is_dir():
        return IngestResult(
            success=False,
            error_message=f"rule knowledge directory not found: {knowledge_dir}",
        )
    paths = sorted(
        path for path in knowledge_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".md", ".txt"}
    )
    raw_passages = [
        passage
        for path in paths
        for passage in _split_clauses(path.name, _read_text(path))
    ]
    passages = []
    seen_chunk_ids: set[str] = set()
    for passage in raw_passages:
        chunk_id = _chunk_id(passage.source, passage.section, passage.content)
        if chunk_id in seen_chunk_ids:
            continue
        seen_chunk_ids.add(chunk_id)
        passages.append(passage)
    try:
        vectors = await _embed_passages(passages)
        points = []
        for passage, vector in zip(passages, vectors, strict=True):
            identifier = _chunk_id(passage.source, passage.section, passage.content)
            points.append(
                (
                    identifier,
                    vector,
                    {
                        "source": passage.source,
                        "rule_id": passage.section,
                        "section": passage.section,
                        "content": passage.content,
                    },
                )
            )
        with SsvQdrantStore() as store:
            store.replace_rule_vectors(points)
    except Exception as exc:
        return IngestResult(
            document_id=str(knowledge_dir),
            success=False,
            error_message=str(exc),
        )
    return IngestResult(document_id=str(knowledge_dir), chunks_count=len(passages))


def _min_score() -> float:
    raw = os.getenv("SSV_KNOWLEDGE_MIN_SCORE")
    if raw is None:
        return DEFAULT_MIN_SCORE
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("SSV_KNOWLEDGE_MIN_SCORE must be a number between -1 and 1") from exc
    if not -1.0 <= value <= 1.0:
        raise ValueError("SSV_KNOWLEDGE_MIN_SCORE must be a number between -1 and 1")
    return value


class QdrantRuleRetriever(Retriever):
    backend_name = "qdrant"

    async def retrieve(
        self, query: str, *, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> RetrievalResult:
        if top_k < 1 or top_k > MAX_RULE_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_RULE_TOP_K}")
        provider = _embedding_provider()
        vector = await provider.embed_query(_expand_rule_query(query))
        if not vector or any(not math.isfinite(value) for value in vector):
            raise ValueError("query embedding vector must be non-empty and finite")
        min_score = _min_score()
        with SsvQdrantStore() as store:
            if not store.collection_exists(store.rule_collection):
                return RetrievalResult(
                    chunks=[],
                    query=query,
                    backend=self.backend_name,
                    success=False,
                    error_message="rule vector index does not exist; run knowledge_ingest first",
                )
            if store.count_points(store.rule_collection) == 0:
                return RetrievalResult(
                    chunks=[],
                    query=query,
                    backend=self.backend_name,
                    success=False,
                    error_message="rule vector index is empty; run knowledge_ingest first",
                )
            hits = store.search_rules(
                vector,
                top_k=min(MAX_RULE_TOP_K, max(top_k, top_k * 4)),
                filters=filters,
            )
        hits = [hit for hit in hits if float(hit.get("score", -1.0)) >= min_score]
        if "安全帽" in query:
            hits = [hit for hit in hits if "安全帽" in str(hit.get("content", ""))]
        chunks = [
            Chunk(
                chunk_id=str(hit["chunk_id"]),
                content=str(hit["content"]),
                score=round(float(hit["score"]), 4),
                metadata={
                    "source": hit["source"],
                    "rule_id": hit["rule_id"],
                    "section": hit["section"],
                },
            )
            for hit in hits
        ]
        summary = None
        if not chunks:
            summary = f"未找到相似度不低于 {min_score:.3f} 的规则片段。"
        return RetrievalResult(
            chunks=chunks[:top_k],
            query=query,
            backend=self.backend_name,
            summary=summary,
        )


class QdrantRuleIngester(Ingester):
    backend_name = "qdrant"

    async def ingest(self, document: Any) -> IngestResult:
        return await ingest_rules(Path(document) if document else DEFAULT_KNOWLEDGE_DIR)


register_backend("qdrant", QdrantRuleRetriever, QdrantRuleIngester)
