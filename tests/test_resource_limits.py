import asyncio
import io
import zipfile
import pytest
from fastapi.testclient import TestClient
from fastapi import UploadFile, HTTPException

from app.main import app
from app.config import settings
from app.core.resource_limits import read_upload_stream_safely, verify_page_count_limit, inspect_zip_package_limits

@pytest.fixture
def auth_client():
    headers = {
        "Authorization": f"Bearer {settings.SERVICE_TOKEN}",
        "X-Tenant-Id": "qa_tenant",
        "X-Owner-Id": "qa_user"
    }
    return TestClient(app, headers=headers)

def test_upload_exceeding_100mb_rejected():
    """QA-09: Files exceeding 100MB limit must be rejected with 413 during streaming read."""
    class MockUploadFile:
        def __init__(self, size_bytes):
            self.remaining = size_bytes
            self.chunk_size = 1024 * 1024

        async def read(self, size=-1):
            if self.remaining <= 0:
                return b""
            amt = min(self.remaining, self.chunk_size)
            self.remaining -= amt
            return b"A" * amt

    async def _run():
        huge_file = MockUploadFile(101 * 1024 * 1024)
        with pytest.raises(HTTPException) as exc:
            await read_upload_stream_safely(huge_file, max_bytes=100 * 1024 * 1024)
        assert exc.value.status_code == 413
        assert "exceeds maximum allowed limit" in exc.value.detail

    asyncio.run(_run())

def test_abnormal_zip_bomb_rejected():
    """QA-08: Abnormal ZIP packages with excessive decompression size must be rejected."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Create an entry with 200,000 zeros which compresses to < 300 bytes (ratio > 600:1)
        zf.writestr("word/huge_zero.xml", b"\x00" * 200000)
    
    content = buf.getvalue()
    # Test that abnormal compression ratio triggers zip bomb rejection
    with pytest.raises(HTTPException) as exc:
        inspect_zip_package_limits(content)
    assert exc.value.status_code == 400
    assert "zip bomb" in exc.value.detail.lower()

    # Test corrupted zip
    with pytest.raises(HTTPException) as exc:
        inspect_zip_package_limits(b"PK\x03\x04corrupted_bytes_here")
    assert exc.value.status_code == 400

def test_max_page_limit_enforced():
    """QA-14: Document with > 500 pages must be rejected."""
    with pytest.raises(HTTPException) as exc:
        verify_page_count_limit(501, max_limit=500)
    assert exc.value.status_code == 400
    assert "exceeds maximum allowed length" in exc.value.detail
