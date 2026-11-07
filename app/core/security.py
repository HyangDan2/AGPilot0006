import os
import zipfile
from pathlib import Path
from typing import Tuple, Optional
from fastapi import HTTPException, status

# Magic byte signatures
MAGIC_PDF = b"%PDF-"
MAGIC_ZIP = b"PK\x03\x04"
MAGIC_ZIP_EMPTY = b"PK\x05\x06"
MAGIC_ZIP_SPANNED = b"PK\x07\x08"

# Maximum decompression ratio to prevent Zip Bomb
MAX_ZIP_DECOMPRESSION_RATIO = 100
MAX_UNCOMPRESSED_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB

SUPPORTED_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent directory traversal and special character attacks."""
    basename = os.path.basename(filename)
    clean = "".join(c for c in basename if c.isalnum() or c in (".", "-", "_", " ")).strip()
    return clean or "document"


def validate_file_magic_bytes(content: bytes) -> str:
    """
    Validate actual file type using magic bytes.
    Returns detected format: 'pdf', 'docx', or 'pptx'.
    Raises HTTPException 400 if invalid or unsupported.
    """
    if len(content) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is too small or empty to be a valid document."
        )

    # 1. PDF Check
    if content.startswith(MAGIC_PDF):
        return "pdf"

    # 2. ZIP Check (DOCX & PPTX are OPC Zip packages)
    if content.startswith(MAGIC_ZIP) or content.startswith(MAGIC_ZIP_EMPTY) or content.startswith(MAGIC_ZIP_SPANNED):
        return _inspect_openxml_package(content)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Unsupported or invalid file signature. Only PDF, DOCX, and PPTX are supported."
    )


def _inspect_openxml_package(content: bytes) -> str:
    """Inspect internal ZIP contents of DOCX/PPTX to ensure valid OPC structure without macros."""
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            total_uncompressed = 0
            has_word_dir = False
            has_ppt_dir = False
            has_macros = False

            for info in zf.infolist():
                # Check for Zip Bomb / exorbitant compression ratio
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_SIZE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="File exceeds maximum uncompressed size limit (Zip Bomb protection)."
                    )

                fname = info.filename.lower()
                # Check for macros
                if "vbaproject.bin" in fname or fname.endswith(".vba") or fname.endswith(".bin"):
                    has_macros = True

                if fname.startswith("word/"):
                    has_word_dir = True
                elif fname.startswith("ppt/"):
                    has_ppt_dir = True

            if has_macros:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Office documents containing executable macros (VBA) are strictly forbidden."
                )

            if len(content) > 0:
                ratio = total_uncompressed / len(content)
                if ratio > MAX_ZIP_DECOMPRESSION_RATIO:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Suspicious compression ratio detected (Zip Bomb protection)."
                    )

            if has_word_dir:
                return "docx"
            elif has_ppt_dir:
                return "pptx"
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Valid ZIP archive detected, but neither DOCX nor PPTX OpenXML structure found."
                )

    except zipfile.BadZipFile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Corrupted or malformed ZIP archive."
        )
