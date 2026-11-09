import io
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
import docx
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.parsers.base import BaseParser, ParseResult
from app.models.document import SectionNode, FigureInfo, ChunkItem, SourceLocation
from app.storage.store import DocumentStore
from app.parsers.chunking_engine import ChunkingEngine
from app.parsers.hierarchy_engine import HierarchyEngine
from app.core.resource_limits import verify_page_count_limit

HEADING_PATTERN = re.compile(r"^Heading\s*([1-9])$", re.IGNORECASE)

class DOCXParser(BaseParser):
    def parse(self, document_id: str, file_path: Path, store: DocumentStore) -> ParseResult:
        doc = docx.Document(str(file_path))
        
        sections: List[SectionNode] = []
        figures: List[FigureInfo] = []
        md_lines: List[str] = [f"# Document: {file_path.name}\n"]
        
        chunking_engine = ChunkingEngine()
        all_chunks: List[ChunkItem] = []
        chunk_counter = 1

        # 1. Extract embedded image assets from document package
        figure_idx = 1
        rId_to_fig: Dict[str, FigureInfo] = {}
        for rel in doc.part.rels.values():
            if "image" in rel.target_ref:
                image_part = rel.target_part
                image_bytes = image_part.blob
                ext = Path(image_part.partname).suffix.lstrip(".").lower() or "png"
                if ext == "jpeg":
                    ext = "jpg"
                fig_id = f"fig_d_{figure_idx:03d}"
                asset_id = f"asset_d_{figure_idx:03d}"
                store.save_figure(fig_id, image_bytes, ext=ext)
                
                fig = FigureInfo(
                    figure_id=fig_id,
                    document_id=document_id,
                    page_or_slide=1,
                    asset_id=asset_id,
                    caption=f"Embedded Image {figure_idx}",
                    source_type="embedded_image"
                )
                figures.append(fig)
                rId_to_fig[rel.rId] = fig
                figure_idx += 1

        # 2. Iterate elements in document order
        current_title = "Overview"
        current_level = 1
        current_content_lines: List[str] = []
        current_figure_ids: List[str] = []
        table_count_in_sec = 0
        section_idx = 1
        parent_stack: List[tuple[int, str]] = []  # [(level, section_id)]
        has_encountered_first_heading = False

        def flush_section(is_heading_flush: bool = False):
            nonlocal section_idx, current_title, current_level, current_content_lines
            nonlocal current_figure_ids, table_count_in_sec, has_encountered_first_heading
            
            content_str = "\n".join(current_content_lines).strip()
            
            # If flushing before the first heading and there is NO content/figures, skip creating empty Overview
            if not has_encountered_first_heading and not content_str and not current_figure_ids:
                return

            # Determine parent_id from parent_stack based on heading level
            while parent_stack and parent_stack[-1][0] >= current_level:
                parent_stack.pop()
            parent_id = parent_stack[-1][1] if parent_stack else None
            
            sec_id = f"sec_{section_idx:03d}"
            sec = SectionNode(
                section_id=sec_id,
                parent_id=parent_id,
                title=current_title,
                level=current_level,
                content=content_str,
                figure_ids=list(current_figure_ids),
                table_count=table_count_in_sec,
                source=SourceLocation(type="docx", page=None)
            )
            sections.append(sec)
            parent_stack.append((current_level, sec_id))
            section_idx += 1

            # Chunk the section
            nonlocal chunk_counter
            sec_chunks = chunking_engine.chunk_section(
                document_id=document_id,
                section_id=sec_id,
                title=current_title,
                content=content_str,
                source=sec.source,
                figure_ids=list(current_figure_ids),
                start_chunk_idx=chunk_counter
            )
            all_chunks.extend(sec_chunks)
            chunk_counter += len(sec_chunks)

            # Reset section accumulator
            current_content_lines = []
            current_figure_ids = []
            table_count_in_sec = 0

        for child in doc.element.body:
            if isinstance(child, CT_P):
                p = Paragraph(child, doc)
                text = p.text.strip()
                style_name = (p.style.name or "").strip()
                
                # Check for images inside this paragraph
                xml_str = child.xml
                for rId, fig in rId_to_fig.items():
                    if rId in xml_str and fig.figure_id not in current_figure_ids:
                        current_figure_ids.append(fig.figure_id)
                
                # Check if Heading 1 ~ 9
                m = HEADING_PATTERN.match(style_name)
                if m:
                    flush_section(is_heading_flush=True)
                    has_encountered_first_heading = True
                    current_level = int(m.group(1))
                    current_title = text or f"Heading {current_level}"
                    md_lines.append(f"{'#' * min(current_level + 1, 6)} {current_title}\n")
                elif text:
                    current_content_lines.append(text)
                    md_lines.append(text + "\n")
                    
            elif isinstance(child, CT_Tbl):
                tbl = Table(child, doc)
                table_count_in_sec += 1
                tbl_md_lines = []
                for row_idx, row in enumerate(tbl.rows):
                    row_cells = [c.text.replace("\n", " ").replace("|", "\\|").strip() for c in row.cells]
                    tbl_md_lines.append("| " + " | ".join(row_cells) + " |")
                    if row_idx == 0:
                        tbl_md_lines.append("| " + " | ".join(["---"] * len(row_cells)) + " |")
                
                tbl_text = "\n".join(tbl_md_lines)
                current_content_lines.append(tbl_text)
                md_lines.append("\n" + tbl_text + "\n")

        # Flush final section
        flush_section()

        # If no sections were created at all, create a default body section
        if not sections:
            sec_id = "sec_001"
            def_sec = SectionNode(
                section_id=sec_id,
                parent_id=None,
                title="Document Body",
                level=1,
                content="",
                figure_ids=[f.figure_id for f in figures],
                table_count=0,
                source=SourceLocation(type="docx")
            )
            sections.append(def_sec)
            all_chunks.extend(chunking_engine.chunk_section(
                document_id, sec_id, "Document Body", "", def_sec.source, def_sec.figure_ids, 1
            ))

        full_markdown = "\n".join(md_lines)
        hierarchy = HierarchyEngine.build_tree(document_id, sections)

        return ParseResult(
            document_id=document_id,
            doc_type="docx",
            page_count=1,
            sections=sections,
            figures=figures,
            chunks=all_chunks,
            markdown=full_markdown,
            metadata={
                "format": "DOCX",
                "sections_count": len(sections),
                "tree": hierarchy.model_dump()
            }
        )
