import base64
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.api.v1.embedding_settings import require_embedding_admin
from app.core.auth import CallerContext, get_current_caller
from app.core.resource_limits import read_upload_stream_safely
from app.services.docx_markdown import build_markdown_zip, convert_docx

router = APIRouter(tags=["docx tools"])

MAX_PREVIEW_IMAGE_BYTES = 2 * 1024 * 1024  # 2MB per image preview cap in JSON


async def _convert_uploaded_docx(
    file: UploadFile,
    table_mode: Literal["auto", "gfm", "html"],
    image_mode: Literal["assets", "base64", "none"],
    response_format: Literal["json", "zip"],
    include_previews: bool = True,
):
    filename = file.filename or "document.docx"
    if not filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are supported")

    content = await read_upload_stream_safely(file)
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if not content.startswith(b"PK\x03\x04"):
        raise HTTPException(status_code=400, detail="Invalid DOCX archive signature")

    with tempfile.NamedTemporaryFile(suffix=".docx", delete=True) as tmp:
        tmp.write(content)
        tmp.flush()
        try:
            effective_img_mode = "assets" if response_format == "zip" else image_mode
            result = convert_docx(
                Path(tmp.name),
                filename=filename,
                table_mode=table_mode,
                image_mode=effective_img_mode,
                asset_prefix="assets/",
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Failed to parse DOCX: {type(exc).__name__}")

    if response_format == "zip":
        zip_bytes = build_markdown_zip(result, asset_prefix="assets/")
        stem = Path(filename).stem or "document"
        safe_ascii = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_" for ch in stem) or "document"
        utf8_name = quote(f"{stem}.zip")
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_ascii}.zip"; filename*=UTF-8\'\'{utf8_name}'
            },
        )

    figures_payload = []
    for fig in result.figures:
        item = {
            "figure_id": fig.figure_id,
            "asset_id": fig.asset_id,
            "filename": fig.filename,
            "mime_type": fig.mime_type,
            "caption": fig.caption,
            "section_id": fig.section_id,
            "context": fig.context,
            "size_bytes": len(fig.data),
            "converted_to_png": fig.converted_to_png,
        }
        if include_previews and len(fig.data) <= MAX_PREVIEW_IMAGE_BYTES:
            b64 = base64.b64encode(fig.data).decode("ascii")
            item["preview_data_url"] = f"data:{fig.mime_type};base64,{b64}"
        figures_payload.append(item)

    return {
        "filename": result.filename,
        "table_mode": table_mode,
        "image_mode": image_mode,
        "stats": result.stats,
        "markdown": result.markdown,
        "sections": [
            {
                "section_id": s.section_id,
                "parent_id": s.parent_id,
                "title": s.title,
                "level": s.level,
                "table_count": s.table_count,
                "figure_ids": s.figure_ids,
            }
            for s in result.sections
        ],
        "figures": figures_payload,
    }


@router.post("/tools/docx-to-md")
async def convert_docx_for_service(
    file: UploadFile = File(...),
    table_mode: Literal["auto", "gfm", "html"] = Query("auto"),
    image_mode: Literal["assets", "base64", "none"] = Query("assets"),
    response_format: Literal["json", "zip"] = Query("json"),
    include_previews: bool = Query(False),
    _caller: CallerContext = Depends(get_current_caller),
):
    """Deterministic complex DOCX -> Markdown extraction tool for orchestration / AGCore."""
    return await _convert_uploaded_docx(file, table_mode, image_mode, response_format, include_previews)


@router.post("/settings/tools/docx-to-md", dependencies=[Depends(require_embedding_admin)])
async def convert_docx_for_admin(
    file: UploadFile = File(...),
    table_mode: Literal["auto", "gfm", "html"] = Query("auto"),
    image_mode: Literal["assets", "base64", "none"] = Query("assets"),
    response_format: Literal["json", "zip"] = Query("json"),
):
    """Administrator console endpoint for DOCX -> Markdown extraction and ZIP download."""
    return await _convert_uploaded_docx(file, table_mode, image_mode, response_format, include_previews=True)


import time as _time
import uuid as _uuid
from pydantic import BaseModel, Field
from app.config import settings as _settings
from app.core.auth import verify_document_access
from app.core.database import DocumentRepository
from app.core.security import validate_safe_id
from app.services.markdown_docx import convert_markdown_to_docx
from app.storage.store import DocumentStore

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _generated_docx_dir() -> Path:
    base = Path(getattr(_settings, "DATA_DIR", "/var/www/services/AGTools/data")) / "generated_docx"
    base.mkdir(parents=True, exist_ok=True)
    return base


import json as _json

def _prune_old_docx(folder: Path, ttl_seconds: int = 86400) -> None:
    now = _time.time()
    for item in list(folder.glob("*.docx")) + list(folder.glob("*.meta.json")):
        try:
            if now - item.stat().st_mtime > ttl_seconds:
                item.unlink(missing_ok=True)
        except Exception:
            pass


def _build_docx_download_response(file_id: str) -> Response:
    validate_safe_id(file_id, "file_id")
    out_dir = _generated_docx_dir()
    path = out_dir / f"{file_id}.docx"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Generated DOCX not found or expired")
    raw_name = f"{file_id}.docx"
    meta_path = out_dir / f"{file_id}.meta.json"
    if meta_path.exists():
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and isinstance(meta.get("filename"), str) and meta["filename"].strip():
                raw_name = meta["filename"].strip()
        except Exception:
            pass
    ascii_name, utf8_name = _safe_docx_name(raw_name)
    return Response(
        content=path.read_bytes(),
        media_type=DOCX_MIME,
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}",
            "Cache-Control": "no-store",
        },
    )


