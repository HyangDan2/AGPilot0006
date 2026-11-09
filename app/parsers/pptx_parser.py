import io
from pathlib import Path
from typing import List, Dict, Any, Optional
import pptx
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.parsers.base import BaseParser, ParseResult
from app.models.document import SectionNode, FigureInfo, ChunkItem, SourceLocation
from app.storage.store import DocumentStore
from app.parsers.renderers.libreoffice_renderer import LibreOfficeRenderer
from app.parsers.chunking_engine import ChunkingEngine
from app.parsers.hierarchy_engine import HierarchyEngine
from app.core.resource_limits import verify_page_count_limit

class PPTXParser(BaseParser):
    def parse(self, document_id: str, file_path: Path, store: DocumentStore) -> ParseResult:
        prs = pptx.Presentation(str(file_path))
        slide_count = len(prs.slides)
        
        # Verify page count limit (Work Order A-4)
        verify_page_count_limit(slide_count)
        
        sections: List[SectionNode] = []
        figures: List[FigureInfo] = []
        md_lines: List[str] = [f"# Presentation: {file_path.name}\n"]
        
        chunking_engine = ChunkingEngine()
        all_chunks: List[ChunkItem] = []
        chunk_counter = 1
        figure_idx = 1

        # 1. True Slide Visual Rendering (Work Order A-1)
        slide_renders = LibreOfficeRenderer.render_slides(file_path, slide_count)
        for s_res in slide_renders:
            s_num = s_res.slide_num
            render_fig_id = f"slide_render_{s_num:04d}"
            
            if s_res.status == "completed" and s_res.image_bytes:
                store.save_slide_render(s_num, s_res.image_bytes, ext="png")
                figures.append(FigureInfo(
                    figure_id=render_fig_id,
                    document_id=document_id,
                    page_or_slide=s_num,
                    asset_id=render_fig_id,
                    caption=f"Full slide {s_num} visual rendering",
                    source_type="slide_render",
                    analysis_status="not_requested"
                ))
            else:
                # Record render unavailable or failed status
                figures.append(FigureInfo(
                    figure_id=render_fig_id,
                    document_id=document_id,
                    page_or_slide=s_num,
                    asset_id=render_fig_id,
                    caption=f"Slide {s_num} visual rendering ({s_res.status})",
                    source_type="slide_render",
                    analysis_status="failed" if s_res.status == "render_failed" else "not_requested"
                ))

        # 2. Extract slides structure & content
        for slide_idx, slide in enumerate(prs.slides, start=1):
            slide_text_parts: List[str] = []
            slide_figures: List[str] = []
            table_count = 0
            slide_title = ""

            # Step A: Identify Slide Title
            # Priority 1: Title placeholder
            try:
                if slide.shapes.title and slide.shapes.title.text_frame:
                    slide_title = slide.shapes.title.text_frame.text.strip()
            except Exception:
                pass

            # Step B: Traverse Shapes (including Groups)
            shapes_to_process = list(slide.shapes)
            while shapes_to_process:
                shape = shapes_to_process.pop(0)

                # Expand Group Shapes
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    for sub_shape in shape.shapes:
                        shapes_to_process.append(sub_shape)
                    continue

                # Title fallback if not found yet
                if not slide_title and shape.has_text_frame:
                    t = shape.text_frame.text.strip()
                    if t and len(t) < 80:
                        slide_title = t

                # Text Extraction
                if shape.has_text_frame:
                    text = shape.text_frame.text.strip()
                    if text and text != slide_title:
                        slide_text_parts.append(text)

                # Embedded Pictures (Original Asset Extraction)
                elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    try:
                        image = shape.image
                        image_bytes = image.blob
                        ext = (image.ext or "png").lower()
                        if ext == "jpeg":
                            ext = "jpg"
                        fig_id = f"fig_s{slide_idx}_{figure_idx:03d}"
                        asset_id = f"asset_s{slide_idx}_{figure_idx:03d}"
                        store.save_figure(fig_id, image_bytes, ext=ext)

                        fig = FigureInfo(
                            figure_id=fig_id,
                            document_id=document_id,
                            page_or_slide=slide_idx,
                            asset_id=asset_id,
                            caption=f"Slide {slide_idx} Image {figure_idx}",
                            source_type="embedded_image"
                        )
                        figures.append(fig)
                        slide_figures.append(fig_id)
                        figure_idx += 1
                    except Exception:
                        pass

                # Tables
                elif shape.has_table:
                    table_count += 1
                    table = shape.table
                    tbl_lines = []
                    for r_idx, row in enumerate(table.rows):
                        row_vals = [c.text.replace("\n", " ").replace("|", "\\|").strip() for c in row.cells]
                        tbl_lines.append("| " + " | ".join(row_vals) + " |")
                        if r_idx == 0:
                            tbl_lines.append("| " + " | ".join(["---"] * len(row_vals)) + " |")
                    slide_text_parts.append("\n".join(tbl_lines))

            # Default slide title
            if not slide_title:
                slide_title = f"Slide {slide_idx}"

            # Create Section Node for Slide
            sec_id = f"sec_s{slide_idx:03d}"
            sec_content = "\n\n".join(slide_text_parts).strip()
            
            # Link slide render figure
            render_fig_id = f"slide_render_{slide_idx:04d}"
            all_sec_figures = [render_fig_id] + slide_figures

            sec = SectionNode(
                section_id=sec_id,
                parent_id=None,
                title=slide_title,
                level=1,
                content=sec_content,
                figure_ids=all_sec_figures,
                table_count=table_count,
                source=SourceLocation(
                    type="pptx",
                    slide=slide_idx,
                    slide_start=slide_idx,
                    slide_end=slide_idx
                )
            )
            sections.append(sec)

            # Markdown
            md_lines.append(f"## Slide {slide_idx}: {slide_title}\n")
            if sec_content:
                md_lines.append(sec_content + "\n")
            for fid in slide_figures:
                md_lines.append(f"\n![{fid}](../assets/figures/{fid})\n")
            md_lines.append(f"\n*Slide Render: `assets/slides/slide_{slide_idx:04d}.png`*\n")
            md_lines.append("\n---\n")

            # Chunking for this slide section
            sec_chunks = chunking_engine.chunk_section(
                document_id=document_id,
                section_id=sec_id,
                title=slide_title,
                content=sec_content,
                source=sec.source,
                figure_ids=all_sec_figures,
                start_chunk_idx=chunk_counter
            )
            all_chunks.extend(sec_chunks)
            chunk_counter += len(sec_chunks)

        full_markdown = "\n".join(md_lines)
        hierarchy = HierarchyEngine.build_tree(document_id, sections)

        return ParseResult(
            document_id=document_id,
            doc_type="pptx",
            page_count=slide_count,
            sections=sections,
            figures=figures,
            chunks=all_chunks,
            markdown=full_markdown,
            metadata={
                "format": "PPTX",
                "slide_count": slide_count,
                "tree": hierarchy.model_dump(),
                "libreoffice_available": LibreOfficeRenderer.is_available()
            }
        )
