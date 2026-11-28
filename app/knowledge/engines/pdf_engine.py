"""PDF engine (WP-03).

Reads page by page with PyMuPDF, keeping page-level citations, block structure, tables, image
metadata, and a partial-read cursor. The legacy `app/parsers/pdf_parser.py` is reused for the
hierarchical document pipeline; this engine is the contract-facing, page-addressed surface.

Handled explicitly
------------------
* encrypted documents          -> ``encrypted_document``
* damaged documents            -> ``corrupt_content``
* page limit                   -> ``page_limit_exceeded``
* scanned pages without text   -> ``no_text_layer`` warning + vision/OCR hook
* large documents              -> page windows and ``next_cursor``
"""

from __future__ import annotations

import time
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.knowledge.contracts import (
    Citation,
    DocNode,
    DocumentRef,
    ImageData,
    KnowledgeError,
    KnowledgeErrorCode,
    Locator,
    LocatorKind,
    NodeKind,
    ReadMode,
    ReadRequest,
    ReadResponse,
    ReadStats,
    TableData,
    make_citation,
)


def _open(path: Path):
    import fitz  # PyMuPDF

    try:
        return fitz.open(path)
    except Exception as exc:  # fitz raises FileDataError, RuntimeError, ValueError
        name = type(exc).__name__
        if "encrypted" in str(exc).lower() or "password" in str(exc).lower():
            raise KnowledgeError(KnowledgeErrorCode.ENCRYPTED_DOCUMENT, "password required") from exc
        raise KnowledgeError(
            KnowledgeErrorCode.CORRUPT_CONTENT,
            "pdf could not be opened",
            detail={"reason": name},
        ) from exc


def page_count(path: Path) -> int:
    document = _open(path)
    try:
        return document.page_count
    finally:
        document.close()


