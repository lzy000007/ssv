"""Local Markdown/TXT regulation retrieval without external services."""

from __future__ import annotations

from collections import Counter
import math
import os
from pathlib import Path
import re
from typing import Any

from ssv_agent.knowledge.catalog import (
    DEFAULT_RULES_DIR,
    RuleDocument,
    discover_rule_documents,
)
from ssv_agent.knowledge.ingester import Ingester
from ssv_agent.knowledge.registry import register_backend
from ssv_agent.knowledge.rule_chunks import (
    RulePassage,
    rule_chunk_id,
    rule_chunk_metadata,
    split_rule_clauses,
)
from ssv_agent.knowledge.retriever import Retriever
from ssv_agent.knowledge.schema import Chunk, IngestResult, RetrievalResult


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for unit in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", text.lower()):
        if "\u4e00" <= unit[0] <= "\u9fff":
            for size in range(2, min(5, len(unit)) + 1):
                tokens.extend(unit[index : index + size] for index in range(len(unit) - size + 1))
        else:
            tokens.append(unit)
    return tokens


class LocalMarkdownRetriever(Retriever):
    """Read project-local regulations and retrieve the strongest matching clauses."""

    backend_name = "local_markdown"

    def __init__(self, knowledge_dir: Path | None = None) -> None:
        configured_dir = os.getenv("SSV_KNOWLEDGE_RULES_DIR")
        self._knowledge_dir = (
            Path(knowledge_dir)
            if knowledge_dir is not None
            else Path(configured_dir) if configured_dir else DEFAULT_RULES_DIR
        )
        self._fingerprint: tuple[RuleDocument, ...] = ()
        self._passages: list[RulePassage] = []
        self._term_frequencies: list[Counter[str]] = []
        self._document_frequencies: Counter[str] = Counter()
        self._average_length = 0.0

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self._reload_if_changed()

        terms = set(_tokens(query))
        active_filters = filters or {}
        source_filter = active_filters.get("source")
        rule_id_filter = active_filters.get("rule_id")
        rule_version_filter = active_filters.get("rule_version")
        event_type_filter = active_filters.get("event_type")
        if not terms:
            return RetrievalResult(chunks=[], query=query, backend=self.backend_name)

        total = len(self._passages)
        if total == 0 or self._average_length == 0:
            return RetrievalResult(
                chunks=[],
                query=query,
                backend=self.backend_name,
                summary=f"未在 {self._knowledge_dir} 找到可检索规章。",
            )

        scores: list[tuple[float, int]] = []
        for index, frequencies in enumerate(self._term_frequencies):
            passage = self._passages[index]
            if source_filter and passage.source != source_filter:
                continue
            if rule_id_filter and passage.rule_id != rule_id_filter:
                continue
            if rule_version_filter and passage.rule_version != rule_version_filter:
                continue
            if event_type_filter and event_type_filter not in passage.event_types:
                continue
            length = sum(frequencies.values())
            score = 0.0
            for term in terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                document_frequency = self._document_frequencies[term]
                inverse_frequency = math.log(
                    1 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                normalized_frequency = frequency * 2.5 / (
                    frequency + 1.5 * (0.25 + 0.75 * length / self._average_length)
                )
                score += inverse_frequency * normalized_frequency * (len(term) - 1)
            if score:
                scores.append((score, index))

        ranked = sorted(scores, key=lambda item: (-item[0], item[1]))[:top_k]
        chunks = [
            Chunk(
                chunk_id=rule_chunk_id(self._passages[index]),
                content=self._passages[index].content,
                score=round(score, 4),
                metadata=rule_chunk_metadata(self._passages[index]),
            )
            for score, index in ranked
        ]
        return RetrievalResult(chunks=chunks, query=query, backend=self.backend_name)

    def _reload_if_changed(self) -> None:
        documents = discover_rule_documents(self._knowledge_dir)
        # 正文不变时，规则身份和适用事件等元数据也可能单独修订。
        fingerprint = documents
        if fingerprint == self._fingerprint:
            return

        passages = [
            passage
            for document in documents
            for passage in split_rule_clauses(document.source_path, document.content, rule=document)
        ]
        frequencies = [Counter(_tokens(passage.content)) for passage in passages]
        self._passages = passages
        self._term_frequencies = frequencies
        self._document_frequencies = Counter(token for terms in frequencies for token in terms)
        self._average_length = (
            sum(sum(terms.values()) for terms in frequencies) / len(frequencies)
            if frequencies
            else 0.0
        )
        self._fingerprint = fingerprint

class LocalMarkdownIngester(Ingester):
    """Static project regulations are read from disk and are not ingested externally."""

    backend_name = "local_markdown"

    async def ingest(self, document: Any) -> IngestResult:
        return IngestResult(success=False, error_message="local Markdown documents are read-only")


register_backend("local_markdown", LocalMarkdownRetriever, LocalMarkdownIngester)
