import io
from pathlib import Path
from typing import List, Dict, Any, Optional
import pymupdf as fitz
from PIL import Image

from app.parsers.base import BaseParser, ParseResult
from app.models.document import SectionNode, FigureInfo, ChunkItem, SourceLocation
from app.storage.store import DocumentStore

class PDFParser(BaseParser):
    def parse(self, document_id: str, file_path: Path, store: DocumentStore) -> ParseResult:
        doc = fitz.open(str(file_path))
        page_count = len(doc)
        
        sections: List[SectionNode] = []
        figures: List[FigureInfo] = []
        md_lines: List[str] = [f"# Document: {file_path.name}\n"]
        
        # Check PDF Table of Contents (TOC)
        toc = doc.get_toc()  # [[lvl, title, page], ...]
        toc_dict = {item[2]: item[1] for item in toc} if toc else {}

        current_section: Optional[SectionNode] = None
        section_idx = 1
        figure_idx = 1
        
        for page_num in range(1, page_count + 1):
            page = doc[page_num - 1]
            page_text = page.get_text("text").strip()
            
            # 1. Render high-resolution page preview (Full Page Rendering)
            pix = page.get_pixmap(dpi=150)
            store.save_page_render(page_num, pix.tobytes("png"), ext="png")
            
            # 2. Extract embedded images
            page_figures: List[str] = []
            image_list = page.get_images(full=True)
            for img_info in image_list:
                xref = img_info[0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                image_ext = base_image["ext"]
                
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
                page_figures.append(fig_id)
                figure_idx += 1
            
            # Determine Section title
            title = toc_dict.get(page_num)
            if not title:
                # Try finding a header from the first line of text
                lines = [l.strip() for l in page_text.splitlines() if l.strip()]
                if lines and len(lines[0]) < 80:
                    title = lines[0]
                else:
                    title = f"Page {page_num}"
            
            sec_id = f"sec_{section_idx:03d}"
            sec = SectionNode(
                section_id=sec_id,
                parent_id=None,
                title=title,
                level=1,
                content=page_text,
                figure_ids=page_figures,
                table_count=0,
                source=SourceLocation(type="pdf", page=page_num)
            )
            sections.append(sec)
            section_idx += 1
            
            # Markdown entry
            md_lines.append(f"## {title} (Page {page_num})\n")
            if page_text:
                md_lines.append(page_text + "\n")
            for fid in page_figures:
                md_lines.append(f"\n![{fid}](../assets/figures/{fid}.png)\n")
            md_lines.append("\n---\n")

        full_markdown = "\n".join(md_lines)
        
        # Build chunks
        chunks = self._build_chunks(document_id, sections)
        
        doc.close()
        
        return ParseResult(
            document_id=document_id,
            doc_type="pdf",
            page_count=page_count,
            sections=sections,
            figures=figures,
            chunks=chunks,
            markdown=full_markdown,
            metadata={"format": "PDF", "page_count": page_count}
        )

    def _build_chunks(self, document_id: str, sections: List[SectionNode]) -> List[ChunkItem]:
        chunks: List[ChunkItem] = []
        chunk_idx = 1
        for sec in sections:
            text = sec.content
            # Approximate token count: 1 token ~ 4 characters
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
