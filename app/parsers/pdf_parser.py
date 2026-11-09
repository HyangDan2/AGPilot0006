import io
from pathlib import Path
from typing import List, Dict, Any, Optional
import pymupdf as fitz

from app.parsers.base import BaseParser, ParseResult
from app.models.document import SectionNode, FigureInfo, ChunkItem, SourceLocation
from app.storage.store import DocumentStore
from app.parsers.chunking_engine import ChunkingEngine
from app.parsers.hierarchy_engine import HierarchyEngine
from app.parsers.renderers.pdf_renderer import PDFRenderer
from app.core.resource_limits import verify_page_count_limit

class PDFParser(BaseParser):
    def parse(self, document_id: str, file_path: Path, store: DocumentStore) -> ParseResult:
        doc = fitz.open(str(file_path))
        page_count = len(doc)
        
        # Verify page count limit (Work Order A-4)
        verify_page_count_limit(page_count)
        
        sections: List[SectionNode] = []
        figures: List[FigureInfo] = []
        md_lines: List[str] = [f"# Document: {file_path.name}\n"]
        
        chunking_engine = ChunkingEngine()
        all_chunks: List[ChunkItem] = []
        chunk_counter = 1
        figure_idx = 1

        # 1. Render all pages and extract all images
        page_to_figures: Dict[int, List[str]] = {}
        for page_num in range(1, page_count + 1):
            page = doc[page_num - 1]
            page_figs: List[str] = []
            
            # Save 150 DPI full page render
            page_render_res = PDFRenderer.render_page(doc, page_num, dpi=150)
            if page_render_res.status == "completed" and page_render_res.image_bytes:
                store.save_page_render(page_num, page_render_res.image_bytes, ext="png")

            # Extract embedded images with exact format (Work Order A-5)
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    image_ext = (base_image["ext"] or "png").lower()
                    if image_ext == "jpeg":
                        image_ext = "jpg"
                    
                    fig_id = f"fig_p{page_num}_{figure_idx:03d}"
                    asset_id = f"asset_p{page_num}_{figure_idx:03d}"
                    store.save_figure(fig_id, image_bytes, ext=image_ext)
                    
                    fig = FigureInfo(
                        figure_id=fig_id,
                        document_id=document_id,
                        page_or_slide=page_num,
                        asset_id=asset_id,
                        caption=f"Image on page {page_num}",
                        source_type="embedded_image"
                    )
                    figures.append(fig)
                    page_figs.append(fig_id)
                    figure_idx += 1
                except Exception:
                    pass
            page_to_figures[page_num] = page_figs

        # 2. Extract Document Hierarchy: Bookmarks/TOC vs Heuristics
        toc = doc.get_toc()  # [[lvl, title, page], ...]
        
        if toc:
            # Use TOC bookmarks (Work Order B-3)
            sections = self._parse_with_toc(doc, toc, document_id, page_to_figures)
        else:
            # Heuristic heading detection
            sections = self._parse_with_heuristics(doc, document_id, page_to_figures)

        # 3. Build Markdown & Chunks
        for sec in sections:
            md_lines.append(f"{'#' * min(sec.level + 1, 6)} {sec.title} (Page {sec.source.page or 1})\n")
            if sec.content:
                md_lines.append(sec.content + "\n")
            for fid in sec.figure_ids:
                md_lines.append(f"\n![{fid}](../assets/figures/{fid})\n")
            md_lines.append("\n---\n")

            sec_chunks = chunking_engine.chunk_section(
                document_id=document_id,
                section_id=sec.section_id,
                title=sec.title,
                content=sec.content,
                source=sec.source,
                figure_ids=sec.figure_ids,
                start_chunk_idx=chunk_counter
            )
            all_chunks.extend(sec_chunks)
            chunk_counter += len(sec_chunks)

        full_markdown = "\n".join(md_lines)
        hierarchy = HierarchyEngine.build_tree(document_id, sections)
        doc.close()

        return ParseResult(
            document_id=document_id,
            doc_type="pdf",
            page_count=page_count,
            sections=sections,
            figures=figures,
            chunks=all_chunks,
            markdown=full_markdown,
            metadata={
                "format": "PDF",
                "page_count": page_count,
                "has_bookmarks": bool(toc),
                "tree": hierarchy.model_dump()
            }
        )

    def _parse_with_toc(
        self,
        doc: fitz.Document,
        toc: List[List[Any]],
        document_id: str,
        page_to_figures: Dict[int, List[str]]
    ) -> List[SectionNode]:
        sections: List[SectionNode] = []
        parent_stack: List[tuple[int, str]] = []  # [(level, section_id)]

        for idx, item in enumerate(toc):
            lvl, title, p_num = item[0], item[1].strip(), item[2]
            sec_id = f"sec_{idx + 1:03d}"

            # Determine parent_id
            while parent_stack and parent_stack[-1][0] >= lvl:
                parent_stack.pop()
            parent_id = parent_stack[-1][1] if parent_stack else None

            # Determine page range of this section until next TOC item
            next_p_num = toc[idx + 1][2] if idx + 1 < len(toc) else len(doc)
            sec_text_parts = []
            sec_figures = []

            for p in range(p_num, min(next_p_num + 1, len(doc) + 1)):
                if p <= len(doc):
                    sec_text_parts.append(doc[p - 1].get_text("text").strip())
                    sec_figures.extend(page_to_figures.get(p, []))

            sec = SectionNode(
                section_id=sec_id,
                parent_id=parent_id,
                title=title or f"Section {idx + 1}",
                level=lvl,
                content="\n\n".join(p for p in sec_text_parts if p),
                figure_ids=list(dict.fromkeys(sec_figures)),  # deduplicate preserving order
                table_count=0,
                source=SourceLocation(type="pdf", page=p_num)
            )
            sections.append(sec)
            parent_stack.append((lvl, sec_id))

        return sections

    def _parse_with_heuristics(
        self,
        doc: fitz.Document,
        document_id: str,
        page_to_figures: Dict[int, List[str]]
    ) -> List[SectionNode]:
        """Heuristically segments PDF pages into sections by inspecting text blocks."""
        sections: List[SectionNode] = []
        section_idx = 1

        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            blocks = page.get_text("blocks")  # (x0, y0, x1, y1, "text", block_no, block_type)
            
            page_text_lines = []
            title = None

            for b in blocks:
                if b[6] == 0:  # text block
                    b_text = b[4].strip()
                    if not b_text:
                        continue
                    first_line = b_text.splitlines()[0].strip()
                    # Candidate for section title if short line
                    if not title and len(first_line) < 80:
                        title = first_line
                    page_text_lines.append(b_text)

            if not title:
                title = f"Page {page_num}"

            sec_id = f"sec_{section_idx:03d}"
            sec = SectionNode(
                section_id=sec_id,
                parent_id=None,
                title=title,
                level=1,
                content="\n\n".join(page_text_lines),
                figure_ids=page_to_figures.get(page_num, []),
                table_count=0,
                source=SourceLocation(type="pdf", page=page_num)
            )
            sections.append(sec)
            section_idx += 1

        return sections
