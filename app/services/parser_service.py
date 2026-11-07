import hashlib
import uuid
import traceback
from pathlib import Path
from typing import Tuple, Dict, Any, Optional

from app.core.security import validate_file_magic_bytes, sanitize_filename, SUPPORTED_TYPES
from app.core.database import DocumentRepository
from app.storage.store import DocumentStore
from app.parsers.dispatcher import ParserDispatcher
from app.config import settings

class DocumentService:
    @staticmethod
    def upload_and_enqueue(
        filename: str,
        content: bytes
    ) -> Tuple[str, str, bool]:
        """
        Validates, stores original, and queues document parsing.
        Returns (document_id, status, is_cached).
        """
        # 1. Security & format validation (Magic bytes + Zip bomb + Macros check)
        doc_type = validate_file_magic_bytes(content)
        mime_type = SUPPORTED_TYPES[doc_type]
        clean_filename = sanitize_filename(filename)
        
        # 2. Compute SHA-256 Hash
        file_hash = hashlib.sha256(content).hexdigest()
        
        # 3. Check Cache
        cached = DocumentRepository.find_by_hash(file_hash)
        if cached:
            # Re-use cached completed document metadata
            doc_id = f"doc_{uuid.uuid4().hex[:12]}"
            # Clone parsed assets to new document store
            source_store = DocumentStore(cached["document_id"])
            target_store = DocumentStore(doc_id)
            target_store.save_original(clean_filename, content)
            
            tree = source_store.load_parsed_tree()
            md = source_store.load_markdown()
            chunks = source_store.load_chunks()
            if tree and md is not None:
                tree["document_id"] = doc_id
                target_store.save_parsed_data(tree, md, chunks)
                DocumentRepository.create_document(
                    document_id=doc_id,
                    filename=clean_filename,
                    file_hash=file_hash,
                    doc_type=doc_type,
                    mime_type=mime_type,
                    file_size=len(content)
                )
                DocumentRepository.update_status(
                    document_id=doc_id,
                    status="completed",
                    page_count=cached.get("page_count", 0),
                    section_count=cached.get("section_count", 0),
                    figure_count=cached.get("figure_count", 0)
                )
                return doc_id, "completed", True

        # 4. Fresh document
        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        store = DocumentStore(doc_id)
        store.save_original(clean_filename, content)
        
        DocumentRepository.create_document(
            document_id=doc_id,
            filename=clean_filename,
            file_hash=file_hash,
            doc_type=doc_type,
            mime_type=mime_type,
            file_size=len(content)
        )
        return doc_id, "queued", False

    @staticmethod
    def process_document(document_id: str) -> None:
        """Background parsing job."""
        doc_record = DocumentRepository.get_document(document_id)
        if not doc_record:
            return

        store = DocumentStore(document_id)
        original_file = store.get_original_path()
        if not original_file:
            DocumentRepository.update_status(
                document_id, status="failed", error_message="Original file not found on disk"
            )
            return

        try:
            DocumentRepository.update_status(document_id, status="processing")
            
            parser = ParserDispatcher.get_parser(doc_record["doc_type"])
            result = parser.parse(document_id, original_file, store)
            
            # Save parsed tree, markdown, and chunks
            store.save_parsed_data(
                document_tree=result.to_tree_dict(),
                markdown_text=result.markdown,
                chunks=[c.model_dump() for c in result.chunks]
            )
            
            # Update DB status
            DocumentRepository.update_status(
                document_id=document_id,
                status="completed",
                page_count=result.page_count,
                section_count=len(result.sections),
                figure_count=len(result.figures)
            )

        except Exception as e:
            err_msg = f"{type(e).__name__}: {str(e)}"
            DocumentRepository.update_status(
                document_id=document_id,
                status="failed",
                error_message=err_msg
            )
