import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from pydantic import BaseModel
import pymupdf as fitz
from app.config import settings

class SlideRenderResult(BaseModel):
    slide_num: int
    status: str  # "completed" | "render_unavailable" | "render_failed"
    image_bytes: Optional[bytes] = None
    width: Optional[int] = None
    height: Optional[int] = None
    mime_type: str = "image/png"
    warning: Optional[str] = None
    error: Optional[str] = None

class LibreOfficeRenderer:
    @classmethod
    def find_libreoffice_binary(cls) -> Optional[str]:
        """Locates the LibreOffice headless executable across platforms."""
        if settings.LIBREOFFICE_PATH and os.path.exists(settings.LIBREOFFICE_PATH):
            return settings.LIBREOFFICE_PATH

        for name in ("soffice", "libreoffice"):
            found = shutil.which(name)
            if found:
                return found

        # Standard macOS locations
        mac_paths = [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            "/opt/homebrew/bin/soffice",
            "/usr/local/bin/soffice"
        ]
        for mp in mac_paths:
            if os.path.exists(mp):
                return mp

        return None

    @classmethod
    def is_available(cls) -> bool:
        return cls.find_libreoffice_binary() is not None

    @classmethod
    def render_pptx_to_pdf(cls, pptx_path: Path, output_dir: Path) -> Tuple[Optional[Path], Optional[str]]:
        """Converts PPTX to PDF using LibreOffice Headless."""
        soffice_bin = cls.find_libreoffice_binary()
        if not soffice_bin:
            return None, "LibreOffice executable not found on host system"

        try:
            cmd = [
                soffice_bin,
                "--headless",
                "--invisible",
                "--nodefault",
                "--nofirststartwizard",
                "--nolockcheck",
                "--nologo",
                "--norestore",
                "--convert-to", "pdf",
                str(pptx_path),
                "--outdir", str(output_dir)
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            if res.returncode != 0:
                err = res.stderr.decode("utf-8", errors="replace")
                return None, f"LibreOffice exit code {res.returncode}: {err}"

            pdf_filename = pptx_path.stem + ".pdf"
            converted_pdf = output_dir / pdf_filename
            if converted_pdf.exists():
                return converted_pdf, None
            return None, "Converted PDF file was not created in output directory"
        except subprocess.TimeoutExpired:
            return None, "LibreOffice PPTX to PDF conversion timed out after 60 seconds"
        except Exception as e:
            return None, f"LibreOffice conversion error: {str(e)}"

    @classmethod
    def render_slides(cls, pptx_path: Path, slide_count: int) -> List[SlideRenderResult]:
        """
        Renders all slides of a PPTX into high-fidelity PNG images via LibreOffice.
        Maintains exact aspect ratio, fonts, shapes, lines, and charts.
        Returns SlideRenderResult for each slide with appropriate status.
        """
        results: List[SlideRenderResult] = []
        soffice_bin = cls.find_libreoffice_binary()

        if not soffice_bin:
            # LibreOffice is not installed
            for s in range(1, slide_count + 1):
                results.append(SlideRenderResult(
                    slide_num=s,
                    status="render_unavailable",
                    warning="LibreOffice Headless not installed on host. Slide visual rendering unavailable."
                ))
            return results

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            pdf_path, err = cls.render_pptx_to_pdf(pptx_path, tmp_path)
            if not pdf_path or err:
                for s in range(1, slide_count + 1):
                    results.append(SlideRenderResult(
                        slide_num=s,
                        status="render_failed",
                        error=err or "Slide conversion failed"
                    ))
                return results

            # Render pages from converted PDF using PyMuPDF
            try:
                doc = fitz.open(str(pdf_path))
                for s_idx in range(1, slide_count + 1):
                    if s_idx <= len(doc):
                        page = doc[s_idx - 1]
                        # 150 DPI provides sharp 1920x1080 equivalent resolution
                        pix = page.get_pixmap(dpi=150)
                        results.append(SlideRenderResult(
                            slide_num=s_idx,
                            status="completed",
                            image_bytes=pix.tobytes("png"),
                            width=pix.width,
                            height=pix.height,
                            mime_type="image/png"
                        ))
                    else:
                        results.append(SlideRenderResult(
                            slide_num=s_idx,
                            status="render_failed",
                            error=f"Slide index {s_idx} out of range in rendered PDF ({len(doc)} pages)"
                        ))
                doc.close()
            except Exception as e:
                for s in range(1, slide_count + 1):
                    results.append(SlideRenderResult(
                        slide_num=s,
                        status="render_failed",
                        error=f"Failed to rasterize converted PDF: {str(e)}"
                    ))

        return results
