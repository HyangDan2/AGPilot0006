from typing import Optional, Literal
from pydantic import BaseModel, Field

class AssetMetadata(BaseModel):
    asset_id: str
    document_id: str
    asset_type: Literal["embedded_image", "slide_render", "page_render", "chart"]
    source_type: Literal["pdf", "docx", "pptx", "unknown"]
    slide_or_page: int
    mime_type: str = "image/png"
    file_name: str
    status: Literal["completed", "render_unavailable", "render_failed"] = "completed"
    width: Optional[int] = None
    height: Optional[int] = None
    file_size_bytes: int = 0
    caption: Optional[str] = None
    relative_path: str

class ManifestData(BaseModel):
    document_id: str
    filename: str
    doc_type: str
    parser_version: str
    page_count: int
    section_count: int
    figure_count: int
    created_at: str
    assets: list[AssetMetadata] = Field(default_factory=list)
