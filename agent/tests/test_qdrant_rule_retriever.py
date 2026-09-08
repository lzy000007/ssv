from __future__ import annotations

import asyncio
import json
from pathlib import Path

import ssv_agent.knowledge.backends.qdrant as qdrant_backend
from ssv_agent.event_store.qdrant_store import SsvQdrantStore
from ssv_agent.knowledge.schema import Chunk
from ssv_agent.tools.rule_retriever import rule_retriever_tool


def test_rule_embedding_batches_at_twenty_and_preserves_order(monkeypatch) -> None:
    class RecordingEmbedding:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            return [[float(int(text.split("-")[-1]))] for text in texts]

    embedding = RecordingEmbedding()
    monkeypatch.setattr(qdrant_backend, "_embedding_provider", lambda: embedding)
    passages = [
        Chunk(chunk_id=str(index), content=f"rule-{index}", score=0.0)
        for index in range(21)
    ]

    vectors = asyncio.run(qdrant_backend._embed_passages(passages))

    assert [len(batch) for batch in embedding.calls] == [20, 1]
    assert vectors == [[float(index)] for index in range(21)]


class _SemanticTestEmbedding:
    backend_name = "test-semantic"

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "目标" in text else [0.0, 1.0] for text in texts]

    async def embed_query(self, query: str) -> list[float]:
        return [1.0, 0.0] if "目标" in query else [0.0, 1.0]


def _configure_test_qdrant(monkeypatch, tmp_path: Path) -> _SemanticTestEmbedding:
    embedding = _SemanticTestEmbedding()
    monkeypatch.setattr(qdrant_backend, "_embedding_provider", lambda: embedding)
    monkeypatch.setenv("SSV_EMBEDDING_BACKEND", "bge_m3")
    monkeypatch.setenv("SSV_EMBEDDING_MODEL", "test-semantic")
    monkeypatch.setenv("SSV_QDRANT_PATH", str(tmp_path / "qdrant"))
    monkeypatch.setenv("SSV_KNOWLEDGE_MIN_SCORE", "0.8")
    return embedding


def test_qdrant_rule_ingest_retrieve_filter_and_rebuild(tmp_path: Path, monkeypatch) -> None:
    _configure_test_qdrant(monkeypatch, tmp_path)
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    document = knowledge / "rules.md"
    document.write_text("1 目标安全帽条款。\n2 其他条款。\n", encoding="utf-8")

    first = asyncio.run(qdrant_backend.ingest_rules(knowledge))
    assert first.success is True
    assert first.chunks_count == 2

    retriever = qdrant_backend.QdrantRuleRetriever()
    result = asyncio.run(retriever.retrieve("目标", top_k=5))
    assert [chunk.metadata["section"] for chunk in result.chunks] == ["1"]

    filtered = asyncio.run(
        retriever.retrieve("目标", top_k=5, filters={"source": "rules.md"})
    )
    assert [chunk.metadata["source"] for chunk in filtered.chunks] == ["rules.md"]

    document.write_text("1 目标安全帽条款的新版本。\n", encoding="utf-8")
    second = asyncio.run(qdrant_backend.ingest_rules(knowledge))
    assert second.chunks_count == 1
    with SsvQdrantStore() as store:
        assert store.count_points(store.rule_collection) == 1


def test_qdrant_rule_retriever_reports_missing_empty_and_below_threshold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_test_qdrant(monkeypatch, tmp_path)
    retriever = qdrant_backend.QdrantRuleRetriever()

    missing = asyncio.run(retriever.retrieve("目标"))
    assert missing.success is False
    assert "knowledge_ingest" in (missing.error_message or "")

    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "rules.md").write_text("1 目标安全帽条款。\n", encoding="utf-8")
    asyncio.run(qdrant_backend.ingest_rules(knowledge))

    unrelated = asyncio.run(retriever.retrieve("完全无关的问题"))
    assert unrelated.success is True
    assert unrelated.chunks == []
    assert unrelated.summary is not None


def test_qdrant_rule_retriever_rejects_mock_embedding(monkeypatch) -> None:
    monkeypatch.delenv("SSV_EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("SSV_EMBEDDING_MODEL", raising=False)

    try:
        qdrant_backend._embedding_provider()
    except RuntimeError as exc:
        assert "real embedding" in str(exc)
    else:  # pragma: no cover - the assertion is the intended branch
        raise AssertionError("mock embedding must not be accepted by rule RAG")


def test_rule_tool_sync_in_existing_event_loop(monkeypatch) -> None:
    monkeypatch.setenv("SSV_KNOWLEDGE_BACKEND", "local_markdown")

    async def invoke() -> dict[str, object]:
        return json.loads(rule_retriever_tool.invoke({"query": "安全帽", "top_k": 1}))

    result = asyncio.run(invoke())
    assert result["success"] is True
    assert result["chunks"]
