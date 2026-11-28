"""Plain-text / Markdown / CSV reader (no writer; text is edited by the artifact layer)."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import List

from app.knowledge.contracts import (
    DocNode,
    DocumentRef,
    ImageData,
    Locator,
    LocatorKind,
    NodeKind,
    ReadMode,
    ReadRequest,
    ReadResponse,
    ReadStats,
    TableData,
    format_family,
)


def _decode(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def read_text(data: bytes, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    text, encoding = _decode(data)
    warnings: List[str] = []
    if encoding == "utf-8(replace)":
        warnings.append("encoding_replaced")
    citations = []
    nodes: List[DocNode] = []

    if format_family(ref.mime_type).value == "image":
        nodes.append(
            DocNode(
                node_id="n1",
                kind=NodeKind.IMAGE,
                locator=Locator(kind=LocatorKind.DOCUMENT),
                image=ImageData(
                    asset_id=f"{ref.document_id}:1",
                    mime_type=ref.mime_type,
                    size_bytes=len(data),
                    caption=ref.name,
                ),
                text=ref.name,
            )
        )
        warnings.append("image_content_requires_vision")
        return ReadResponse(
            document=ref,
            mode=request.mode,
            nodes=nodes,
            citations=citations,
            warnings=warnings,
            stats=ReadStats(chars=0, nodes=1, images=1),
        )

    if format_family(ref.mime_type).value == "csv":
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except csv.Error as exc:
            rows = []
            warnings.append(f"csv_parse_failed:{type(exc).__name__}")
        rows = [[cell for cell in row] for row in rows[: request.max_rows]]
        nodes.append(
            DocNode(
                node_id="n1",
                kind=NodeKind.TABLE,
                locator=Locator(kind=LocatorKind.DOCUMENT),
                table=TableData(rows=rows, header_rows=1 if rows else 0),
            )
        )
        return ReadResponse(
            document=ref,
            mode=request.mode,
            nodes=nodes,
            warnings=warnings,
            stats=ReadStats(chars=len(text), nodes=1, tables=1),
        )

    if request.mode is ReadMode.OUTLINE:
        for index, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                nodes.append(
                    DocNode(
                        node_id=f"n{index}",
                        kind=NodeKind.HEADING,
                        text=stripped.lstrip("# ").strip(),
                        level=min(level, 9),
                        locator=Locator(kind=LocatorKind.SECTION, section=stripped.lstrip("# ").strip()),
                    )
                )
        return ReadResponse(
            document=ref, mode=request.mode, nodes=nodes, warnings=warnings,
            stats=ReadStats(chars=len(text), nodes=len(nodes)),
        )

    section = ""
    index = 0
    consumed = 0
    for block in _paragraphs(text):
        index += 1
        consumed += len(block)
        if consumed > request.max_chars:
            warnings.append("max_chars_reached")
            break
        if block.startswith("#"):
            level = len(block) - len(block.lstrip("#"))
            section = block.lstrip("# ").strip()
            nodes.append(
                DocNode(
                    node_id=f"n{index}",
                    kind=NodeKind.HEADING,
                    text=section,
                    level=min(level, 9),
                    locator=Locator(kind=LocatorKind.SECTION, section=section),
                )
            )
            continue
        node = DocNode(
            node_id=f"n{index}",
            kind=NodeKind.PARAGRAPH,
            text=block,
            locator=Locator(
                kind=LocatorKind.PARAGRAPH, paragraph=index, section=section or None
            ),
        )
        nodes.append(node)
    return ReadResponse(
        document=ref,
        mode=request.mode,
        nodes=nodes,
        warnings=warnings,
        truncated="max_chars_reached" in warnings,
        stats=ReadStats(chars=min(consumed, request.max_chars), nodes=len(nodes)),
    )


def _paragraphs(text: str) -> List[str]:
    blocks: List[str] = []
    buffer: List[str] = []
    for line in text.splitlines():
        if not line.strip():
            if buffer:
                blocks.append(" ".join(buffer).strip())
                buffer = []
            continue
        if line.lstrip().startswith("#"):
            if buffer:
                blocks.append(" ".join(buffer).strip())
                buffer = []
            blocks.append(line.strip())
            continue
        buffer.append(line.strip())
    if buffer:
        blocks.append(" ".join(buffer).strip())
    return [block for block in blocks if block]


def read_path(path: Path, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    return read_text(path.read_bytes(), ref, request)
