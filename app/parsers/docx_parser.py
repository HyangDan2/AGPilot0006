from pathlib import Path
from typing import List

from app.models.document import ChunkItem, FigureInfo, SectionNode, SourceLocation
from app.parsers.base import BaseParser, ParseResult
from app.parsers.chunking_engine import ChunkingEngine
from app.parsers.hierarchy_engine import HierarchyEngine
from app.services.docx_markdown import convert_docx
from app.storage.store import DocumentStore


class DOCXParser(BaseParser):
    def parse(self, document_id: str, file_path: Path, store: DocumentStore) -> ParseResult:
        conv = convert_docx(
            file_path,
            filename=file_path.name,
            table_mode="auto",
            image_mode="assets",
            asset_prefix="assets/",
        )

        # 1. Persist extracted figures to DocumentStore
        figures: List[FigureInfo] = []
        for fig in conv.figures:
            store.save_figure(fig.figure_id, fig.data, ext=fig.ext)
            figures.append(
                FigureInfo(
                    figure_id=fig.figure_id,
                    document_id=document_id,
                    page_or_slide=1,
                    asset_id=fig.asset_id,
                    caption=fig.caption,
                    source_type=f"embedded_image:{fig.context}",
                )
            )

        # 2. Build SectionNodes & ChunkItems with accurate section <-> figure mapping
        chunking_engine = ChunkingEngine()
        sections: List[SectionNode] = []
        all_chunks: List[ChunkItem] = []
        chunk_counter = 1

        for sec in conv.sections:
            node = SectionNode(
                section_id=sec.section_id,
                parent_id=sec.parent_id,
                title=sec.title,
                level=sec.level,
                content=sec.content,
                figure_ids=list(sec.figure_ids),
                table_count=sec.table_count,
                source=SourceLocation(type="docx", page=None),
            )
            sections.append(node)
            sec_chunks = chunking_engine.chunk_section(
                document_id=document_id,
                section_id=sec.section_id,
                title=sec.title,
                content=sec.content,
                source=node.source,
                figure_ids=list(sec.figure_ids),
                start_chunk_idx=chunk_counter,
            )
            all_chunks.extend(sec_chunks)
            chunk_counter += len(sec_chunks)

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
                source=SourceLocation(type="docx"),
            )
            sections.append(def_sec)
            all_chunks.extend(
                chunking_engine.chunk_section(
                    document_id, sec_id, "Document Body", "", def_sec.source, def_sec.figure_ids, 1
                )
            )

        hierarchy = HierarchyEngine.build_tree(document_id, sections)

        return ParseResult(
            document_id=document_id,
            doc_type="docx",
            page_count=1,
            sections=sections,
            figures=figures,
            chunks=all_chunks,
            markdown=conv.markdown,
            metadata={
                "format": "DOCX",
                "sections_count": len(sections),
                "tables_count": conv.stats["tables"],
                "merged_tables_count": conv.stats["merged_tables"],
                "images_count": conv.stats["images"],
                "tree": hierarchy.model_dump(),
            },
        )
