from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field

class SourceLocation(BaseModel):
    type: Literal["pdf", "docx", "pptx", "unknown"]
    page: Optional[int] = None
    slide: Optional[int] = None
    slide_start: Optional[int] = None
    slide_end: Optional[int] = None

class FigureInfo(BaseModel):
    figure_id: str
    document_id: str
    page_or_slide: int
    asset_id: str
    caption: Optional[str] = None
    visual_description: Optional[str] = None
    analysis_status: Literal["not_requested", "processing", "completed", "failed"] = "not_requested"
    source_type: str = "embedded_image"  # embedded_image | slide_render | page_render | chart

class SectionNode(BaseModel):
    section_id: str
    parent_id: Optional[str] = None
    title: str
    level: int = 1
    content: str = ""
    figure_ids: List[str] = Field(default_factory=list)
    table_count: int = 0
    source: SourceLocation

class OutlineSection(BaseModel):
    section_id: str
    parent_id: Optional[str] = None
    title: str
    level: int = 1
    source: SourceLocation

class OutlineResponse(BaseModel):
    document_id: str
    status: str
    sections: List[OutlineSection]

class ChunkItem(BaseModel):
    chunk_id: str
    document_id: str
    parent_section_id: Optional[str] = None
    content: str
    figure_ids: List[str] = Field(default_factory=list)
    source: SourceLocation
    token_count: int

class DocumentStatusResponse(BaseModel):
    document_id: str
    status: str
    page_count: int = 0
    section_count: int = 0
    figure_count: int = 0
    error_message: Optional[str] = None
    created_at: str
    updated_at: str

class DocumentUploadResponse(BaseModel):
    document_id: str
    filename: str
    status: str
    message: str

class SearchResultItem(BaseModel):
    chunk_id: str
    section_id: Optional[str] = None
    snippet: str
    score: float
    source: SourceLocation
    figure_ids: List[str] = Field(default_factory=list)

class SearchResponse(BaseModel):
    document_id: str
    query: str
    total_matches: int
    results: List[SearchResultItem]
