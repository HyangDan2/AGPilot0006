import io
import struct
import zipfile
import zlib
import docx
from docx.shared import Inches
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.docx_markdown import build_markdown_zip, convert_docx


def _make_png(rgb: tuple[int, int, int]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes(rgb)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG_1X1_A = _make_png((20, 120, 220))
PNG_1X1_B = _make_png((220, 40, 40))

SERVICE_HEADERS = {
    "X-Service-Token": settings.SERVICE_TOKEN,
    "X-Owner-Id": "agcore",
    "X-Tenant-Id": "default_tenant",
}
ADMIN_HEADERS = {
    **SERVICE_HEADERS,
    "X-HD2Lab-User": "hd2service",
    "X-HD2Lab-Role": "admiral",
}


def _build_complex_docx(path) -> None:
    doc = docx.Document()
    doc.add_heading("1. 설비 개요", level=1)
    p = doc.add_paragraph("이 문서는 ")
    r_bold = p.add_run("복합 설비 점검")
    r_bold.bold = True
    p.add_run(" 절차를 설명합니다.")

    # Paragraph with embedded image
    p_img = doc.add_paragraph()
    p_img.add_run().add_picture(io.BytesIO(PNG_1X1_A), width=Inches(1.0))

    # Simple table -> should render as GFM
    doc.add_heading("2. 단순 사양표", level=2)
    t_simple = doc.add_table(rows=2, cols=2)
    t_simple.cell(0, 0).text = "항목"
    t_simple.cell(0, 1).text = "값"
    t_simple.cell(1, 0).text = "교체 주기"
    t_simple.cell(1, 1).text = "6개월"

    # Merged table (colspan + rowspan) with embedded image inside a cell -> should render as HTML table in auto mode
    doc.add_heading("3. 병합 점검표", level=2)
    t_merged = doc.add_table(rows=3, cols=3)
    t_merged.cell(0, 0).merge(t_merged.cell(0, 2))
    t_merged.cell(0, 0).text = "통합 헤더"
    t_merged.cell(1, 0).merge(t_merged.cell(2, 0))
    t_merged.cell(1, 0).text = "A 구역"
    t_merged.cell(1, 1).text = "필터"
    cell_with_img = t_merged.cell(1, 2)
    cell_with_img.paragraphs[0].add_run("정상 ").bold = True
    cell_with_img.paragraphs[0].add_run().add_picture(io.BytesIO(PNG_1X1_B), width=Inches(0.5))
    t_merged.cell(2, 1).text = "팬"
    t_merged.cell(2, 2).text = "점검 필요"

    doc.save(str(path))


def test_complex_docx_conversion_tables_images_and_section_mapping(tmp_path):
    docx_path = tmp_path / "complex_manual.docx"
    _build_complex_docx(docx_path)

    res = convert_docx(docx_path, table_mode="auto", image_mode="assets")
    assert res.stats["sections"] == 3
    assert res.stats["tables"] == 2
    assert res.stats["merged_tables"] == 1
    assert res.stats["images"] == 2

    # Inline formatting preserved
    assert "**복합 설비 점검**" in res.markdown
    # Simple table rendered as GFM
    assert "| 항목 | 값 |" in res.markdown
    # Merged table rendered as HTML with colspan & rowspan and inline image
    assert 'colspan="3"' in res.markdown
    assert 'rowspan="2"' in res.markdown
    assert '<img src="assets/fig_d_002.png"' in res.markdown
    # Paragraph image rendered in markdown
    assert "](assets/fig_d_001.png)" in res.markdown

    # Section <-> figure mapping preserved for orchestration
    sec_by_title = {s.title: s for s in res.sections}
    assert "fig_d_001" in sec_by_title["1. 설비 개요"].figure_ids
    assert "fig_d_002" in sec_by_title["3. 병합 점검표"].figure_ids

    # ZIP package contains document.md + both image assets
    zip_bytes = build_markdown_zip(res)
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = set(zf.namelist())
        assert "document.md" in names
        assert "assets/fig_d_001.png" in names
        assert "assets/fig_d_002.png" in names


def test_docx_to_md_api_json_zip_and_auth(tmp_path):
    docx_path = tmp_path / "sample.docx"
    _build_complex_docx(docx_path)
    raw = docx_path.read_bytes()

    with TestClient(app) as client:
        # Non-admin blocked on admin endpoint
        r_denied = client.post(
            "/api/v1/settings/tools/docx-to-md",
            files={"file": ("sample.docx", raw, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            headers=SERVICE_HEADERS,
        )
        assert r_denied.status_code == 403

        # Service endpoint JSON response
        r_json = client.post(
            "/api/v1/tools/docx-to-md?table_mode=auto&image_mode=assets&response_format=json",
            files={"file": ("sample.docx", raw, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            headers=SERVICE_HEADERS,
        )
        assert r_json.status_code == 200
        body = r_json.json()
        assert body["stats"]["merged_tables"] == 1
        assert body["stats"]["images"] == 2
        assert len(body["sections"]) == 3
        assert len(body["figures"]) == 2

        # Admin endpoint ZIP response
        r_zip = client.post(
            "/api/v1/settings/tools/docx-to-md?table_mode=auto&image_mode=assets&response_format=zip",
            files={"file": ("sample.docx", raw, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            headers=ADMIN_HEADERS,
        )
        assert r_zip.status_code == 200
        assert r_zip.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(r_zip.content), "r") as zf:
            assert "document.md" in zf.namelist()
            assert "assets/fig_d_001.png" in zf.namelist()
