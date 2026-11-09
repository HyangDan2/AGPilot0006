import asyncio
from pathlib import Path
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, status, Query, Depends
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.core.auth import get_current_caller, verify_document_access, CallerContext
from app.core.database import DocumentRepository
from app.core.security import validate_safe_id
from app.core.resource_limits import read_upload_stream_safely
from app.core.job_manager import JobManager
from app.storage.store import DocumentStore
from app.services.parser_service import DocumentService
from app.services.document_search import DocumentSearchService
from app.models.document import (
    DocumentUploadResponse,
    DocumentStatusResponse,
    OutlineResponse,
    OutlineSection,
    SectionNode,
    FigureInfo,
    ChunkItem,
    SearchResponse,
    SourceLocation,
)

router = APIRouter(prefix="/documents", tags=["Documents"])

class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=50)

def _get_authorized_doc(document_id: str, caller: CallerContext) -> Dict[str, Any]:
    """Helper to validate ID, fetch document, and check access rights."""
    validate_safe_id(document_id, "document_id")
    doc = DocumentRepository.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    verify_document_access(doc, caller)
    return doc

@router.post("", response_model=DocumentUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    caller: CallerContext = Depends(get_current_caller)
):
    """
    Upload a document (PDF, DOCX, PPTX).
    Enforces 100MB streaming upload limit, performs security validation,
    creates an asynchronous parsing Job, and returns the document ID.
    """
    # Safe chunked reading with 100MB ceiling (Work Order A-4)
    content = await read_upload_stream_safely(file)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file uploaded."
        )

    doc_id, initial_status, is_cached = DocumentService.upload_and_enqueue(
        filename=file.filename or "uploaded_doc",
        content=content,
        owner_id=caller.owner_id,
        tenant_id=caller.tenant_id,
        access_scope=caller.access_scope
    )

    if not is_cached:
        background_tasks.add_task(
            JobManager.run_job,
            doc_id,
            DocumentService.process_document
        )

    msg = "Document parsing retrieved from cache." if is_cached else "Document uploaded and parsing job enqueued."
    return DocumentUploadResponse(
        document_id=doc_id,
        filename=file.filename or "unknown",
        status=initial_status,
        message=msg
    )

@router.get("/{id}", response_model=DocumentStatusResponse)
async def get_document(
    id: str,
    caller: CallerContext = Depends(get_current_caller)
):
    """Get metadata and overview of a document."""
    doc = _get_authorized_doc(id, caller)
    return DocumentStatusResponse(**doc)

@router.get("/{id}/status", response_model=DocumentStatusResponse)
async def get_document_status(
    id: str,
    caller: CallerContext = Depends(get_current_caller)
):
    """Get real-time parsing job status."""
    doc = _get_authorized_doc(id, caller)
    return DocumentStatusResponse(**doc)

@router.get("/{id}/outline", response_model=OutlineResponse)
async def get_document_outline(
    id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    caller: CallerContext = Depends(get_current_caller)
):
    """
    Get the hierarchical outline and list of sections.
    Supports pagination for large documents (Work Order B-7).
    """
    doc = _get_authorized_doc(id, caller)
    store = DocumentStore(id)
    tree = store.load_parsed_tree()
    if not tree or "sections" not in tree:
        return OutlineResponse(
            document_id=id,
            status=doc["status"],
            sections=[]
        )

    all_sections = tree.get("sections", [])
    paged_sections = all_sections[offset : offset + limit]

    sections_out: List[OutlineSection] = []
    for s in paged_sections:
        sections_out.append(OutlineSection(
            section_id=s["section_id"],
            parent_id=s.get("parent_id"),
            title=s["title"],
            level=s.get("level", 1),
            source=SourceLocation(**s["source"])
        ))

    return OutlineResponse(
        document_id=id,
        status=doc["status"],
        sections=sections_out
    )

@router.get("/{id}/sections/{section_id}", response_model=SectionNode)
async def get_section_detail(
    id: str,
    section_id: str,
    caller: CallerContext = Depends(get_current_caller)
):
    """Get specific section content, connected figure IDs, and table counts."""
    validate_safe_id(section_id, "section_id")
    _get_authorized_doc(id, caller)
    store = DocumentStore(id)
    tree = store.load_parsed_tree()
    if not tree:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not parsed yet")
    
    for s in tree.get("sections", []):
        if s["section_id"] == section_id:
            return SectionNode(**s)
            
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Section not found")

@router.get("/{id}/chunks/{chunk_id}", response_model=ChunkItem)
async def get_chunk_detail(
    id: str,
    chunk_id: str,
    caller: CallerContext = Depends(get_current_caller)
):
    """Get specific token-bounded chunk detail (Work Order B-7)."""
    validate_safe_id(chunk_id, "chunk_id")
    _get_authorized_doc(id, caller)
    store = DocumentStore(id)
    chunks = store.load_chunks()
    for chk in chunks:
        if chk["chunk_id"] == chunk_id:
            return ChunkItem(**chk)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chunk not found")

@router.get("/{id}/figures/{figure_id}", response_model=FigureInfo)
async def get_figure_metadata(
    id: str,
    figure_id: str,
    caller: CallerContext = Depends(get_current_caller)
):
    """Get metadata, caption, and page reference of a figure or visual element."""
    validate_safe_id(figure_id, "figure_id")
    _get_authorized_doc(id, caller)
    store = DocumentStore(id)
    tree = store.load_parsed_tree()
    if not tree:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not parsed yet")
        
    for fig in tree.get("figures", []):
        if fig["figure_id"] == figure_id:
            return FigureInfo(**fig)
            
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Figure not found")

@router.get("/{id}/figures/{figure_id}/asset")
async def get_figure_asset(
    id: str,
    figure_id: str,
    caller: CallerContext = Depends(get_current_caller)
):
    """Stream binary image of a figure or full slide/page render."""
    validate_safe_id(figure_id, "figure_id")
    _get_authorized_doc(id, caller)
    store = DocumentStore(id)
    path = store.get_asset_path(figure_id)
    if not path or not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset file not found")
        
    media_type = f"image/{path.suffix.lstrip('.')}"
    return FileResponse(path, media_type=media_type)

@router.get("/{id}/assets/{asset_id}")
async def get_named_asset(
    id: str,
    asset_id: str,
    caller: CallerContext = Depends(get_current_caller)
):
    """Direct alias to stream binary visual asset (Work Order B-7)."""
    return await get_figure_asset(id, asset_id, caller)

@router.get("/{id}/manifest")
async def get_document_manifest(
    id: str,
    caller: CallerContext = Depends(get_current_caller)
):
    """Get complete document manifest with asset catalog and checksums (Work Order B-7)."""
    _get_authorized_doc(id, caller)
    store = DocumentStore(id)
    manifest = store.load_manifest()
    if not manifest:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Manifest not found")
    return manifest

@router.post("/{id}/search", response_model=SearchResponse)
async def search_document(
    id: str,
    body: SearchRequest,
    caller: CallerContext = Depends(get_current_caller)
):
    """FTS5 full-text keyword search across document chunks with context and source grounding."""
    _get_authorized_doc(id, caller)
    return DocumentSearchService.search(id, body.query, limit=body.limit)
