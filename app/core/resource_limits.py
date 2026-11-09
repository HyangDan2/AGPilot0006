import io
import os
import zipfile
from typing import AsyncGenerator, Tuple
from fastapi import UploadFile, HTTPException, status
from app.config import settings

class DocumentLimitExceeded(HTTPException):
    def __init__(self, detail: str, status_code: int = status.HTTP_413_CONTENT_TOO_LARGE):
        super().__init__(status_code=status_code, detail=detail)

async def read_upload_stream_safely(
    file: UploadFile,
    max_bytes: int = settings.MAX_FILE_SIZE_BYTES,
    chunk_size: int = 1024 * 1024  # 1MB chunk
) -> bytes:
    """
    Safely read upload file in chunks without loading unbounded data into memory.
    Raises HTTPException 413 immediately if uploaded bytes exceed max_bytes.
    """
    buffer = io.BytesIO()
    total_read = 0

    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > max_bytes:
            raise DocumentLimitExceeded(
                f"File size exceeds maximum allowed limit of {max_bytes // (1024 * 1024)} MB."
            )
        buffer.write(chunk)

    return buffer.getvalue()

def inspect_zip_package_limits(content: bytes) -> None:
    """
    Validates ZIP container limits:
    - Maximum entries (prevent ZIP bomb with 1,000,000 small files)
    - Maximum uncompressed size (prevent explosion)
    - Maximum compression ratio
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infolist = zf.infolist()
            if len(infolist) > settings.MAX_ZIP_ENTRIES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Archive contains too many files ({len(infolist)} > {settings.MAX_ZIP_ENTRIES})."
                )

            total_uncompressed = 0
            for info in infolist:
                total_uncompressed += info.file_size
                if total_uncompressed > settings.MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise DocumentLimitExceeded(
                        "Decompressed size exceeds maximum limit of 500 MB (Zip Bomb protection)."
                    )

            if len(content) > 0:
                ratio = total_uncompressed / len(content)
                if ratio > settings.MAX_ZIP_RATIO:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Suspicious compression ratio detected (Zip Bomb protection)."
                    )
    except zipfile.BadZipFile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Corrupted or malformed ZIP archive."
        )

def verify_page_count_limit(page_count: int, max_limit: int = settings.MAX_PAGES_LIMIT) -> None:
    """Ensure document page or slide count does not exceed limit (Work Order A-4)."""
    if page_count > max_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Document exceeds maximum allowed length ({page_count} > {max_limit} pages/slides)."
        )
