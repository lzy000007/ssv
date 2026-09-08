"""Local Markdown/TXT regulation retrieval without external services."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Any

from ssv_agent.knowledge.ingester import Ingester
from ssv_agent.knowledge.registry import register_backend
from ssv_agent.knowledge.retriever import Retriever
from ssv_agent.knowledge.schema import Chunk, IngestResult, RetrievalResult


DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[4] / "knowledge"
MAX_SECTION_LENGTH = 1200
CLAUSE_HEADING = re.compile(
    r"^(?:(?:#{1,6}\s*)?(?P<section>\d+(?:\.\d+){0,5})(?=\s|$)"
    r"|(?P<heading>#{1,6})\s+(?P<title>\S.*))"
)
_LAYOUT_NOISE = re.compile(
    r"^(?:#{1,6}\s+\?\s*\d+\s*\?|\d+|GB26860[—-]2011)$"
)
SUPPORTED_EXTENSIONS = frozenset({".md", ".txt"})


@dataclass(frozen=True)
class _Passage:
    source: str
    section: str
    content: str


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for unit in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", text.lower()):
        if "\u4e00" <= unit[0] <= "\u9fff":
            for size in range(2, min(5, len(unit)) + 1):
                tokens.extend(unit[index : index + size] for index in range(len(unit) - size + 1))
        else:
            tokens.append(unit)
    return tokens


def _split_large_section(section: str) -> list[str]:
    if len(section) <= MAX_SECTION_LENGTH:
        return [section]

    chunks: list[str] = []
    buffer = ""
    for sentence in re.split(r"(?<=[\u3002\uff01\uff1f\uff1b!?;])", section):
        sentence = sentence.strip()
        if not sentence:
            continue
        if buffer and len(buffer) + len(sentence) + 1 > MAX_SECTION_LENGTH:
            chunks.append(buffer.strip())
            buffer = ""
        buffer += sentence + "\n"
    if buffer.strip():
        chunks.append(buffer.strip())
    return chunks


def _split_clauses(source: str, text: str) -> list[_Passage]:
    passages: list[_Passage] = []
    section = "未编号"
    section_lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _LAYOUT_NOISE.fullmatch(line):
            continue
        match = CLAUSE_HEADING.match(line)
        if match and section_lines:
            passages.extend(
                _Passage(source, section, content)
                for content in _split_large_section("\n".join(section_lines))
            )
            section_lines = []
        if match and match.group("section"):
            section = match.group("section")
        elif match and match.group("title"):
            section = match.group("title")
        section_lines.append(line)
    if section_lines:
        passages.extend(
            _Passage(source, section, content)
            for content in _split_large_section("\n".join(section_lines))
        )
    return passages


class LocalMarkdownRetriever(Retriever):
    """Read project-local regulations and retrieve the strongest matching clauses."""

    backend_name = "local_markdown"

    def __init__(self, knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR) -> None:
        self._knowledge_dir = knowledge_dir
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()
        self._passages: list[_Passage] = []
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

        if "安全帽" in query and "绝缘安全帽" not in query:
            query = f"{query} 绝缘安全帽"
        terms = set(_tokens(query))
        source_filter = (filters or {}).get("source")
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

        ranked = sorted(scores, key=lambda item: (-item[0], item[1]))[: min(top_k, 2)]
        chunks = [
            Chunk(
                chunk_id=self._chunk_id(self._passages[index]),
                content=self._passages[index].content,
                score=round(score, 4),
                metadata={
                    "source": self._passages[index].source,
                    "rule_id": self._passages[index].section,
                    "section": self._passages[index].section,
                },
            )
            for score, index in ranked
        ]
        return RetrievalResult(chunks=chunks, query=query, backend=self.backend_name)

    def _reload_if_changed(self) -> None:
        paths = self._document_paths()
        fingerprint = tuple(
            (str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
            for path in paths
        )
        if fingerprint == self._fingerprint:
            return

        passages = [
            passage
            for path in paths
            for passage in _split_clauses(path.name, _read_text(path))
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

    def _document_paths(self) -> list[Path]:
        if not self._knowledge_dir.is_dir():
            return []
        return sorted(
            path
            for path in self._knowledge_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        )

    @staticmethod
    def _chunk_id(passage: _Passage) -> str:
        digest = hashlib.sha256(passage.content.encode("utf-8")).hexdigest()[:12]
        return f"{passage.source}:{passage.section}:{digest}"


class LocalMarkdownIngester(Ingester):
    """Static project regulations are read from disk and are not ingested externally."""

    backend_name = "local_markdown"

    async def ingest(self, document: Any) -> IngestResult:
        return IngestResult(success=False, error_message="local Markdown documents are read-only")


register_backend("local_markdown", LocalMarkdownRetriever, LocalMarkdownIngester)
