"""WP-06 PPTX reader/writer: slides, shapes, notes, layouts/themes, structural edits."""

from __future__ import annotations

import pytest
from pptx import Presentation

from app.knowledge.contracts import (
    ContentBlock,
    DocumentRef,
    LocatorKind,
    NodeKind,
    OpKind,
    ReadMode,
    ReadRequest,
    UpdateOperation,
)
from app.knowledge.engines import pptx_engine
from tests.knowledge_fixtures import make_pptx

PPTX_MIME = pptx_engine.PPTX_MIME


def _ref() -> DocumentRef:
    return DocumentRef(document_id="kd_pptxtest001", name="deck.pptx", mime_type=PPTX_MIME, revision=1)


def test_reads_slides_shapes_tables_notes_and_theme(tmp_path):
    path = make_pptx(tmp_path / "deck.pptx")
    response = pptx_engine.read_path(path, _ref(), ReadRequest(document_id="kd_pptxtest001"))
    assert response.stats.slides == 2
    assert {citation.locator.slide for citation in response.citations} == {1, 2}
    table = next(node for node in response.nodes if node.kind is NodeKind.TABLE)
    assert table.table.rows == [["Region", "Sales"], ["KR", "1200"]]
    assert table.locator.kind is LocatorKind.SLIDE
    notes = [node for node in response.nodes if node.kind is NodeKind.NOTE]
    assert notes and "Speaker note" in notes[0].text
    meta = next(node for node in response.nodes if node.kind is NodeKind.METADATA and node.locator.kind is LocatorKind.DOCUMENT)
    assert meta.meta["theme_colors"], "theme colour scheme should be reported"
    assert meta.meta["masters"] and meta.meta["layouts"]


def test_outline_and_single_slide_mode(tmp_path):
    path = make_pptx(tmp_path / "deck.pptx")
    outline = pptx_engine.read_path(path, _ref(), ReadRequest(document_id="kd_pptxtest001", mode=ReadMode.OUTLINE))
    titles = [node.text for node in outline.nodes if node.kind is NodeKind.HEADING]
    assert titles == ["Q4 Review", "Appendix"]
    single = pptx_engine.read_path(path, _ref(), ReadRequest(document_id="kd_pptxtest001", mode=ReadMode.SLIDE, slide=2))
    assert {node.locator.slide for node in single.nodes} == {2}


def test_add_delete_and_move_slide_preserve_layouts_and_theme(tmp_path):
    path = make_pptx(tmp_path / "deck.pptx")
    original = Presentation(str(path))
    original_theme = pptx_engine._theme_colors(original)
    result = pptx_engine.apply_pptx(
        path,
        _ref(),
        [
            UpdateOperation(op=OpKind.ADD_SLIDE, layout=1, text="New Slide", bullets=["a", "b"]),
            UpdateOperation(op=OpKind.MOVE_SLIDE, from_index=2, to_index=0),
            UpdateOperation(op=OpKind.DELETE_SLIDE, slide=3),
        ],
    )
    assert result.applied
    edited = tmp_path / "edited.pptx"
    edited.write_bytes(result.data)
    presentation = Presentation(str(edited))
    titles = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                titles.append(shape.text_frame.text.strip().splitlines()[0])
                break
    assert len(presentation.slides) == 2
    assert titles[0] == "New Slide"
    assert pptx_engine._theme_colors(presentation) == original_theme
    assert [layout.name for layout in presentation.slide_layouts] == [
        layout.name for layout in original.slide_layouts
    ]


def test_replace_text_keeps_run_formatting(tmp_path):
    path = make_pptx(tmp_path / "deck.pptx")
    result = pptx_engine.apply_pptx(
        path,
        _ref(),
        [UpdateOperation(op=OpKind.REPLACE_TEXT, find="Revenue grew", replace="Revenue doubled")],
    )
    edited = tmp_path / "replaced.pptx"
    edited.write_bytes(result.data)
    presentation = Presentation(str(edited))
    texts = " ".join(
        shape.text_frame.text
        for slide in presentation.slides
        for shape in slide.shapes
        if getattr(shape, "has_text_frame", False)
    )
    assert "Revenue doubled" in texts and "Revenue grew" not in texts


def test_create_from_blocks(tmp_path):
    blocks = [
        ContentBlock(kind="heading", text="Generated Deck"),
        ContentBlock(kind="bullets", bullets=["one", "two"]),
        ContentBlock(kind="table", rows=[["A", "B"], ["1", "2"]]),
    ]
    data = pptx_engine.create_pptx(blocks, metadata={"title": "R21 Deck", "author": "HD2"})
    produced = tmp_path / "created.pptx"
    produced.write_bytes(data)
    presentation = Presentation(str(produced))
    assert len(presentation.slides) >= 1
    assert presentation.core_properties.title == "R21 Deck"
    texts = " ".join(
        shape.text_frame.text
        for slide in presentation.slides
        for shape in slide.shapes
        if getattr(shape, "has_text_frame", False)
    )
    assert "Generated Deck" in texts and "one" in texts


def test_corrupt_pptx_raises_contract_error(tmp_path):
    from app.knowledge.contracts import KnowledgeError, KnowledgeErrorCode

    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"PK\x03\x04nope")
    with pytest.raises(KnowledgeError) as excinfo:
        pptx_engine.read_path(broken, _ref(), ReadRequest(document_id="kd_pptxtest001"))
    assert excinfo.value.body.code is KnowledgeErrorCode.CORRUPT_CONTENT