def read_pdf(path: Path, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    started = time.monotonic()
    document = _open(path)
    warnings: List[str] = []
    try:
        if document.needs_pass:
            raise KnowledgeError(
                KnowledgeErrorCode.ENCRYPTED_DOCUMENT,
                "document is password protected",
                detail={"document_id": ref.document_id},
            )
        total = document.page_count
        limit = int(getattr(settings, "MAX_PAGES_LIMIT", 500) or 500)
        if total > limit:
            raise KnowledgeError(
                KnowledgeErrorCode.PAGE_LIMIT_EXCEEDED,
                f"document has {total} pages, limit is {limit}",
                detail={"pages": total, "limit": limit},
            )
        if total == 0:
            raise KnowledgeError(KnowledgeErrorCode.CORRUPT_CONTENT, "document has no pages")

        if request.pages:
            pages = [page for page in request.pages if 1 <= page <= total]
            if not pages:
                raise KnowledgeError(
                    KnowledgeErrorCode.INVALID_REQUEST,
                    "requested pages outside document",
                    detail={"pages": request.pages, "total": total},
                )
        else:
            pages = list(range(1, total + 1))

        if request.mode is ReadMode.OUTLINE:
            return _outline(document, ref, pages, started, warnings)

        nodes: List[DocNode] = []
        citations: List[Citation] = []
        stats = ReadStats(pages=total)
        consumed = 0
        next_page: Optional[int] = None

        for page_number in pages:
            page = document.load_page(page_number - 1)
            text = page.get_text("text") or ""
            if consumed + len(text) > request.max_chars and nodes:
                next_page = page_number
                break
            page_nodes, page_citations, page_warnings = _extract_page(
                document, page, page_number, ref, request
            )
            warnings.extend(page_warnings)
            nodes.extend(page_nodes)
            citations.extend(page_citations)
            consumed += len(text)
            stats.tables += sum(1 for node in page_nodes if node.kind is NodeKind.TABLE)
            stats.images += sum(1 for node in page_nodes if node.kind is NodeKind.IMAGE)

        stats.chars = consumed
        stats.nodes = len(nodes)
        stats.elapsed_ms = int((time.monotonic() - started) * 1000)
        return ReadResponse(
            document=ref,
            mode=request.mode,
            nodes=nodes,
            citations=citations,
            truncated=next_page is not None,
            next_cursor=f"page:{next_page}" if next_page else None,
            warnings=warnings,
            stats=stats,
        )
    finally:
        document.close()


def _outline(document, ref: DocumentRef, pages: List[int], started: float, warnings: List[str]) -> ReadResponse:
    """Cheap TOC: bookmarks when present, otherwise per-page first strong line."""

    nodes: List[DocNode] = []
    try:
        toc = document.get_toc(simple=True)
    except Exception:  # pragma: no cover - malformed TOC
        toc = []
    seen = set()
    for level, title, page in toc:
        if title and page:
            seen.add(page)
            nodes.append(
                DocNode(
                    node_id=f"toc{len(nodes) + 1}",
                    kind=NodeKind.HEADING,
                    text=title,
                    level=max(1, min(int(level), 9)),
                    locator=Locator(kind=LocatorKind.PAGE, page=int(page), section=title),
                )
            )
    if not nodes:
        for page_number in pages[:200]:
            page = document.load_page(page_number - 1)
            first = (page.get_text("text") or "").strip().splitlines()[:1]
            title = first[0].strip() if first else f"Page {page_number}"
            nodes.append(
                DocNode(
                    node_id=f"p{page_number}",
                    kind=NodeKind.HEADING,
                    text=title[:160],
                    level=1,
                    locator=Locator(kind=LocatorKind.PAGE, page=page_number),
                )
            )
    return ReadResponse(
        document=ref,
        mode=ReadMode.OUTLINE,
        nodes=nodes,
        warnings=warnings,
        stats=ReadStats(
            nodes=len(nodes), pages=document.page_count, elapsed_ms=int((time.monotonic() - started) * 1000)
        ),
    )


def _extract_page(
    document, page, page_number: int, ref: DocumentRef, request: ReadRequest
) -> Tuple[List[DocNode], List[Citation], List[str]]:
    nodes: List[DocNode] = []
    citations: List[Citation] = []
    warnings: List[str] = []
    locator = Locator(kind=LocatorKind.PAGE, page=page_number)

    blocks = []
    try:
        payload = page.get_text("dict")
        blocks = [block for block in payload.get("blocks", []) if block.get("type") == 0]
    except Exception:
        warnings.append(f"page_{page_number}_block_extraction_failed")

    sizes: List[float] = []
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if str(span.get("text", "")).strip():
                    sizes.append(float(span.get("size", 0) or 0))
    body_size = median(sizes) if sizes else 0.0

    index = 0
    page_text_parts: List[str] = []
    for block in blocks:
        text_parts: List[str] = []
        bold = False
        max_size = 0.0
        for line in block.get("lines", []):
            line_text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
            if line_text.strip():
                text_parts.append(line_text)
            for span in line.get("spans", []):
                max_size = max(max_size, float(span.get("size", 0) or 0))
                if int(span.get("flags", 0)) & 16 or str(span.get("font", "")).lower().find("bold") >= 0:
                    bold = True
        text = "\n".join(text_parts).strip()
        if not text:
            continue
        index += 1
        page_text_parts.append(text)
        is_heading = (
            len(text) <= 120
            and body_size > 0
            and (max_size >= body_size * 1.18 or (bold and max_size >= body_size * 1.05))
        )
        nodes.append(
            DocNode(
                node_id=f"p{page_number}-b{index}",
                kind=NodeKind.HEADING if is_heading else NodeKind.PARAGRAPH,
                text=text,
                level=1 if is_heading else 0,
                locator=Locator(
                    kind=LocatorKind.PAGE,
                    page=page_number,
                    paragraph=index,
                ),
                style=f"size={round(max_size, 1)}{';bold' if bold else ''}",
            )
        )

    if request.include_tables:
        table_nodes = _extract_tables(page, page_number)
        nodes.extend(table_nodes)
        if table_nodes:
            page_text_parts.extend(
                "\n".join("\t".join(row) for row in node.table.rows)  # type: ignore[union-attr]
                for node in table_nodes
            )

    if request.include_images:
        image_nodes = _extract_images(document, page, page_number, ref)
        nodes.extend(image_nodes)

    page_text = "\n".join(page_text_parts).strip()
    if page_text:
        citations.append(
            make_citation(ref, locator, page_text[:600], content_hash=ref.content_hash)
        )
    else:
        warnings.append(f"page_{page_number}_no_text_layer")
    return nodes, citations, warnings


def _extract_tables(page, page_number: int) -> List[DocNode]:
    nodes: List[DocNode] = []
    try:
        finder = page.find_tables()
    except Exception:
        return nodes
    for table_index, table in enumerate(getattr(finder, "tables", []) or [], start=1):
        try:
            rows = [
                [("" if cell is None else str(cell).replace("\n", " ").strip()) for cell in row]
                for row in table.extract()
            ]
        except Exception:
            continue
        if not rows:
            continue
        nodes.append(
            DocNode(
                node_id=f"p{page_number}-t{table_index}",
                kind=NodeKind.TABLE,
                locator=Locator(kind=LocatorKind.PAGE, page=page_number),
                table=TableData(rows=rows[:200], header_rows=1, source_range=f"page {page_number}"),
            )
        )
    return nodes


def _extract_images(document, page, page_number: int, ref: DocumentRef) -> List[DocNode]:
    nodes: List[DocNode] = []
    try:
        images = page.get_images(full=True)
    except Exception:
        return nodes
    for image_index, image in enumerate(images, start=1):
        xref = int(image[0])
        width, height = int(image[2]), int(image[3])
        size_bytes = 0
        try:
            info = document.extract_image(xref)
            size_bytes = len(info.get("image", b""))
        except Exception:
            pass
        nodes.append(
            DocNode(
                node_id=f"p{page_number}-i{image_index}",
                kind=NodeKind.IMAGE,
                locator=Locator(kind=LocatorKind.IMAGE, page=page_number, image=str(xref)),
                image=ImageData(
                    asset_id=f"{ref.document_id}:{page_number}:{xref}",
                    mime_type="image/png",
                    width=width,
                    height=height,
                    size_bytes=size_bytes,
                ),
            )
        )
    return nodes


def read_path(path: Path, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    return read_pdf(path, ref, request)
