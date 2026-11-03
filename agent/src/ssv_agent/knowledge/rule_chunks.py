"""规则文档的分块与稳定 chunk ID。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

from ssv_agent.knowledge.catalog import RuleDocument


MAX_SECTION_LENGTH = 1200
CLAUSE_HEADING = re.compile(
    r"^(?:(?:#{1,6}\s*)?(?P<section>\d+(?:\.\d+){0,5})(?=\s|$)"
    r"|(?P<heading>#{1,6})\s+(?P<title>\S.*))"
)
_LAYOUT_NOISE = re.compile(
    r"^(?:#{1,6}\s+\?\s*\d+\s*\?|\d+|GB(?:/T)?\s*\d+(?:\.\d+)*\s*[—-]\s*\d{4})$"
)


@dataclass(frozen=True)
class RulePassage:
    """一条规则正文分块及其检索元数据。"""

    source: str
    section: str
    content: str
    rule_id: str = ""
    rule_version: str = ""
    event_types: tuple[str, ...] = ()
    content_hash: str = ""
    part_index: int = 0


def rule_chunk_metadata(passage: RulePassage) -> dict[str, Any]:
    """构造与检索后端一致的规则 chunk 元数据。"""
    return {
        "source": passage.source,
        "rule_id": passage.rule_id,
        "rule_version": passage.rule_version,
        "section": passage.section,
        "content_hash": passage.content_hash,
        "event_types": list(passage.event_types),
    }


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
        # 单个句子也可能超过上限，不能仅在句间拆分。
        while len(sentence) > MAX_SECTION_LENGTH:
            chunks.append(sentence[:MAX_SECTION_LENGTH])
            sentence = sentence[MAX_SECTION_LENGTH:]
        if sentence:
            buffer = f"{buffer}\n{sentence}" if buffer else sentence
    if buffer.strip():
        chunks.append(buffer.strip())
    return chunks


def split_rule_clauses(
    source: str,
    text: str,
    *,
    rule: RuleDocument | None = None,
) -> list[RulePassage]:
    """按条款标题切分规则正文，并附加规则身份元数据。"""
    passages: list[RulePassage] = []
    section_parts: dict[str, int] = {}

    def make_passage(section: str, content: str) -> RulePassage:
        part_index = section_parts.get(section, 0)
        section_parts[section] = part_index + 1
        return RulePassage(
            source=source,
            section=section,
            content=content,
            rule_id=rule.rule_id if rule else "",
            rule_version=rule.version if rule else "",
            event_types=rule.event_types if rule else (),
            content_hash=rule.content_hash if rule else "",
            part_index=part_index,
        )

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
                make_passage(section, content)
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
            make_passage(section, content)
            for content in _split_large_section("\n".join(section_lines))
        )
    return passages


def rule_chunk_id(passage: RulePassage) -> str:
    """保留文档修订身份，并区分同条款的不同分段及重复正文。"""
    digest = passage.content_hash.removeprefix("sha256:")[:12]
    chunk_digest = hashlib.sha256(passage.content.encode("utf-8")).hexdigest()[:16]
    return (
        f"{passage.source}:{passage.rule_id}:{passage.rule_version}:"
        f"{passage.section}:{digest}:{passage.part_index}:{chunk_digest}"
    )
