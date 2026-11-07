import io
from pathlib import Path
from typing import List, Dict, Any, Optional
import pptx
from pptx.enum.shapes import MSO_SHAPE_TYPE
from PIL import Image, ImageDraw, ImageFont

from app.parsers.base import BaseParser, ParseResult
from app.models.document import SectionNode, FigureInfo, ChunkItem, SourceLocation
from app.storage.store import DocumentStore

class PPTXParser(BaseParser):
    def parse(self, document_id: str, file_path: Path, store: DocumentStore) -> ParseResult:
        prs = pptx.Presentation(str(file_path))
        slide_count = len(prs.slides)
        
        sections: List[SectionNode] = []
        figures: List[FigureInfo] = []
        md_lines: List[str] = [f"# Presentation: {file_path.name}\n"]
        
        slide_width_emu = prs.slide_width
        slide_height_emu = prs.slide_height
        # Render canvas dimensions (16:9 standard 1920x1080 or 4:3 1440x1080)
        canvas_width = 1280
        scale = canvas_width / max(1, slide_width_emu)
        canvas_height = int(slide_height_emu * scale)

        figure_idx = 1
        
        for slide_idx, slide in enumerate(prs.slides, start=1):
            slide_text_parts: List[str] = []
            slide_figures: List[str] = []
            slide_title = ""
            table_count = 0
            
            # Canvas for full slide rendering
            slide_img = Image.new("RGB", (canvas_width, canvas_height), color="#1b1d19")
            draw = ImageDraw.Draw(slide_img)
            
            # Subtle grid / header border
            draw.rectangle([0, 0, canvas_width, 60], fill="#22251f")
            draw.text((20, 20), f"Slide {slide_idx}", fill="#a2a59c")

            # Extract shapes
            for shape in slide.shapes:
                # 1. Text & Titles
                if shape.has_text_frame:
                    text = shape.text_frame.text.strip()
                    if text:
                        if not slide_title and shape == slide.shapes.title:
                            slide_title = text
                        else:
                            slide_text_parts.append(text)
                            
                        # Draw bounding text outline on slide preview
                        try:
                            left = int(shape.left * scale)
                            top = int(shape.top * scale)
                            width = int(shape.width * scale)
                            height = int(shape.height * scale)
                            draw.rectangle([left, top, left + width, top + height], outline="#45473f", width=1)
                            # Draw snippet text
                            draw.text((left + 4, top + 4), text[:40], fill="#f2f1eb")
                        except Exception:
                            pass

                # 2. Embedded Pictures
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    try:
                        image = shape.image
                        image_bytes = image.blob
                        ext = image.ext or "png"
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

                        # Composite embedded picture onto slide rendering canvas
                        try:
                            left = int(shape.left * scale)
                            top = int(shape.top * scale)
                            width = int(shape.width * scale)
                            height = int(shape.height * scale)
                            if width > 10 and height > 10:
                                with Image.open(io.BytesIO(image_bytes)) as pil_pic:
                                    resized = pil_pic.resize((width, height), Image.Resampling.LANCZOS)
                                    if resized.mode in ("RGBA", "LA"):
                                        slide_img.paste(resized, (left, top), resized)
                                    else:
                                        slide_img.paste(resized, (left, top))
                        except Exception:
                            pass
                    except Exception:
                        pass

                # 3. Tables
                elif shape.has_table:
                    table_count += 1
                    table = shape.table
                    tbl_lines = []
                    for r_idx, row in enumerate(table.rows):
                        row_vals = [c.text.replace("\n", " ").strip() for c in row.cells]
                        tbl_lines.append("| " + " | ".join(row_vals) + " |")
                        if r_idx == 0:
                            tbl_lines.append("| " + " | ".join(["---"] * len(row_vals)) + " |")
                    slide_text_parts.append("\n".join(tbl_lines))

            # Default slide title if missing
            if not slide_title:
                slide_title = f"Slide {slide_idx}"
            
            # Draw Title on slide banner
            draw.text((120, 20), slide_title[:80], fill="#ff824f")

            # 4. Save full slide PNG rendering (Work order requirement)
            buf = io.BytesIO()
            slide_img.save(buf, format="PNG")
            store.save_slide_render(slide_idx, buf.getvalue(), ext="png")

            # Create Section for Slide
            sec_id = f"sec_s{slide_idx:03d}"
            sec_content = "\n\n".join(slide_text_parts)
            sec = SectionNode(
                section_id=sec_id,
                parent_id=None,
                title=slide_title,
                level=1,
                content=sec_content,
                figure_ids=slide_figures,
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
                md_lines.append(f"\n![{fid}](../assets/figures/{fid}.png)\n")
            md_lines.append(f"\n*Slide Render: `assets/slides/slide_{slide_idx:04d}.png`*\n")
            md_lines.append("\n---\n")

        full_markdown = "\n".join(md_lines)
        chunks = self._build_chunks(document_id, sections)

        return ParseResult(
            document_id=document_id,
            doc_type="pptx",
            page_count=slide_count,
            sections=sections,
            figures=figures,
            chunks=chunks,
            markdown=full_markdown,
            metadata={"format": "PPTX", "slide_count": slide_count}
        )

    def _build_chunks(self, document_id: str, sections: List[SectionNode]) -> List[ChunkItem]:
        chunks: List[ChunkItem] = []
        chunk_idx = 1
        for sec in sections:
            text = sec.content or sec.title
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
