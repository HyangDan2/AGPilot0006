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
