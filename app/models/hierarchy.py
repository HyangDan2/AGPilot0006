from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field
from app.models.document import SourceLocation

class DocumentTreeNode(BaseModel):
    node_id: str
    parent_id: Optional[str] = None
    node_type: Literal["chapter", "section", "subsection", "paragraph", "table", "figure", "slide", "root"] = "section"
    level: int = 1
    order_index: int = 0
    title: str
    source: SourceLocation
    content: str = ""
    child_ids: List[str] = Field(default_factory=list)
    figure_ids: List[str] = Field(default_factory=list)
    table_count: int = 0
    extraction_method: Literal["native_style", "pdf_toc", "heuristic", "slide_order"] = "native_style"
    structure_status: Literal["confirmed", "inferred", "unknown"] = "confirmed"
    confidence: float = 1.0

class DocumentHierarchy(BaseModel):
    document_id: str
    root_node_ids: List[str] = Field(default_factory=list)
    nodes: Dict[str, DocumentTreeNode] = Field(default_factory=dict)

    def add_node(self, node: DocumentTreeNode) -> None:
        self.nodes[node.node_id] = node
        if node.parent_id and node.parent_id in self.nodes:
            parent = self.nodes[node.parent_id]
            if node.node_id not in parent.child_ids:
                parent.child_ids.append(node.node_id)
        elif not node.parent_id:
            if node.node_id not in self.root_node_ids:
                self.root_node_ids.append(node.node_id)
