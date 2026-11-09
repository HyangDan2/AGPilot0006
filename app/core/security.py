import io
import os
import re
import zipfile
from pathlib import Path
from typing import Tuple, Optional
from fastapi import HTTPException, status
from app.config import settings
from app.core.resource_limits import inspect_zip_package_limits

# Magic byte signatures
MAGIC_PDF = b"%PDF-"
MAGIC_ZIP = b"PK\x03\x04"
MAGIC_ZIP_EMPTY = b"PK\x05\x06"
MAGIC_ZIP_SPANNED = b"PK\x07\x08"

SUPPORTED_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent directory traversal and special character attacks."""
    basename = os.path.basename(filename)
    clean = "".join(c for c in basename if c.isalnum() or c in (".", "-", "_", " ")).strip()
    return clean or "document"

def validate_safe_id(identifier: str, field_name: str = "id") -> str:
    """Validate that an ID (document_id, section_id, figure_id, asset_id) has no path traversal chars."""
    if not identifier or not SAFE_ID_REGEX.match(identifier):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}: contains forbidden characters or path traversal sequences."
        )
    return identifier

def validate_file_format_and_extension(filename: str, content: bytes) -> str:
    """
    Validates magic bytes AND checks for extension/format mismatch.
    Returns detected format ('pdf', 'docx', 'pptx').
    """
    doc_type = validate_file_magic_bytes(content)
    
    # Check extension mismatch if filename has an extension
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext:
        expected_types = {
            "pdf": ["pdf"],
            "docx": ["docx"],
            "pptx": ["pptx"],
        }
        valid_exts = expected_types.get(doc_type, [])
        if ext not in valid_exts and ext != "bin":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File extension '.{ext}' does not match actual detected format '{doc_type}'."
            )
            
    return doc_type

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
        # Check for encrypted or damaged PDF header
        return "pdf"

    # 2. ZIP Check (DOCX & PPTX are OPC Zip packages)
    if content.startswith(MAGIC_ZIP) or content.startswith(MAGIC_ZIP_EMPTY) or content.startswith(MAGIC_ZIP_SPANNED):
        # Run ZIP limits check (entries, size, ratio)
        inspect_zip_package_limits(content)
        return _inspect_openxml_package(content)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Unsupported or invalid file signature. Only PDF, DOCX, and PPTX are supported."
    )

def _inspect_openxml_package(content: bytes) -> str:
    """Inspect internal ZIP contents of DOCX/PPTX to ensure valid OPC structure without macros."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            has_word_dir = False
            has_ppt_dir = False
            has_macros = False

            for info in zf.infolist():
                fname = info.filename.lower()
                # Check for executable VBA macros (Work Order 11)
                if "vbaproject.bin" in fname or "vbasignature.bin" in fname or fname.endswith(".vba"):
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
