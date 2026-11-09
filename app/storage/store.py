import json
import os
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, List
from app.config import settings
from app.core.security import validate_safe_id

class DocumentStore:
    """Manages the disk structure for a single document under documents/{document_id}/"""

    def __init__(self, document_id: str):
        validate_safe_id(document_id, "document_id")
        self.document_id = document_id
        self.doc_dir = settings.DOCUMENTS_DIR / document_id
        self.original_dir = self.doc_dir / "original"
        self.parsed_dir = self.doc_dir / "parsed"
        self.assets_dir = self.doc_dir / "assets"
        self.figures_dir = self.assets_dir / "figures"
        self.pages_dir = self.assets_dir / "pages"
        self.slides_dir = self.assets_dir / "slides"
        self.manifest_path = self.doc_dir / "manifest.json"

    def init_layout(self) -> None:
        """Create full directory hierarchy for the document."""
        self.original_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        self.figures_dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.slides_dir.mkdir(parents=True, exist_ok=True)

    def save_original(self, filename: str, content: bytes) -> Path:
        self.init_layout()
        safe_name = os.path.basename(filename)
        file_path = self.original_dir / safe_name
        file_path.write_bytes(content)
        return file_path

    def get_original_path(self) -> Optional[Path]:
        if not self.original_dir.exists():
            return None
        files = list(self.original_dir.iterdir())
        return files[0] if files else None

    def save_parsed_data(
        self,
        document_tree: Dict[str, Any],
        markdown_text: str,
        chunks: List[Dict[str, Any]]
    ) -> None:
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. document.json
        (self.parsed_dir / "document.json").write_text(
            json.dumps(document_tree, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        
        # 2. document.md
        (self.parsed_dir / "document.md").write_text(
            markdown_text,
            encoding="utf-8"
        )
        
        # 3. chunks.jsonl
        chunks_path = self.parsed_dir / "chunks.jsonl"
        with open(chunks_path, "w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    def save_manifest(self, manifest: Dict[str, Any]) -> None:
        self.doc_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def load_manifest(self) -> Optional[Dict[str, Any]]:
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return None

    def load_parsed_tree(self) -> Optional[Dict[str, Any]]:
        tree_path = self.parsed_dir / "document.json"
        if tree_path.exists():
            return json.loads(tree_path.read_text(encoding="utf-8"))
        return None

    def load_markdown(self) -> Optional[str]:
        md_path = self.parsed_dir / "document.md"
        if md_path.exists():
            return md_path.read_text(encoding="utf-8")
        return None

    def load_chunks(self) -> List[Dict[str, Any]]:
        chunks_path = self.parsed_dir / "chunks.jsonl"
        chunks = []
        if chunks_path.exists():
            with open(chunks_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        chunks.append(json.loads(line.strip()))
        return chunks

    def save_figure(self, figure_id: str, content: bytes, ext: str = "png") -> Path:
        validate_safe_id(figure_id, "figure_id")
        self.figures_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.figures_dir / f"{figure_id}.{ext}"
        file_path.write_bytes(content)
        return file_path

    def save_page_render(self, page_num: int, content: bytes, ext: str = "png") -> Path:
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.pages_dir / f"page_{page_num:04d}.{ext}"
        file_path.write_bytes(content)
        return file_path

    def save_slide_render(self, slide_num: int, content: bytes, ext: str = "png") -> Path:
        self.slides_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.slides_dir / f"slide_{slide_num:04d}.{ext}"
        file_path.write_bytes(content)
        return file_path

    def get_figure_path(self, figure_id: str) -> Optional[Path]:
        validate_safe_id(figure_id, "figure_id")
        if not self.figures_dir.exists():
            return None
        for path in self.figures_dir.glob(f"{figure_id}.*"):
            return path
        return None

    def get_asset_path(self, asset_id: str) -> Optional[Path]:
        validate_safe_id(asset_id, "asset_id")
        # Search in figures, slides, and pages
        for search_dir in (self.figures_dir, self.slides_dir, self.pages_dir):
            if search_dir.exists():
                for path in search_dir.glob(f"{asset_id}.*"):
                    return path
                # Also check without extension if asset_id is e.g. "slide_0001"
                exact = search_dir / f"{asset_id}.png"
                if exact.exists():
                    return exact
        return None

    def clone_from(self, source_store: "DocumentStore") -> bool:
        """
        Deep copies all parsed data and visual assets from source_store
        while updating references to self.document_id (Work Order A-3).
        Guarantees ID integrity across document.json, chunks.jsonl, and manifest.json.
        """
        try:
            self.init_layout()
            
            # 1. Copy and remap parsed tree
            tree = source_store.load_parsed_tree()
            if not tree:
                return False
            tree["document_id"] = self.document_id
            for fig in tree.get("figures", []):
                fig["document_id"] = self.document_id

            # 2. Copy and remap chunks
            source_chunks = source_store.load_chunks()
            remapped_chunks = []
            for chk in source_chunks:
                chk["document_id"] = self.document_id
                remapped_chunks.append(chk)

            # 3. Copy markdown
            md = source_store.load_markdown() or ""

            # Save remapped parsed data
            self.save_parsed_data(tree, md, remapped_chunks)

            # 4. Deep copy all asset directories
            for src_dir, dst_dir in [
                (source_store.figures_dir, self.figures_dir),
                (source_store.pages_dir, self.pages_dir),
                (source_store.slides_dir, self.slides_dir),
            ]:
                if src_dir.exists():
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    for f in src_dir.iterdir():
                        if f.is_file():
                            shutil.copy2(f, dst_dir / f.name)

            # 5. Copy & remap manifest
            manifest = source_store.load_manifest()
            if manifest:
                manifest["document_id"] = self.document_id
                for a in manifest.get("assets", []):
                    a["document_id"] = self.document_id
                self.save_manifest(manifest)

            return True
        except Exception:
            self.cleanup()
            return False

    def cleanup(self) -> None:
        """Safely remove the document directory."""
        if self.doc_dir.exists():
            shutil.rmtree(self.doc_dir, ignore_errors=True)
