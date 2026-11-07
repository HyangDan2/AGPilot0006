import io
from pathlib import Path
from typing import List, Dict, Any, Optional
import docx
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph

from app.parsers.base import BaseParser, ParseResult
from app.models.document import SectionNode, FigureInfo, ChunkItem, SourceLocation
from app.storage.store import DocumentStore

class DOCXParser(BaseParser):
    def parse(self, document_id: str, file_path: Path, store: DocumentStore) -> ParseResult:
        doc = docx.Document(str(file_path))
        
        sections: List[SectionNode] = []
        figures: List[FigureInfo] = []
        md_lines: List[str] = [f"# Document: {file_path.name}\n"]
        
        # 1. Extract embedded image assets from document package
        figure_idx = 1
        rId_to_fig: Dict[str, FigureInfo] = {}
        for rel in doc.part.rels.values():
            if "image" in rel.target_ref:
                image_part = rel.target_part
                image_bytes = image_part.blob
                ext = Path(image_part.partname).suffix.lstrip(".") or "png"
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

        # 2. Iterate elements in document order (Paragraphs and Tables)
        current_title = "Overview"
        current_level = 1
        current_content_lines: List[str] = []
        current_figure_ids: List[str] = []
        table_count_in_sec = 0
        section_idx = 1
        parent_stack: List[tuple[int, str]] = []  # [(level, section_id)]

        def flush_section():
            nonlocal section_idx, current_title, current_level, current_content_lines
            nonlocal current_figure_ids, table_count_in_sec
            
            content_str = "\n".join(current_content_lines).strip()
            if not content_str and not current_figure_ids and section_idx > 1:
                return

            # Determine parent_id
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
            current_content_lines = []
            current_figure_ids = []
            table_count_in_sec = 0

        for child in doc.element.body:
            if isinstance(child, CT_P):
                p = Paragraph(child, doc)
                text = p.text.strip()
                style_name = p.style.name if p.style else ""
                
                # Check for images referenced in blip fills
                xml_str = child.xml
                for rId, fig in rId_to_fig.items():
                    if rId in xml_str and fig.figure_id not in current_figure_ids:
                        current_figure_ids.append(fig.figure_id)
                
                # Check if Heading
                if style_name.startswith("Heading"):
                    try:
                        lvl = int(style_name.replace("Heading", "").strip())
                    except ValueError:
                        lvl = 1
                    flush_section()
                    current_title = text or f"Heading {lvl}"
                    current_level = lvl
                    md_lines.append(f"{'#' * min(lvl + 1, 6)} {current_title}\n")
                elif text:
                    current_content_lines.append(text)
                    md_lines.append(text + "\n")
                    
            elif isinstance(child, CT_Tbl):
                tbl = Table(child, doc)
                table_count_in_sec += 1
                tbl_md_lines = []
                for row_idx, row in enumerate(tbl.rows):
                    row_cells = [c.text.replace("\n", " ").strip() for c in row.cells]
                    tbl_md_lines.append("| " + " | ".join(row_cells) + " |")
                    if row_idx == 0:
                        tbl_md_lines.append("| " + " | ".join(["---"] * len(row_cells)) + " |")
                
                tbl_text = "\n".join(tbl_md_lines)
                current_content_lines.append(tbl_text)
                md_lines.append("\n" + tbl_text + "\n")

        # Flush final section
        flush_section()

        # If no sections were created, create a default one
        if not sections:
            sections.append(SectionNode(
                section_id="sec_001",
                parent_id=None,
                title="Document Body",
                level=1,
                content="",
                figure_ids=[f.figure_id for f in figures],
                table_count=0,
                source=SourceLocation(type="docx")
            ))

        full_markdown = "\n".join(md_lines)
        chunks = self._build_chunks(document_id, sections)

        return ParseResult(
            document_id=document_id,
            doc_type="docx",
            page_count=1,
            sections=sections,
            figures=figures,
            chunks=chunks,
            markdown=full_markdown,
            metadata={"format": "DOCX", "sections_count": len(sections)}
        )

    def _build_chunks(self, document_id: str, sections: List[SectionNode]) -> List[ChunkItem]:
        chunks: List[ChunkItem] = []
        chunk_idx = 1
        for sec in sections:
            text = sec.content
            approx_tokens = max(1, len(text) // 4)
            chunks.append(ChunkItem(
                chunk_id=f"chk_{chunk_idx:04d}",
                document_id=document_id,
                parent_section_id=sec.section_id,
                content=text,
                figure_ids=sec.figure_ids,
                source=sec.source,
                token_count=approx_tokens
            ))
            chunk_idx += 1
        return chunks
