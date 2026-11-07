from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, List
from pydantic import BaseModel, Field
from app.models.document import SectionNode, FigureInfo, ChunkItem
from app.storage.store import DocumentStore

class ParseResult(BaseModel):
    document_id: str
    doc_type: str
    page_count: int
    sections: List[SectionNode] = Field(default_factory=list)
    figures: List[FigureInfo] = Field(default_factory=list)
    chunks: List[ChunkItem] = Field(default_factory=list)
    markdown: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def to_tree_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "doc_type": self.doc_type,
            "page_count": self.page_count,
            "sections": [s.model_dump() for s in self.sections],
            "figures": [f.model_dump() for f in self.figures],
            "metadata": self.metadata,
        }

class BaseParser(ABC):
    @abstractmethod
    def parse(self, document_id: str, file_path: Path, store: DocumentStore) -> ParseResult:
        """Parse file, extract hierarchy, extract assets, and render slides/pages."""
        pass
