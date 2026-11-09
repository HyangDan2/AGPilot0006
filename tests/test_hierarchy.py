import io
from pathlib import Path
import docx
import pymupdf as fitz
import pytest

from app.parsers.docx_parser import DOCXParser
from app.parsers.pdf_parser import PDFParser
from app.storage.store import DocumentStore
from app.config import settings

@pytest.fixture
def tmp_store(tmp_path):
    settings.DOCUMENTS_DIR = tmp_path / "documents"
    settings.DATA_DIR = tmp_path
    settings.init_directories()
    return DocumentStore("doc_hierarchy_test")

def test_docx_heading_hierarchy_and_no_empty_overview(tmp_path, tmp_store):
    """
    QA-03:
    1. Document starting directly with Heading 1 must NOT create an empty Overview section.
    2. Skipped levels (Heading 1 -> Heading 3) must link correctly with parent_id.
    3. Markdown table pipe characters must be escaped.
    """
    doc_path = tmp_path / "hierarchy_doc.docx"
    doc = docx.Document()

    # Heading 1
    doc.add_heading("1. Executive Summary", level=1)
    doc.add_paragraph("Introductory text under section 1.")

    # Skipped level: Heading 3 directly under Heading 1
    doc.add_heading("1.1.1 Deep Subsection", level=3)
    doc.add_paragraph("Content inside deep subsection.")

    # Table with pipe character inside cell
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Option"
    table.rows[0].cells[1].text = "Condition"
    table.rows[1].cells[0].text = "Mode A"
    table.rows[1].cells[1].text = "Value | Flag"

    doc.save(str(doc_path))

    parser = DOCXParser()
    result = parser.parse("doc_hierarchy_test", doc_path, tmp_store)

    assert result.doc_type == "docx"
    assert len(result.sections) == 2
    
    sec1 = result.sections[0]
    sec2 = result.sections[1]

    # No empty Overview created
    assert sec1.title == "1. Executive Summary"
    assert sec1.level == 1
    assert sec1.parent_id is None

    # Skipped level Heading 3 properly parents to Heading 1
    assert sec2.title == "1.1.1 Deep Subsection"
    assert sec2.level == 3
    assert sec2.parent_id == sec1.section_id

    # Table cell pipe escaping
    assert "\\|" in sec2.content or "Value \\| Flag" in sec2.content

def test_docx_overview_created_when_preceding_text_exists(tmp_path, tmp_store):
    """
    QA-03: If text exists before the first Heading, an Overview section should be created.
    """
    doc_path = tmp_path / "with_preamble.docx"
    doc = docx.Document()
    doc.add_paragraph("This is preamble text before any heading appears.")
    doc.add_heading("Main Chapter", level=1)
    doc.add_paragraph("Body of chapter.")
    doc.save(str(doc_path))

    parser = DOCXParser()
    result = parser.parse("doc_hierarchy_test", doc_path, tmp_store)

    assert len(result.sections) == 2
    assert result.sections[0].title == "Overview"
    assert result.sections[0].content == "This is preamble text before any heading appears."
    assert result.sections[1].title == "Main Chapter"

def test_pdf_multiple_bookmarks_same_page(tmp_path, tmp_store):
    """
    QA-04: PDF with multiple bookmarks on the same page must preserve both sections
    with proper hierarchy levels and parent-child linkage.
    """
    pdf_path = tmp_path / "multi_toc.pdf"
    doc = fitz.open()

    p1 = doc.new_page()
    p1.insert_text((50, 72), "Title: Photonics\nPhotonics fundamentals.\n\nLaser diodes in detail.")

    # TOC: Level 1 and Level 2 both on Page 1
    toc = [
        [1, "Photonics Overview", 1],
        [2, "Laser Diodes", 1]
    ]
    doc.set_toc(toc)
    doc.save(str(pdf_path))
    doc.close()

    parser = PDFParser()
    result = parser.parse("doc_hierarchy_test", pdf_path, tmp_store)

    assert result.doc_type == "pdf"
    assert len(result.sections) == 2
    
    sec1 = result.sections[0]
    sec2 = result.sections[1]

    assert sec1.title == "Photonics Overview"
    assert sec1.level == 1
    assert sec1.parent_id is None

    assert sec2.title == "Laser Diodes"
    assert sec2.level == 2
    assert sec2.parent_id == sec1.section_id
