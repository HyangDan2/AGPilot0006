import io
import pytest
from pathlib import Path
import pymupdf as fitz
import docx
import pptx
from pptx.util import Inches
from PIL import Image

from app.parsers.pdf_parser import PDFParser
from app.parsers.docx_parser import DOCXParser
from app.parsers.pptx_parser import PPTXParser
from app.storage.store import DocumentStore
from app.config import settings

@pytest.fixture
def tmp_store(tmp_path):
    settings.DOCUMENTS_DIR = tmp_path / "documents"
    settings.DATA_DIR = tmp_path
    settings.DATABASE_PATH = tmp_path / "test.db"
    settings.init_directories()
    return DocumentStore("doc_test_123")

def test_pdf_parser(tmp_path, tmp_store):
    # 1. Create a dummy PDF with 2 pages and an image
    pdf_path = tmp_path / "sample.pdf"
    doc = fitz.open()
    
    # Page 1
    page1 = doc.new_page()
    page1.insert_text((50, 72), "Chapter 1: Optical Design Overview\nThis is sample text about Mini-LED.")
    
    # Insert an image into Page 1
    img = Image.new("RGB", (100, 100), color="blue")
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format="PNG")
    page1.insert_image(fitz.Rect(50, 150, 150, 250), stream=img_byte_arr.getvalue())
    
    # Page 2
    page2 = doc.new_page()
    page2.insert_text((50, 72), "Chapter 2: Backlight Architecture\nDetails of optical lenses.")
    
    doc.save(str(pdf_path))
    doc.close()

    # 2. Parse
    parser = PDFParser()
    result = parser.parse("doc_test_123", pdf_path, tmp_store)

    assert result.doc_type == "pdf"
    assert result.page_count == 2
    assert len(result.sections) == 2
    assert len(result.figures) >= 1
    assert len(result.chunks) == 2
    assert "Optical Design" in result.sections[0].title
    assert (tmp_store.pages_dir / "page_0001.png").exists()
    assert (tmp_store.pages_dir / "page_0002.png").exists()

def test_docx_parser(tmp_path, tmp_store):
    # 1. Create a dummy DOCX with headings and a table
    docx_path = tmp_path / "sample.docx"
    doc = docx.Document()
    doc.add_heading("System Architecture", level=1)
    doc.add_paragraph("This document describes the high-level architecture.")
    doc.add_heading("Hardware Specifications", level=2)
    
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Component"
    table.rows[0].cells[1].text = "Spec"
    table.rows[1].cells[0].text = "MCU"
    table.rows[1].cells[1].text = "ARM Cortex-M4"
    
    doc.save(str(docx_path))

    # 2. Parse
    parser = DOCXParser()
    result = parser.parse("doc_test_123", docx_path, tmp_store)

    assert result.doc_type == "docx"
    assert len(result.sections) >= 2
    titles = [s.title for s in result.sections]
    assert "System Architecture" in titles
    assert "Hardware Specifications" in titles
    assert any(s.table_count > 0 for s in result.sections)
    assert len(result.chunks) >= 2

def test_pptx_parser(tmp_path, tmp_store):
    # 1. Create a dummy PPTX with 2 slides and an image
    pptx_path = tmp_path / "sample.pptx"
    prs = pptx.Presentation()
    blank_layout = prs.slide_layouts[6]  # blank layout
    
    # Slide 1
    s1 = prs.slides.add_slide(blank_layout)
    txBox = s1.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    txBox.text_frame.text = "Micro-LED Display Technologies"
    
    # Add an image to Slide 1
    img = Image.new("RGB", (100, 100), color="orange")
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format="PNG")
    img_path = tmp_path / "temp_img.png"
    img_path.write_bytes(img_byte_arr.getvalue())
    s1.shapes.add_picture(str(img_path), Inches(1), Inches(2), Inches(2), Inches(2))

    # Slide 2
    s2 = prs.slides.add_slide(blank_layout)
    txBox2 = s2.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    txBox2.text_frame.text = "Efficiency Analysis"

    prs.save(str(pptx_path))

    # 2. Parse
    parser = PPTXParser()
    result = parser.parse("doc_test_123", pptx_path, tmp_store)

    assert result.doc_type == "pptx"
    assert result.page_count == 2
    assert len(result.sections) == 2
    assert len(result.figures) >= 1
    # Check that full slide render images exist
    assert (tmp_store.slides_dir / "slide_0001.png").exists()
    assert (tmp_store.slides_dir / "slide_0002.png").exists()
