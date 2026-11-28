"""DOCX reader / writer (WP-04).

Reading walks the body in true document order (paragraphs and tables interleaved), reports
headings, paragraph runs, tables, images, headers/footers, sections and styles, and gives every
paragraph a 1-based body index locator that the writer accepts back as an edit target.

Writing re-opens the existing package and touches only the targeted XML elements, so styles,
headers, footers, numbering, section properties and any element the operation did not name are
preserved byte-for-byte where DOCX allows it.
"""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.knowledge.engines.base import ApplyResult
from app.knowledge.contracts import (
    ContentBlock,
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

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_HEADING_STYLE = re.compile(r"^(heading|제목)\s*([1-9])?$", re.IGNORECASE)


def _load(path: Path):
    from docx import Document

    try:
        return Document(str(path))
    except Exception as exc:  # zipfile.BadZipFile, KeyError, PackageNotFoundError
        name = type(exc).__name__
        if isinstance(exc, FileNotFoundError):
            raise KnowledgeError(KnowledgeErrorCode.DOCUMENT_NOT_FOUND, "file missing") from exc
        raise KnowledgeError(
            KnowledgeErrorCode.CORRUPT_CONTENT,
            "docx package could not be opened",
            detail={"reason": name},
        ) from exc


def _heading_level(style_name: str, paragraph) -> int:
    match = _HEADING_STYLE.match((style_name or "").strip())
    if match:
        return int(match.group(2) or 1)
    try:
        ppr = paragraph._p.pPr
        if ppr is not None and ppr.outlineLvl is not None:
            return min(9, int(ppr.outlineLvl.val) + 1)
    except Exception:
        pass
    return 0


def _paragraph_node(paragraph, index: int, section_path: str, doc) -> DocNode:
    style_name = getattr(getattr(paragraph, "style", None), "name", "") or ""
    level = _heading_level(style_name, paragraph)
    runs = [
        {
            "text": run.text,
            "bold": bool(run.bold),
            "italic": bool(run.italic),
            "underline": bool(run.underline),
            "size_pt": run.font.size.pt if run.font.size else None,
            "font": run.font.name,
        }
        for run in paragraph.runs
    ]
    has_image = bool(paragraph._p.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"))
    kind = NodeKind.HEADING if level else NodeKind.PARAGRAPH
    node = DocNode(
        node_id=f"p{index}",
        kind=kind,
        text=paragraph.text,
        level=level,
        style=style_name,
        locator=Locator(
            kind=LocatorKind.PARAGRAPH,
            paragraph=index,
            section=section_path or None,
        ),
        meta={
            "runs": runs,
            "alignment": str(paragraph.alignment) if paragraph.alignment is not None else None,
            "list_style": style_name if "list" in style_name.lower() else None,
            "has_image": has_image,
        },
    )
    if has_image and not paragraph.text.strip():
        images = _inline_images(paragraph, doc)
        if images:
            node.kind = NodeKind.IMAGE
            node.image = images[0]
            node.meta["images"] = [image.model_dump(mode="json") for image in images]
    return node


def _inline_images(paragraph, doc) -> List[ImageData]:
    images: List[ImageData] = []
    blips = paragraph._p.findall(
        ".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
    )
    for blip in blips:
        rel_id = blip.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
        )
        if not rel_id:
            continue
        try:
            part = doc.part.related_parts[rel_id]
            width = height = None
            extent = blip.getparent()
            # extents live on the ancestor <wp:extent>; walk up a few levels defensively
            cursor = blip
            for _ in range(6):
                if cursor is None:
                    break
                if cursor.tag.endswith("}extent"):
                    width = int(int(cursor.get("cx", 0)) / 9525)
                    height = int(int(cursor.get("cy", 0)) / 9525)
                    break
                cursor = cursor.getparent()
            images.append(
                ImageData(
                    asset_id=rel_id,
                    mime_type=getattr(part, "content_type", "image/png"),
                    width=width,
                    height=height,
                    size_bytes=len(getattr(part, "blob", b"")),
                    alt_text="",
                )
            )
        except Exception:
            continue
    return images


def _table_node(table, index: int, section_path: str) -> DocNode:
    rows: List[List[str]] = []
    merged_cells = 0
    for row in table.rows:
        values: List[str] = []
        for cell in row.cells:
            try:
                if cell._tc.grid_span and cell._tc.grid_span > 1:
                    merged_cells += 1
            except Exception:
                pass
            values.append(cell.text.replace("\n", " ").strip())
        rows.append(values)
    style = ""
    try:
        style = table.style.name if table.style else ""
    except Exception:
        style = ""
    return DocNode(
        node_id=f"t{index}",
        kind=NodeKind.TABLE,
        locator=Locator(kind=LocatorKind.DOCUMENT, section=section_path or None),
        table=TableData(rows=rows, header_rows=1, caption=""),
        style=style,
        meta={"merged_cells": merged_cells, "style": style},
    )


def read_docx(path: Path, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    document = _load(path)
    nodes: List[DocNode] = []
    citations: List = []
    warnings: List[str] = []
    section_path = ""
    paragraph_index = 0
    table_index = 0
    consumed = 0

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            from docx.text.paragraph import Paragraph

            paragraph = Paragraph(child, document)
            paragraph_index += 1
            node = _paragraph_node(paragraph, paragraph_index, section_path, document)
            if node.kind is NodeKind.HEADING:
                section_path = (section_path.split(" > ")[0] if node.level == 1 else section_path)
                section_path = (
                    f"{section_path} > {node.text}" if section_path and node.level > 1 else node.text
                )
            if request.mode is ReadMode.OUTLINE and node.kind is not NodeKind.HEADING:
                continue
            nodes.append(node)
            consumed += len(node.text)
        elif tag == "tbl" and request.include_tables and request.mode is not ReadMode.OUTLINE:
            from docx.table import Table

            table_index += 1
            nodes.append(_table_node(Table(child, document), table_index, section_path))
            consumed += 1
        if consumed > request.max_chars:
            warnings.append("max_chars_reached")
            break

    if request.mode is not ReadMode.OUTLINE:
        for section_index, section in enumerate(document.sections, start=1):
            for part_name in ("header", "footer"):
                part = getattr(section, part_name, None)
                if part is None:
                    continue
                text = "\n".join(p.text for p in part.paragraphs if p.text.strip()).strip()
                if text:
                    nodes.append(
                        DocNode(
                            node_id=f"s{section_index}-{part_name}",
                            kind=NodeKind.NOTE,
                            text=text,
                            locator=Locator(kind=LocatorKind.SECTION, section=f"section {section_index} {part_name}"),
                            meta={"part": part_name, "section_index": section_index},
                        )
                    )

    text_for_citation = "\n".join(node.text for node in nodes if node.text).strip()
    if text_for_citation:
        citations.append(make_citation(ref, Locator(kind=LocatorKind.DOCUMENT), text_for_citation[:600]))

    styles = sorted({node.style for node in nodes if node.style})
    nodes.append(
        DocNode(
            node_id="meta-styles",
            kind=NodeKind.METADATA,
            locator=Locator(kind=LocatorKind.DOCUMENT),
            meta={
                "styles": styles,
                "sections": [
                    {
                        "index": index,
                        "orientation": str(section.orientation),
                        "page_width_cm": round(section.page_width.cm, 2) if section.page_width else None,
                        "page_height_cm": round(section.page_height.cm, 2) if section.page_height else None,
                        "header_distance_cm": round(section.header_distance.cm, 2) if section.header_distance else None,
                    }
                    for index, section in enumerate(document.sections, start=1)
                ],
                "paragraphs": paragraph_index,
                "tables": table_index,
            },
        )
    )

    return ReadResponse(
        document=ref,
        mode=request.mode,
        nodes=nodes,
        citations=citations,
        truncated="max_chars_reached" in warnings,
        warnings=warnings,
        stats=ReadStats(
            chars=min(consumed, request.max_chars),
            nodes=len(nodes),
            tables=table_index,
            images=sum(1 for node in nodes if node.kind is NodeKind.IMAGE),
        ),
    )


# --------------------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------------------


def _style_for_heading(document, level: int) -> str:
    for candidate in (f"Heading {level}", f"제목 {level}", "Heading 1", "Title"):
        try:
            document.styles[candidate]
            return candidate
        except KeyError:
            continue
    return "Normal"


def _image_bytes(block: ContentBlock, assets: Optional[Dict[str, bytes]]) -> Optional[bytes]:
    if block.image_base64:
        try:
            return base64.b64decode(block.image_base64.split(",", 1)[-1])
        except Exception as exc:
            raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "invalid image_base64") from exc
    if block.asset_id and assets and block.asset_id in assets:
        return assets[block.asset_id]
    return None


def create_docx(
    blocks: List[ContentBlock],
    *,
    template: Optional[Path] = None,
    assets: Optional[Dict[str, bytes]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> bytes:
    from docx import Document
    from docx.shared import Cm

    document = Document(str(template)) if template else Document()
    if metadata:
        _apply_metadata(document, metadata)
    for block in blocks:
        if block.kind.value == "heading":
            document.add_heading(block.text, level=max(1, min(block.level, 9)))
        elif block.kind.value == "paragraph":
            document.add_paragraph(block.text)
        elif block.kind.value == "bullets":
            for bullet in block.bullets:
                document.add_paragraph(bullet, style="List Bullet")
        elif block.kind.value == "table":
            if not block.rows:
                continue
            columns = max(len(row) for row in block.rows)
            table = document.add_table(rows=len(block.rows), cols=columns)
            table.style = "Table Grid"
            for row_index, row in enumerate(block.rows):
                for column_index in range(columns):
                    value = row[column_index] if column_index < len(row) else ""
                    table.cell(row_index, column_index).text = str(value)
        elif block.kind.value == "image":
            data = _image_bytes(block, assets)
            if data:
                width = Cm(block.width_cm) if block.width_cm else None
                document.add_picture(io.BytesIO(data), width=width)
            else:
                raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "image block without data")
        elif block.kind.value == "page_break":
            document.add_page_break()
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _apply_metadata(document, metadata: Dict[str, Any]) -> None:
    properties = document.core_properties
    for key in ("title", "author", "subject", "keywords", "comments", "category"):
        if key in metadata and metadata[key] is not None:
            setattr(properties, key, str(metadata[key]))


def _replace_in_paragraph(paragraph, find: str, replace: str, all_occurrences: bool) -> int:
    """Replace text run-aware when possible, otherwise inside the paragraph text.

    Run-aware replacement preserves each run's formatting. When the needle crosses run
    boundaries we rewrite the paragraph text into the first run and clear the others, which is
    the only option DOCX offers without a full text-mapping pass; the paragraph style itself is
    preserved.
    """

    count = 0
    for run in paragraph.runs:
        if find and find in run.text:
            run.text = run.text.replace(find, replace) if all_occurrences else run.text.replace(find, replace, 1)
            count += run.text.count(replace) if all_occurrences else 1
    if count:
        return count
    text = paragraph.text
    if find and find in text:
        replacements = text.count(find) if all_occurrences else 1
        new_text = text.replace(find, replace) if all_occurrences else text.replace(find, replace, 1)
        if paragraph.runs:
            paragraph.runs[0].text = new_text
            for run in paragraph.runs[1:]:
                run.text = ""
        else:
            paragraph.text = new_text
        return replacements
    return 0


def _body_paragraph(document, index: int):
    from docx.text.paragraph import Paragraph

    counter = 0
    for child in document.element.body.iterchildren():
        if child.tag.rsplit("}", 1)[-1] == "p":
            counter += 1
            if counter == index:
                return Paragraph(child, document)
    return None


def apply_docx(
    path: Path,
    ref: DocumentRef,
    operations: List[Any],
) -> "ApplyResult":
    from docx.shared import Cm

    document = _load(path)
    applied: List[str] = []
    skipped: List[str] = []
    warnings: List[str] = []
    diff: List[str] = []

    for op_index, operation in enumerate(operations, start=1):
        kind = operation.op.value
        if kind == "replace_text":
            total = 0
            targets = list(document.paragraphs)
            for table in document.tables:
                for row in table.rows:
                    for cell in row.cells:
                        targets.extend(cell.paragraphs)
            for section in document.sections:
                for part in (section.header, section.footer):
                    if part is not None:
                        targets.extend(part.paragraphs)
            for paragraph in targets:
                total += _replace_in_paragraph(paragraph, operation.find or "", operation.replace or "", operation.all_occurrences)
            if total:
                applied.append(f"replace_text:{total}")
                diff.append(f"replaced {total} occurrence(s) of {operation.find!r}")
            else:
                skipped.append(f"replace_text:not_found:{operation.find!r}")
        elif kind == "insert_paragraph":
            anchor = _body_paragraph(document, operation.paragraph) if operation.paragraph else None
            style = _style_for_heading(document, operation.level) if operation.level > 1 else None
            if anchor is not None:
                new_paragraph = anchor.insert_paragraph_before(operation.text or "", style=style)
                applied.append("insert_paragraph")
            else:
                document.add_paragraph(operation.text or "", style=style)
                applied.append("insert_paragraph:append")
            diff.append(f"inserted paragraph {operation.text[:60]!r}")
        elif kind == "delete_paragraph":
            target = _body_paragraph(document, operation.paragraph or 0)
            if target is None:
                skipped.append("delete_paragraph:not_found")
            else:
                element = target._p
                element.getparent().remove(element)
                applied.append("delete_paragraph")
                diff.append(f"deleted paragraph {operation.paragraph}")
        elif kind == "insert_image":
            data = None
            if operation.image_base64:
                data = base64.b64decode(operation.image_base64.split(",", 1)[-1])
            if not data:
                skipped.append("insert_image:no_data")
                continue
            width = Cm(operation.width_cm) if operation.width_cm else None
            document.add_picture(io.BytesIO(data), width=width)
            applied.append("insert_image")
            diff.append("inserted image")
        elif kind == "replace_image":
            if not operation.asset_id or not operation.image_base64:
                skipped.append("replace_image:missing_asset_or_data")
                continue
            try:
                part = document.part.related_parts[operation.asset_id]
                part._blob = base64.b64decode(operation.image_base64.split(",", 1)[-1])
                applied.append("replace_image")
                diff.append(f"replaced image {operation.asset_id}")
            except KeyError:
                skipped.append("replace_image:asset_not_found")
        elif kind == "add_table":
            if not operation.rows:
                skipped.append("add_table:no_rows")
                continue
            columns = max(len(row) for row in operation.rows)
            table = document.add_table(rows=len(operation.rows), cols=columns)
            table.style = "Table Grid"
            for row_index, row in enumerate(operation.rows):
                for column_index in range(columns):
                    table.cell(row_index, column_index).text = str(
                        row[column_index] if column_index < len(row) else ""
                    )
            applied.append("add_table")
            diff.append(f"added table {len(operation.rows)}x{columns}")
        elif kind == "set_metadata":
            _apply_metadata(document, operation.metadata)
            applied.append("set_metadata")
            diff.append("updated core properties")
        else:
            skipped.append(f"{kind}:unsupported_for_docx")

    buffer = io.BytesIO()
    document.save(buffer)
    return ApplyResult(buffer.getvalue(), applied, skipped, warnings, diff)


def read_path(path: Path, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    return read_docx(path, ref, request)
