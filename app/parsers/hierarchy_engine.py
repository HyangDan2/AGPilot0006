from typing import List, Dict, Any, Optional
from app.models.hierarchy import DocumentTreeNode, DocumentHierarchy
from app.models.document import SectionNode, SourceLocation

class HierarchyEngine:
    @classmethod
    def build_tree(cls, document_id: str, sections: List[SectionNode]) -> DocumentHierarchy:
        """
        Builds a verified DocumentHierarchy tree from a list of sections.
        Calculates order_index, populates child_ids, and ensures parent integrity.
        """
        hierarchy = DocumentHierarchy(document_id=document_id)
        
        # First pass: add all nodes
        for idx, sec in enumerate(sections):
            node = DocumentTreeNode(
                node_id=sec.section_id,
                parent_id=sec.parent_id,
                node_type="section",
                level=sec.level,
                order_index=idx,
                title=sec.title,
                source=sec.source,
                content=sec.content,
                figure_ids=sec.figure_ids,
                table_count=sec.table_count,
                extraction_method="native_style" if sec.source.type == "docx" else ("pdf_toc" if sec.source.type == "pdf" else "slide_order"),
                structure_status="confirmed",
                confidence=1.0
            )
            hierarchy.add_node(node)

        # Second pass: ensure all parent_ids exist; if missing, promote to root
        for node_id, node in hierarchy.nodes.items():
            if node.parent_id and node.parent_id not in hierarchy.nodes:
                node.parent_id = None
                if node_id not in hierarchy.root_node_ids:
                    hierarchy.root_node_ids.append(node_id)

        return hierarchy

    @classmethod
    def outline_from_hierarchy(cls, hierarchy: DocumentHierarchy) -> List[Dict[str, Any]]:
        """Converts hierarchy nodes into flat outline in reading order."""
        nodes_list = sorted(hierarchy.nodes.values(), key=lambda n: n.order_index)
        return [
            {
                "section_id": n.node_id,
                "parent_id": n.parent_id,
                "title": n.title,
                "level": n.level,
                "source": n.source.model_dump(),
                "child_ids": n.child_ids,
                "figure_ids": n.figure_ids,
                "table_count": n.table_count,
                "structure_status": n.structure_status,
                "confidence": n.confidence
            }
            for n in nodes_list
        ]
