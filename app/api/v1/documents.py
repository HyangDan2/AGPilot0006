from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, status, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.core.database import DocumentRepository
from app.storage.store import DocumentStore
from app.services.parser_service import DocumentService
from app.models.document import (
    DocumentUploadResponse,
    DocumentStatusResponse,
    OutlineResponse,
    OutlineSection,
    SectionNode,
    FigureInfo,
    SearchResponse,
    SearchResultItem,
    SourceLocation,
)

router = APIRouter(prefix="/documents", tags=["Documents"])

class SearchRequest(BaseModel):
    query: str
    limit: int = 10

@router.post("", response_model=DocumentUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):
    """
    Upload a document (PDF, DOCX, PPTX).
    Performs security validation, creates an asynchronous parsing Job, and returns the document ID.
    """
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file uploaded."
        )

    doc_id, initial_status, is_cached = DocumentService.upload_and_enqueue(
        filename=file.filename or "uploaded_doc",
        content=content
    )

    if not is_cached:
        background_tasks.add_task(DocumentService.process_document, doc_id)

    msg = "Document parsing retrieved from cache." if is_cached else "Document uploaded and parsing job enqueued."
    return DocumentUploadResponse(
        document_id=doc_id,
        filename=file.filename or "unknown",
        status=initial_status,
        message=msg
    )

@router.get("/{id}", response_model=DocumentStatusResponse)
async def get_document(id: str):
    """Get metadata and overview of a document."""
    doc = DocumentRepository.get_document(id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return DocumentStatusResponse(**doc)

@router.get("/{id}/status", response_model=DocumentStatusResponse)
async def get_document_status(id: str):
    """Get real-time parsing job status."""
    doc = DocumentRepository.get_document(id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return DocumentStatusResponse(**doc)

@router.get("/{id}/outline", response_model=OutlineResponse)
async def get_document_outline(id: str):
    """
    Get the hierarchical outline and list of sections.
    AGPilot0005 calls this to survey the document structure.
    """
    doc = DocumentRepository.get_document(id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    
    store = DocumentStore(id)
    tree = store.load_parsed_tree()
    if not tree or "sections" not in tree:
        return OutlineResponse(
            document_id=id,
            status=doc["status"],
            sections=[]
        )

    sections_out: List[OutlineSection] = []
    for s in tree.get("sections", []):
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
async def get_section_detail(id: str, section_id: str):
    """Get specific section content, connected figure IDs, and table counts."""
    store = DocumentStore(id)
    tree = store.load_parsed_tree()
    if not tree:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not parsed yet")
    
    for s in tree.get("sections", []):
        if s["section_id"] == section_id:
            return SectionNode(**s)
            
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Section not found")

@router.get("/{id}/figures/{figure_id}", response_model=FigureInfo)
async def get_figure_metadata(id: str, figure_id: str):
    """Get metadata, caption, and page reference of a figure or visual element."""
    store = DocumentStore(id)
    tree = store.load_parsed_tree()
    if not tree:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not parsed yet")
        
    for fig in tree.get("figures", []):
        if fig["figure_id"] == figure_id:
            return FigureInfo(**fig)
            
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Figure not found")

@router.get("/{id}/figures/{figure_id}/asset")
async def get_figure_asset(id: str, figure_id: str):
    """Stream binary image of a figure or full slide/page render."""
    store = DocumentStore(id)
    path = store.get_figure_path(figure_id)
    if not path or not path.exists():
        # Check if requested figure is a slide render (e.g., slide_0001)
        slide_path = store.slides_dir / f"{figure_id}.png"
        if slide_path.exists():
            return FileResponse(slide_path, media_type="image/png")
        page_path = store.pages_dir / f"{figure_id}.png"
        if page_path.exists():
            return FileResponse(page_path, media_type="image/png")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset file not found")
        
    media_type = f"image/{path.suffix.lstrip('.')}"
    return FileResponse(path, media_type=media_type)

@router.post("/{id}/search", response_model=SearchResponse)
async def search_document(id: str, body: SearchRequest):
    """Keyword search across document chunks with context and source grounding."""
    store = DocumentStore(id)
    chunks = store.load_chunks()
    if not chunks:
        return SearchResponse(document_id=id, query=body.query, total_matches=0, results=[])

    query_terms = [t.lower() for t in body.query.strip().split() if t.strip()]
    matches: List[SearchResultItem] = []

    for chk in chunks:
        content_lower = chk["content"].lower()
        score = sum(content_lower.count(term) for term in query_terms)
        if score > 0:
            snippet = chk["content"][:200] + ("..." if len(chk["content"]) > 200 else "")
            matches.append(SearchResultItem(
                chunk_id=chk["chunk_id"],
                section_id=chk.get("parent_section_id"),
                snippet=snippet,
                score=float(score),
                source=SourceLocation(**chk["source"]),
                figure_ids=chk.get("figure_ids", [])
            ))

    # Sort descending by match score
    matches.sort(key=lambda x: x.score, reverse=True)
    selected = matches[:body.limit]

    return SearchResponse(
        document_id=id,
        query=body.query,
        total_matches=len(matches),
        results=selected
    )
