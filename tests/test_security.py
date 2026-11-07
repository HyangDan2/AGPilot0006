import pytest
import io
import zipfile
from fastapi import HTTPException
from app.core.security import validate_file_magic_bytes, sanitize_filename

def test_sanitize_filename():
    assert sanitize_filename("../../../etc/passwd") == "passwd"
    assert sanitize_filename("test document (1).pdf") == "test document 1.pdf"
    assert sanitize_filename("") == "document"

def test_validate_pdf_magic():
    pdf_bytes = b"%PDF-1.7\n%some binary data"
    assert validate_file_magic_bytes(pdf_bytes) == "pdf"

def test_validate_invalid_file():
    invalid_bytes = b"Just plain text without magic bytes"
    with pytest.raises(HTTPException) as exc:
        validate_file_magic_bytes(invalid_bytes)
    assert exc.value.status_code == 400

def test_validate_docx_package():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:document></w:document>")
        zf.writestr("[Content_Types].xml", "<Types></Types>")
    content = buf.getvalue()
    assert validate_file_magic_bytes(content) == "docx"

def test_validate_pptx_package():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ppt/presentation.xml", "<p:presentation></p:presentation>")
        zf.writestr("[Content_Types].xml", "<Types></Types>")
    content = buf.getvalue()
    assert validate_file_magic_bytes(content) == "pptx"

def test_forbid_macros():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:document></w:document>")
        zf.writestr("word/vbaProject.bin", b"fake macro binary")
    content = buf.getvalue()
    with pytest.raises(HTTPException) as exc:
        validate_file_magic_bytes(content)
    assert "macros" in exc.value.detail.lower()
