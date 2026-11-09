import os
from pathlib import Path
from typing import List, Optional
from PIL import Image
from app.models.asset import AssetMetadata, ManifestData
from app.storage.store import DocumentStore

class AssetService:
    @classmethod
    def generate_manifest(
        cls,
        document_id: str,
        filename: str,
        doc_type: str,
        parser_version: str,
        page_count: int,
        section_count: int,
        figure_count: int,
        created_at: str,
        store: DocumentStore
    ) -> ManifestData:
        """Inspects disk asset directories and generates a comprehensive ManifestData."""
        assets: List[AssetMetadata] = []

        # 1. Figures
        if store.figures_dir.exists():
            for f in sorted(store.figures_dir.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    meta = cls._inspect_file_asset(document_id, f, "embedded_image", doc_type, store.doc_dir)
                    assets.append(meta)

        # 2. Slides
        if store.slides_dir.exists():
            for f in sorted(store.slides_dir.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    meta = cls._inspect_file_asset(document_id, f, "slide_render", doc_type, store.doc_dir)
                    assets.append(meta)

        # 3. Pages
        if store.pages_dir.exists():
            for f in sorted(store.pages_dir.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    meta = cls._inspect_file_asset(document_id, f, "page_render", doc_type, store.doc_dir)
                    assets.append(meta)

        manifest = ManifestData(
            document_id=document_id,
            filename=filename,
            doc_type=doc_type,
            parser_version=parser_version,
            page_count=page_count,
            section_count=section_count,
            figure_count=figure_count,
            created_at=created_at,
            assets=assets
        )
        store.save_manifest(manifest.model_dump())
        return manifest

    @classmethod
    def _inspect_file_asset(
        cls,
        document_id: str,
        path: Path,
        asset_type: str,
        doc_type: str,
        doc_root: Path
    ) -> AssetMetadata:
        ext = path.suffix.lower().lstrip(".")
        mime = "image/png"
        if ext in ("jpg", "jpeg"):
            mime = "image/jpeg"
        elif ext == "webp":
            mime = "image/webp"

        width, height = None, None
        try:
            with Image.open(path) as img:
                width, height = img.size
        except Exception:
            pass

        # Parse slide or page number from filename if available (e.g. slide_0001, page_0002)
        slide_or_page = 1
        name_stem = path.stem
        parts = name_stem.split("_")
        if len(parts) >= 2 and parts[-1].isdigit():
            slide_or_page = int(parts[-1])

        rel_path = str(path.relative_to(doc_root))

        return AssetMetadata(
            asset_id=name_stem,
            document_id=document_id,
            asset_type=asset_type,  # type: ignore
            source_type=doc_type,  # type: ignore
            slide_or_page=slide_or_page,
            mime_type=mime,
            file_name=path.name,
            status="completed",
            width=width,
            height=height,
            file_size_bytes=path.stat().st_size,
            relative_path=rel_path
        )
