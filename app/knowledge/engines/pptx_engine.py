"""PPTX reader / writer (WP-06).

Reading reports slide geometry, master/layout names, theme colours, every shape (text box,
table, chart, picture, placeholder) with slide-level citations, and speaker notes. Writing
supports template-based creation, slide add/delete/reorder, run-aware text replacement,
images and core properties while leaving layouts and themes intact.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    ChartData,
    make_citation,
)
from app.knowledge.engines.base import ApplyResult

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _load(path: Path):
    from pptx import Presentation

    try:
        return Presentation(str(path))
    except Exception as exc:
        name = type(exc).__name__
        if "encrypt" in str(exc).lower() or "password" in str(exc).lower():
            raise KnowledgeError(KnowledgeErrorCode.ENCRYPTED_DOCUMENT, "presentation is encrypted") from exc
        raise KnowledgeError(
            KnowledgeErrorCode.CORRUPT_CONTENT,
            "pptx package could not be opened",
            detail={"reason": name},
        ) from exc


def _theme_colors(presentation) -> Dict[str, str]:
    colors: Dict[str, str] = {}
    try:
        from lxml import etree

        for part in presentation.part.package.iter_parts():
            if not str(part.partname).startswith("/ppt/theme/"):
                continue
            root = etree.fromstring(part.blob)
            ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
            scheme = root.find(".//a:clrScheme", ns)
            if scheme is None:
                continue
            for child in scheme:
                name = etree.QName(child).localname
                srgb = child.find("a:srgbClr", ns)
                sys_color = child.find("a:sysClr", ns)
                if srgb is not None:
                    colors[name] = "#" + (srgb.get("val") or "")
                elif sys_color is not None:
                    colors[name] = "#" + (sys_color.get("lastClr") or "")
            break
    except Exception:
        return colors
    return colors


def _shape_node(shape, slide_index: int, shape_index: int, presentation) -> Optional[DocNode]:
    locator = Locator(
        kind=LocatorKind.SLIDE,
        slide=slide_index,
        shape=getattr(shape, "name", None) or f"shape{shape_index}",
    )
    node_id = f"s{slide_index}-sh{shape_index}"
    if getattr(shape, "has_table", False) and shape.has_table:
        rows = [
            [cell.text.replace("\n", " ").strip() for cell in row.cells] for row in shape.table.rows
        ]
        return DocNode(
            node_id=node_id,
            kind=NodeKind.TABLE,
            locator=locator,
            table=TableData(rows=rows, header_rows=1, caption=getattr(shape, "name", "")),
            style=getattr(shape, "name", ""),
        )
    if getattr(shape, "has_chart", False) and shape.has_chart:
        chart = shape.chart
        series = []
        for item in chart.series:
            name = ""
            try:
                name = item.name or ""
            except Exception:
                name = ""
            series.append({"name": name, "values": list(item.values)})
        categories = [str(category) for category in getattr(chart.plots[0], "categories", [])] if getattr(chart, "plots", None) else []
        return DocNode(
            node_id=node_id,
            kind=NodeKind.CHART,
            locator=locator,
            chart=ChartData(
                chart_type=str(getattr(chart, "chart_type", "unknown")),
                title=getattr(getattr(chart, "chart_title", None), "text_frame", None)
                and chart.chart_title.text_frame.text
                or "",
                categories=categories,
                series=series,
            ),
        )
    if getattr(shape, "shape_type", None) is not None and str(shape.shape_type).startswith("PICTURE"):
        try:
            image = shape.image
            image_data = ImageData(
                asset_id=getattr(shape, "name", node_id),
                mime_type=image.content_type,
                width=getattr(shape, "width", None),
                height=getattr(shape, "height", None),
                size_bytes=len(image.blob),
                alt_text=getattr(shape, "name", ""),
            )
        except Exception:
            image_data = ImageData(asset_id=node_id, mime_type="image/png")
        return DocNode(
            node_id=node_id, kind=NodeKind.IMAGE, locator=locator, image=image_data,
            text=getattr(shape, "name", ""),
        )
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        paragraphs = []
        style_bits = []
        for paragraph in shape.text_frame.paragraphs:
            text = paragraph.text
            if text.strip():
                paragraphs.append(text)
                style_bits.append(
                    f"lvl={paragraph.level};"
                    + ",".join(
                        bit
                        for bit, on in (
                            ("bold", any(run.font.bold for run in paragraph.runs)),
                            ("italic", any(run.font.italic for run in paragraph.runs)),
                        )
                        if on
                    )
                )
        if not paragraphs:
            return None
        return DocNode(
            node_id=node_id,
            kind=NodeKind.PARAGRAPH,
            text="\n".join(paragraphs),
            locator=locator,
            style=";".join(style_bits)[:120],
            meta={"placeholder": getattr(getattr(shape, "placeholder_format", None), "type", None) is not None},
        )
    return None


def read_pptx(path: Path, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    presentation = _load(path)
    nodes: List[DocNode] = []
    citations: List = []
    warnings: List[str] = []
    slides = list(presentation.slides)
    if request.mode is ReadMode.SLIDE and request.slide:
        if not 1 <= request.slide <= len(slides):
            raise KnowledgeError(
                KnowledgeErrorCode.INVALID_REQUEST,
                "slide out of range",
                detail={"slide": request.slide, "total": len(slides)},
            )
        targets = [(request.slide, slides[request.slide - 1])]
    else:
        targets = list(enumerate(slides, start=1))

    stats = ReadStats(slides=len(slides))
    consumed = 0
    for slide_index, slide in targets:
        slide_text_parts: List[str] = []
        layout = getattr(slide, "slide_layout", None)
        heading = DocNode(
            node_id=f"s{slide_index}-layout",
            kind=NodeKind.METADATA,
            locator=Locator(kind=LocatorKind.SLIDE, slide=slide_index),
            meta={
                "layout": getattr(layout, "name", None),
                "master": getattr(getattr(layout, "slide_master", None), "name", None),
            },
        )
        nodes.append(heading)
        if request.mode is ReadMode.OUTLINE:
            title = ""
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.has_text_frame and shape.text_frame.text.strip():
                    title = shape.text_frame.text.strip().splitlines()[0]
                    break
            nodes.append(
                DocNode(
                    node_id=f"s{slide_index}-title",
                    kind=NodeKind.HEADING,
                    text=title or f"Slide {slide_index}",
                    level=1,
                    locator=Locator(kind=LocatorKind.SLIDE, slide=slide_index),
                )
            )
            continue

        shape_index = 0
        for shape in slide.shapes:
            shape_index += 1
            node = _shape_node(shape, slide_index, shape_index, presentation)
            if node is None:
                continue
            nodes.append(node)
            if node.text or node.kind is NodeKind.TABLE or node.kind is NodeKind.CHART:
                slide_text_parts.append(node.text or "")
            consumed += len(node.text)
            stats.tables += 1 if node.kind is NodeKind.TABLE else 0
            stats.images += 1 if node.kind is NodeKind.IMAGE else 0

        if request.include_notes:
            try:
                notes_frame = slide.notes_slide.notes_text_frame
                notes = notes_frame.text.strip() if notes_frame is not None else ""
            except Exception:
                notes = ""
            if notes:
                nodes.append(
                    DocNode(
                        node_id=f"s{slide_index}-notes",
                        kind=NodeKind.NOTE,
                        text=notes,
                        locator=Locator(kind=LocatorKind.SLIDE, slide=slide_index),
                        meta={"part": "speaker_notes"},
                    )
                )
        slide_text = " ".join(part for part in slide_text_parts if part).strip()
        if slide_text:
            citations.append(
                make_citation(ref, Locator(kind=LocatorKind.SLIDE, slide=slide_index), slide_text[:600])
            )
        if consumed > request.max_chars:
            warnings.append("max_chars_reached")
            break

    if request.mode is not ReadMode.SLIDE:
        nodes.append(
            DocNode(
                node_id="meta-presentation",
                kind=NodeKind.METADATA,
                locator=Locator(kind=LocatorKind.DOCUMENT),
                meta={
                    "slide_width": presentation.slide_width,
                    "slide_height": presentation.slide_height,
                    "masters": [master.name for master in presentation.slide_masters],
                    "layouts": sorted(
                        {
                            layout.name
                            for master in presentation.slide_masters
                            for layout in master.slide_layouts
                        }
                    ),
                    "theme_colors": _theme_colors(presentation),
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
        stats=stats,
    )


def _layout(presentation, spec: Any):
    layouts = list(presentation.slide_layouts)
    if spec is None:
        return layouts[min(1, len(layouts) - 1)] if layouts else None
    if isinstance(spec, int):
        if 0 <= spec < len(layouts):
            return layouts[spec]
        return layouts[min(1, len(layouts) - 1)]
    if isinstance(spec, str):
        for layout in layouts:
            if layout.name.strip().lower() == spec.strip().lower():
                return layout
    return layouts[min(1, len(layouts) - 1)] if layouts else None


def create_pptx(
    blocks: List[ContentBlock],
    *,
    template: Optional[Path] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation(str(template)) if template else Presentation()
    if metadata:
        _apply_metadata(presentation, metadata)
    if template:
        # Template slides are kept; content blocks append after them.
        pass
    layout = _layout(presentation, None)
    slide = presentation.slides.add_slide(layout) if layout is not None else None
    cursor_top = Inches(1.6)
    for block in blocks:
        if block.kind.value == "heading":
            if slide is None:
                slide = presentation.slides.add_slide(_layout(presentation, None))
            if slide.shapes.title is not None:
                slide.shapes.title.text = block.text
            else:
                box = slide.shapes.add_textbox(Inches(0.6), cursor_top, Inches(8.8), Inches(0.8))
                box.text_frame.text = block.text
                cursor_top += Inches(0.9)
        elif block.kind.value in ("paragraph", "bullets"):
            if slide is None:
                slide = presentation.slides.add_slide(_layout(presentation, None))
            box = slide.shapes.add_textbox(Inches(0.6), cursor_top, Inches(8.8), Inches(1.2))
            frame = box.text_frame
            frame.word_wrap = True
            texts = block.bullets if block.kind.value == "bullets" else [block.text]
            for index, text in enumerate(texts):
                paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                paragraph.text = text
            cursor_top += Inches(1.4)
        elif block.kind.value == "table" and block.rows:
            slide = slide or presentation.slides.add_slide(_layout(presentation, None))
            columns = max(len(row) for row in block.rows)
            shape = slide.shapes.add_table(
                len(block.rows), columns, Inches(0.6), cursor_top, Inches(8.8), Inches(0.4) * len(block.rows)
            )
            for row_index, row in enumerate(block.rows):
                for column_index in range(columns):
                    shape.table.cell(row_index, column_index).text = str(
                        row[column_index] if column_index < len(row) else ""
                    )
            cursor_top += Inches(0.4) * len(block.rows) + Inches(0.3)
        elif block.kind.value == "image" and block.image_base64:
            slide = slide or presentation.slides.add_slide(_layout(presentation, None))
            data = base64.b64decode(block.image_base64.split(",", 1)[-1])
            slide.shapes.add_picture(io.BytesIO(data), Inches(0.6), cursor_top, width=Inches(6))
            cursor_top += Inches(3)
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _apply_metadata(presentation, metadata: Dict[str, Any]) -> None:
    properties = presentation.core_properties
    for key in ("title", "author", "subject", "keywords", "comments", "category"):
        if key in metadata and metadata[key] is not None:
            setattr(properties, key, str(metadata[key]))


def _slide_id_list(presentation):
    return presentation.slides._sldIdLst


def apply_pptx(path: Path, ref: DocumentRef, operations: List[Any]) -> ApplyResult:
    from pptx.util import Inches

    presentation = _load(path)
    applied: List[str] = []
    skipped: List[str] = []
    warnings: List[str] = []
    diff: List[str] = []

    for operation in operations:
        kind = operation.op.value
        if kind == "add_slide":
            layout = _layout(presentation, operation.layout)
            if layout is None:
                skipped.append("add_slide:no_layout")
                continue
            slide = presentation.slides.add_slide(layout)
            if operation.text and slide.shapes.title is not None:
                slide.shapes.title.text = operation.text
            bullets = operation.bullets
            if bullets:
                body = None
                for shape in slide.placeholders:
                    if shape.placeholder_format.idx == 1:
                        body = shape
                        break
                if body is not None and body.has_text_frame:
                    frame = body.text_frame
                    for index, bullet in enumerate(bullets):
                        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                        paragraph.text = bullet
            applied.append("add_slide")
            diff.append(f"added slide using layout {layout.name}")
        elif kind == "delete_slide":
            slides = list(presentation.slides)
            if not operation.slide or operation.slide > len(slides):
                skipped.append("delete_slide:out_of_range")
                continue
            slide = slides[operation.slide - 1]
            slide_id_list = _slide_id_list(presentation)
            for element in list(slide_id_list):
                if int(element.get("id")) == slide.slide_id:
                    slide_id_list.remove(element)
                    break
            applied.append("delete_slide")
            diff.append(f"deleted slide {operation.slide}")
        elif kind == "move_slide":
            slides = list(presentation.slides)
            if (
                operation.from_index is None
                or operation.to_index is None
                or max(operation.from_index, operation.to_index) >= len(slides)
            ):
                skipped.append("move_slide:out_of_range")
                continue
            slide_id_list = _slide_id_list(presentation)
            element = list(slide_id_list)[operation.from_index]
            slide_id_list.remove(element)
            slide_id_list.insert(operation.to_index, element)
            applied.append("move_slide")
            diff.append(f"moved slide {operation.from_index} -> {operation.to_index}")
        elif kind == "replace_text":
            total = 0
            for slide in presentation.slides:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                        for paragraph in shape.text_frame.paragraphs:
                            for run in paragraph.runs:
                                if operation.find and operation.find in run.text:
                                    run.text = (
                                        run.text.replace(operation.find, operation.replace or "")
                                        if operation.all_occurrences
                                        else run.text.replace(operation.find, operation.replace or "", 1)
                                    )
                                    total += 1
                            if operation.find and operation.find in paragraph.text and not paragraph.runs:
                                paragraph.text = paragraph.text.replace(
                                    operation.find, operation.replace or ""
                                )
                                total += 1
            if total:
                applied.append(f"replace_text:{total}")
                diff.append(f"replaced {total} run(s) of {operation.find!r}")
            else:
                skipped.append(f"replace_text:not_found:{operation.find!r}")
        elif kind == "insert_image":
            if not operation.image_base64:
                skipped.append("insert_image:no_data")
                continue
            slides = list(presentation.slides)
            slide = slides[(operation.slide or len(slides)) - 1]
            data = base64.b64decode(operation.image_base64.split(",", 1)[-1])
            slide.shapes.add_picture(io.BytesIO(data), Inches(1), Inches(1), width=Inches(6))
            applied.append("insert_image")
            diff.append("inserted image")
        elif kind == "replace_image":
            skipped.append("replace_image:identify_shape_first")
        elif kind == "set_metadata":
            _apply_metadata(presentation, operation.metadata)
            applied.append("set_metadata")
            diff.append("updated core properties")
        else:
            skipped.append(f"{kind}:unsupported_for_pptx")

    buffer = io.BytesIO()
    presentation.save(buffer)
    return ApplyResult(buffer.getvalue(), applied, skipped, warnings, diff)


def read_path(path: Path, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
    return read_pptx(path, ref, request)