class DocxByDocRequest(BaseModel):
    doc_id: str = Field(..., min_length=1, max_length=128)
    table_mode: Literal["auto", "gfm", "html"] = "auto"
    image_mode: Literal["assets", "base64", "none"] = "none"
    max_chars: int = Field(20000, ge=500, le=100000)


class MdToDocxRequest(BaseModel):
    markdown: str = Field(..., min_length=1, max_length=400000)
    filename: str = Field("document.docx", min_length=1, max_length=160)
    title: str | None = Field(None, max_length=200)
    return_format: Literal["json", "docx"] = "json"


def _safe_docx_name(raw_name: str) -> tuple[str, str]:
    stem = Path(raw_name or "document.docx").stem or "document"
    safe_ascii = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_" for ch in stem) or "document"
    return f"{safe_ascii}.docx", quote(f"{stem}.docx")


@router.post("/tools/docx-to-md/by-doc")
async def convert_stored_docx_to_md(
    body: DocxByDocRequest,
    caller: CallerContext = Depends(get_current_caller),
):
    """Convert an already-uploaded DOCX document (by doc_id) into clean Markdown."""
    validate_safe_id(body.doc_id, "doc_id")
    doc = DocumentRepository.get_document(body.doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    verify_document_access(doc, caller)
    orig = DocumentStore(body.doc_id).get_original_path()
    if not orig or not orig.exists():
        raise HTTPException(status_code=404, detail="Original document file not found")
    if orig.suffix.lower() != ".docx":
        raise HTTPException(status_code=400, detail="Only .docx documents are supported by docx-to-md")
    try:
        result = convert_docx(
            orig,
            filename=orig.name,
            table_mode=body.table_mode,
            image_mode=body.image_mode,
            asset_prefix="assets/",
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to parse DOCX: {type(exc).__name__}") from exc

    md_text = result.markdown or ""
    truncated = len(md_text) > body.max_chars
    clipped = md_text[: body.max_chars]
    return {
        "doc_id": body.doc_id,
        "filename": result.filename,
        "markdown": clipped,
        "chars": len(clipped),
        "total_chars": len(md_text),
        "truncated": truncated,
        "stats": result.stats,
        "sections": [
            {"section_id": sec.section_id, "title": sec.title, "level": sec.level, "table_count": sec.table_count}
            for sec in result.sections
        ],
        "meta": {"quality": "sufficient" if clipped.strip() else "empty", "reason": "ok" if clipped.strip() else "empty_docx", "next_hints": []},
    }


@router.post("/tools/md-to-docx")
async def convert_md_to_docx_for_service(
    body: MdToDocxRequest,
    _caller: CallerContext = Depends(get_current_caller),
):
    """Bidirectional Markdown -> DOCX conversion tool for AGCore and services."""
    docx_bytes, stats = convert_markdown_to_docx(body.markdown, title=body.title)
    ascii_name, utf8_name = _safe_docx_name(body.filename)
    if body.return_format == "docx":
        return Response(
            content=docx_bytes,
            media_type=DOCX_MIME,
            headers={"Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8''{utf8_name}'},
        )
    out_dir = _generated_docx_dir()
    _prune_old_docx(out_dir)
    file_id = f"docx_{_uuid.uuid4().hex[:16]}"
    clean_filename = f"{Path(body.filename).stem or 'document'}.docx"
    (out_dir / f"{file_id}.docx").write_bytes(docx_bytes)
    (out_dir / f"{file_id}.meta.json").write_text(
        _json.dumps({"filename": clean_filename}, ensure_ascii=False), encoding="utf-8"
    )
    download_url = f"/admin/agtools/api/v1/settings/tools/md-to-docx/{file_id}"
    return {
        "file_id": file_id,
        "filename": clean_filename,
        "size_bytes": len(docx_bytes),
        "download_path": f"/api/v1/tools/md-to-docx/{file_id}",
        "download_url": download_url,
        "markdown_link": f"[📥 {clean_filename} 다운로드]({download_url})",
        "stats": stats,
        "meta": {"quality": "sufficient", "reason": "ok", "next_hints": []},
    }


@router.get("/tools/md-to-docx/{file_id}")
async def download_generated_docx(
    file_id: str,
    _caller: CallerContext = Depends(get_current_caller),
):
    return _build_docx_download_response(file_id)


@router.get("/settings/tools/md-to-docx/{file_id}", dependencies=[Depends(require_embedding_admin)])
async def download_generated_docx_for_admin(file_id: str):
    """Admin SSO browser download endpoint for generated DOCX files."""
    return _build_docx_download_response(file_id)


@router.post("/settings/tools/md-to-docx", dependencies=[Depends(require_embedding_admin)])
async def convert_md_to_docx_for_admin(body: MdToDocxRequest):
    """Administrator console endpoint for Markdown -> DOCX conversion."""
    docx_bytes, _ = convert_markdown_to_docx(body.markdown, title=body.title)
    ascii_name, utf8_name = _safe_docx_name(body.filename)
    return Response(
        content=docx_bytes,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8''{utf8_name}'},
    )
