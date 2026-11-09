from pathlib import Path
from typing import List, Optional, Tuple
from pydantic import BaseModel
import pymupdf as fitz

class PageRenderResult(BaseModel):
    page_num: int
    status: str
    image_bytes: Optional[bytes] = None
    width: Optional[int] = None
    height: Optional[int] = None
    mime_type: str = "image/png"
    error: Optional[str] = None

class PDFRenderer:
    @classmethod
    def render_page(cls, doc: fitz.Document, page_num: int, dpi: int = 150) -> PageRenderResult:
        try:
            if page_num < 1 or page_num > len(doc):
                return PageRenderResult(page_num=page_num, status="render_failed", error="Page out of bounds")
            page = doc[page_num - 1]
            pix = page.get_pixmap(dpi=dpi)
            return PageRenderResult(
                page_num=page_num,
                status="completed",
                image_bytes=pix.tobytes("png"),
                width=pix.width,
                height=pix.height,
                mime_type="image/png"
            )
        except Exception as e:
            return PageRenderResult(page_num=page_num, status="render_failed", error=str(e))
