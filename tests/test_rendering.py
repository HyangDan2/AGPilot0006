import io
from pathlib import Path
from unittest.mock import patch
import pymupdf as fitz
from PIL import Image
import pytest

from app.parsers.renderers.libreoffice_renderer import LibreOfficeRenderer, SlideRenderResult
from app.parsers.pdf_parser import PDFParser
from app.storage.store import DocumentStore
from app.config import settings

@pytest.fixture
def tmp_store(tmp_path):
    settings.DOCUMENTS_DIR = tmp_path / "documents"
    settings.DATA_DIR = tmp_path
    settings.init_directories()
    return DocumentStore("doc_render_test")

def test_libreoffice_unavailable_handling():
    """QA-01: When LibreOffice is absent, slide render returns render_unavailable without crashing."""
    with patch.object(LibreOfficeRenderer, "find_libreoffice_binary", return_value=None):
        results = LibreOfficeRenderer.render_slides(Path("dummy.pptx"), slide_count=3)
        assert len(results) == 3
        for res in results:
            assert res.status == "render_unavailable"
            assert "LibreOffice Headless not installed" in res.warning

def test_libreoffice_mocked_true_render(tmp_path):
    """QA-01: When LibreOffice converts successfully to PDF, render_slides rasterizes exact PNGs."""
    # Create sample PDF that acts as the LibreOffice conversion output
    converted_pdf_path = tmp_path / "converted.pdf"
    doc = fitz.open()
    p1 = doc.new_page(width=960, height=540)  # 16:9
    p1.draw_rect(fitz.Rect(50, 50, 200, 200), color=(1, 0, 0), fill=(1, 0.5, 0))
    doc.save(str(converted_pdf_path))
    doc.close()

    with patch.object(LibreOfficeRenderer, "find_libreoffice_binary", return_value="/mock/soffice"), \
         patch.object(LibreOfficeRenderer, "render_pptx_to_pdf", return_value=(converted_pdf_path, None)):
        
        results = LibreOfficeRenderer.render_slides(Path("sample.pptx"), slide_count=1)
        assert len(results) == 1
        res = results[0]
        assert res.status == "completed"
        assert res.image_bytes is not None
        assert res.width is not None and res.height is not None
        # Verify 16:9 aspect ratio ratio preserved
        aspect_ratio = res.width / res.height
        assert 1.7 <= aspect_ratio <= 1.8

def test_pdf_image_extension_fidelity(tmp_path, tmp_store):
    """
    QA-07: PDF embedded JPEG image must be saved with .jpg extension
    and markdown link must reference exact file.
    """
    pdf_path = tmp_path / "jpeg_test.pdf"
    doc = fitz.open()
    p = doc.new_page()

    # Create JPEG image bytes
    img = Image.new("RGB", (64, 64), color="green")
    jpeg_bytes_io = io.BytesIO()
    img.save(jpeg_bytes_io, format="JPEG")
    jpeg_bytes = jpeg_bytes_io.getvalue()

    p.insert_image(fitz.Rect(50, 50, 150, 150), stream=jpeg_bytes)
    doc.save(str(pdf_path))
    doc.close()

    parser = PDFParser()
    result = parser.parse("doc_render_test", pdf_path, tmp_store)

    assert len(result.figures) >= 1
    fig = result.figures[0]
    
    # Check that disk asset has .jpg extension
    saved_files = list(tmp_store.figures_dir.glob(f"{fig.figure_id}.*"))
    assert len(saved_files) == 1
    assert saved_files[0].suffix in (".jpg", ".jpeg")
