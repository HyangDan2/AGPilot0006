"""WP-04 DOCX reader/writer: structure, images, and non-destructive editing."""

from __future__ import annotations

import base64
import io

import pytest
from docx import Document

from app.knowledge.contracts import (
    ContentBlock,
    DocumentRef,
    Locator,
    LocatorKind,
    NodeKind,
    OpKind,
    ReadMode,
    ReadRequest,
    UpdateOperation,
)
from app.knowledge.engines import docx_engine
from tests.knowledge_fixtures import make_docx, png_bytes

DOCX_MIME = docx_engine.DOCX_MIME


def _ref() -> DocumentRef:
    return DocumentRef(document_id="kd_docxtest001", name="sample.docx", mime_type=DOCX_MIME, revision=1)


def test_reads_headings_paragraphs_runs_tables_and_images(tmp_path):
    path = make_docx(tmp_path / "sample.docx")
    response = docx_engine.read_path(path, _ref(), ReadRequest(document_id="kd_docxtest001", include_images=True))
    kinds = [node.kind for node in response.nodes]
    assert NodeKind.HEADING in kinds and NodeKind.TABLE in kinds and NodeKind.IMAGE in kinds
    heading = next(node for node in response.nodes if node.kind is NodeKind.HEADING)
    assert heading.text == "R21 Knowledge Engine" and heading.level == 1
    paragraph = next(
        node for node in response.nodes if node.kind is NodeKind.PARAGRAPH and "structured" in node.text
    )
    runs = paragraph.meta["runs"]
    assert any(run["bold"] for run in runs)
    table = next(node for node in response.nodes if node.kind is NodeKind.TABLE)
    assert table.table.rows[1] == ["Widget", "3", "1200"]
    # header text is captured as a note node, and paragraph locators are 1-based body indexes
    assert any(node.kind is NodeKind.NOTE and "HD2Lab Confidential" in node.text for node in response.nodes)
    assert paragraph.locator.kind is LocatorKind.PARAGRAPH and paragraph.locator.paragraph >= 1


def test_outline_lists_only_headings(tmp_path):
    path = make_docx(tmp_path / "sample.docx")
    response = docx_engine.read_path(path, _ref(), ReadRequest(document_id="kd_docxtest001", mode=ReadMode.OUTLINE))
    headings = [node.text for node in response.nodes if node.kind is NodeKind.HEADING]
    assert headings == ["R21 Knowledge Engine", "Table Section"]


def test_replace_text_preserves_header_styles_and_untouched_content(tmp_path):
    path = make_docx(tmp_path / "sample.docx")
    result = docx_engine.apply_docx(
        path,
        _ref(),
        [UpdateOperation(op=OpKind.REPLACE_TEXT, find="closing paragraph", replace="final paragraph")],
    )
    assert result.applied and result.changed
    edited = tmp_path / "edited.docx"
    edited.write_bytes(result.data)
    document = Document(str(edited))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "A final paragraph that must survive edits." in text
    assert document.sections[0].header.paragraphs[0].text == "HD2Lab Confidential"
    assert len(document.tables) == 1
    assert len(document.inline_shapes) == 1
    assert document.paragraphs[0].style.name.startswith("Heading")


def test_insert_and_delete_paragraph_by_body_index(tmp_path):
    path = make_docx(tmp_path / "sample.docx")
    result = docx_engine.apply_docx(
        path,
        _ref(),
        [
            UpdateOperation(op=OpKind.INSERT_PARAGRAPH, paragraph=3, text="Inserted paragraph", level=1),
            UpdateOperation(op=OpKind.DELETE_PARAGRAPH, paragraph=1),
        ],
    )
    edited = tmp_path / "structured.docx"
    edited.write_bytes(result.data)
    document = Document(str(edited))
    texts = [paragraph.text for paragraph in document.paragraphs]
    assert "Inserted paragraph" in texts
    assert "R21 Knowledge Engine" not in texts  # the first body paragraph was the heading


def test_add_table_and_image(tmp_path):
    path = make_docx(tmp_path / "sample.docx")
    result = docx_engine.apply_docx(
        path,
        _ref(),
        [
            UpdateOperation(op=OpKind.ADD_TABLE, rows=[["A", "B"], ["1", "2"]]),
            UpdateOperation(
                op=OpKind.INSERT_IMAGE,
                image_base64=base64.b64encode(png_bytes()).decode(),
            ),
        ],
    )
    edited = tmp_path / "added.docx"
    edited.write_bytes(result.data)
    document = Document(str(edited))
    assert len(document.tables) == 2
    assert len(document.inline_shapes) == 2


def test_create_from_blocks_with_template(tmp_path):
    template = make_docx(tmp_path / "template.docx")
    blocks = [
        ContentBlock(kind="heading", text="Generated Report", level=1),
        ContentBlock(kind="paragraph", text="Produced by the knowledge engine."),
        ContentBlock(kind="bullets", bullets=["first", "second"]),
        ContentBlock(kind="table", rows=[["K", "V"], ["a", "1"]], header_rows=1),
    ]
    data = docx_engine.create_docx(blocks, template=template, metadata={"title": "R21", "author": "HD2"})
    produced = tmp_path / "produced.docx"
    produced.write_bytes(data)
    document = Document(str(produced))
    texts = [paragraph.text for paragraph in document.paragraphs]
    assert "Generated Report" in texts and "Produced by the knowledge engine." in texts
    assert document.core_properties.title == "R21"
    assert len(document.tables) == 2  # template table kept + new table
    assert document.sections[0].header.paragraphs[0].text == "HD2Lab Confidential"


def test_unsupported_operation_is_reported_not_fatal(tmp_path):
    path = make_docx(tmp_path / "sample.docx")
    result = docx_engine.apply_docx(
        path, _ref(), [UpdateOperation(op=OpKind.SET_CELL, sheet="Data", cell="A1", value="x")]
    )
    assert result.skipped and "unsupported_for_docx" in result.skipped[0]
    assert result.applied == []
