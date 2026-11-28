"""Structure-aware chunking shared by all format engines.

The chunker walks the ``DocNode`` list a reader produced and cuts on structural boundaries
(heading changes, table edges, slide/page changes) before falling back to sentence splitting.
This keeps `locator` accurate, which is what page/slide/cell citations depend on.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, Iterable, List, Optional

from app.knowledge.contracts import DocNode, Locator, NodeKind

CHUNK_TARGET_TOKENS = 640
CHUNK_MAX_TOKENS = 1000
CHUNK_OVERLAP_CHARS = 160

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def estimate_tokens(text: str) -> int:
    """Same estimator as AGCore (Korean ~1.5 chars/token, other ~3.5)."""

    if not text:
        return 0
    hangul = len(re.findall(r"[\uac00-\ud7a3]", text))
    other = len(text) - hangul
    return max(1, int(hangul / 1.5 + other / 3.5))


def _split_long(text: str, max_tokens: int) -> List[str]:
    pieces: List[str] = []
    buffer: List[str] = []
    tokens = 0
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if estimate_tokens(sentence) > max_tokens:
            if buffer:
                pieces.append(" ".join(buffer).strip())
                buffer, tokens = [], 0
            step = max(100, len(sentence) // max(1, estimate_tokens(sentence) // max_tokens + 1))
            pieces.extend(sentence[i : i + step].strip() for i in range(0, len(sentence), step))
            continue
        if tokens + estimate_tokens(sentence) > max_tokens and buffer:
            pieces.append(" ".join(buffer).strip())
            buffer, tokens = [], 0
        buffer.append(sentence)
        tokens += estimate_tokens(sentence)
    if buffer:
        pieces.append(" ".join(buffer).strip())
    return [piece for piece in pieces if piece]


def _node_text(node: DocNode) -> str:
    if node.kind is NodeKind.TABLE and node.table:
        lines = ["\t".join(row) for row in node.table.rows]
        header = f"[table {node.table.caption}]" if node.table.caption else "[table]"
        return header + "\n" + "\n".join(lines)
    if node.kind is NodeKind.IMAGE and node.image:
        label = node.image.caption or node.image.alt_text or node.image.asset_id
        return f"[image {label}]"
    if node.kind is NodeKind.CHART and node.chart:
        series = ", ".join(str(item.get("name", "")) for item in node.chart.series)
        return f"[chart {node.chart.chart_type} {node.chart.title} categories={','.join(node.chart.categories)} series={series}]"
    return node.text


def chunk_nodes(
    nodes: Iterable[DocNode],
    *,
    document_key: str,
    target_tokens: int = CHUNK_TARGET_TOKENS,
    max_tokens: int = CHUNK_MAX_TOKENS,
) -> List[Dict]:
    """Return ``[{chunk_id, locator, content, node_id, section}]`` in reading order."""

    chunks: List[Dict] = []
    buffer_text: List[str] = []
    buffer_tokens = 0
    buffer_locator: Optional[Locator] = None
    section = ""
    sequence = 0

    def flush() -> None:
        nonlocal buffer_text, buffer_tokens, buffer_locator, sequence
        content = "\n".join(buffer_text).strip()
        if content:
            sequence += 1
            digest = hashlib.sha256(
                f"{document_key}|{sequence}|{content[:200]}".encode("utf-8")
            ).hexdigest()[:12]
            chunks.append(
                {
                    "chunk_id": f"chk_{digest}",
                    "locator": (buffer_locator or Locator()).model_dump(mode="json", exclude_none=True),
                    "content": content,
                    "section": section,
                }
            )
        buffer_text, buffer_tokens, buffer_locator = [], 0, None

    for node in nodes:
        text = _node_text(node).strip()
        if not text:
            continue
        if node.kind is NodeKind.HEADING:
            flush()
            section = node.text.strip()
            buffer_text.append(node.text.strip())
            buffer_tokens = estimate_tokens(node.text)
            buffer_locator = node.locator
            continue
        if node.kind in (NodeKind.TABLE, NodeKind.CHART):
            flush()
            buffer_text = [text]
            buffer_tokens = estimate_tokens(text)
            buffer_locator = node.locator
            flush()
            continue
        node_tokens = estimate_tokens(text)
        if node_tokens > max_tokens:
            flush()
            for piece in _split_long(text, target_tokens):
                sequence += 1
                digest = hashlib.sha256(f"{document_key}|{sequence}|{piece[:200]}".encode()).hexdigest()[:12]
                chunks.append(
                    {
                        "chunk_id": f"chk_{digest}",
                        "locator": node.locator.model_dump(mode="json", exclude_none=True),
                        "content": piece,
                        "section": section,
                    }
                )
            continue
        if buffer_tokens + node_tokens > target_tokens and buffer_text:
            flush()
        if buffer_locator is None:
            buffer_locator = node.locator
        buffer_text.append(text)
        buffer_tokens += node_tokens
    flush()
    return chunks
