import hashlib
import uuid
import logging
from pathlib import Path
from typing import Tuple, Dict, Any, Optional

from app.core.security import validate_file_format_and_extension, sanitize_filename, SUPPORTED_TYPES
from app.core.database import DocumentRepository
from app.storage.store import DocumentStore
from app.parsers.dispatcher import ParserDispatcher
from app.services.asset_service import AssetService
from app.services.document_search import DocumentSearchService
from app.config import settings

logger = logging.getLogger("document_intelligence.parser_service")

class DocumentService:
    @staticmethod
    def calculate_cache_key(file_hash: str, doc_type: str, parser_version: str = settings.PROJECT_VERSION) -> str:
        """Computes deterministic cache key including file content hash and parser configuration."""
        raw = f"{file_hash}:{doc_type}:{parser_version}:{settings.CHUNK_TARGET_TOKENS}:{settings.CHUNK_MAX_TOKENS}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def upload_and_enqueue(
        filename: str,
        content: bytes,
        owner_id: str = "default_user",
        tenant_id: str = "default_tenant",
        access_scope: str = "private"
    ) -> Tuple[str, str, bool]:
        """
        Validates, stores original, and queues document parsing.
        Returns (document_id, status, is_cached).
        """
        # 1. Format validation & extension match check
        clean_filename = sanitize_filename(filename)
        doc_type = validate_file_format_and_extension(clean_filename, content)
        mime_type = SUPPORTED_TYPES[doc_type]
        
        # 2. Compute SHA-256 and Cache Key
        file_hash = hashlib.sha256(content).hexdigest()
        cache_key = DocumentService.calculate_cache_key(file_hash, doc_type)
        
        # 3. Check Tenant-Isolated Cache (Work Order A-3)
        cached = DocumentRepository.find_cached_document(cache_key, tenant_id=tenant_id)
        if cached:
            doc_id = f"doc_{uuid.uuid4().hex[:12]}"
            source_store = DocumentStore(cached["document_id"])
            target_store = DocumentStore(doc_id)
            target_store.save_original(clean_filename, content)
            
            # Deep clone assets & re-map document IDs
            clone_success = target_store.clone_from(source_store)
            if clone_success:
                DocumentRepository.create_document(
                    document_id=doc_id,
                    filename=clean_filename,
                    file_hash=file_hash,
                    doc_type=doc_type,
                    mime_type=mime_type,
                    file_size=len(content),
                    parser_version=settings.PROJECT_VERSION,
                    owner_id=owner_id,
                    tenant_id=tenant_id,
                    access_scope=access_scope,
                    cache_key=cache_key
                )
                DocumentRepository.update_status(
                    document_id=doc_id,
                    status="completed",
                    page_count=cached.get("page_count", 0),
                    section_count=cached.get("section_count", 0),
                    figure_count=cached.get("figure_count", 0)
                )
                # Index cloned chunks into FTS
                DocumentSearchService.index_document_chunks(doc_id, target_store.load_chunks())
                return doc_id, "completed", True
            else:
                target_store.cleanup()
                logger.warning(f"Cache cloning failed for doc {doc_id}; falling back to fresh parse.")

        # 4. Fresh document enqueued
        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        store = DocumentStore(doc_id)
        store.save_original(clean_filename, content)
        
        DocumentRepository.create_document(
            document_id=doc_id,
            filename=clean_filename,
            file_hash=file_hash,
            doc_type=doc_type,
            mime_type=mime_type,
            file_size=len(content),
            parser_version=settings.PROJECT_VERSION,
            owner_id=owner_id,
            tenant_id=tenant_id,
            access_scope=access_scope,
            cache_key=cache_key
        )
        return doc_id, "queued", False

    @staticmethod
    def process_document(document_id: str) -> None:
        """Synchronous parsing worker execution (run inside JobManager threadpool)."""
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
            
            # Generate and save manifest.json
            AssetService.generate_manifest(
                document_id=document_id,
                filename=doc_record["filename"],
                doc_type=doc_record["doc_type"],
                parser_version=settings.PROJECT_VERSION,
                page_count=result.page_count,
                section_count=len(result.sections),
                figure_count=len(result.figures),
                created_at=doc_record["created_at"],
                store=store
            )

            # Index chunks for FTS5 keyword search
            DocumentSearchService.index_document_chunks(
                document_id=document_id,
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
            logger.error(f"Error parsing document {document_id}: {err_msg}", exc_info=True)
            DocumentRepository.update_status(
                document_id=document_id,
                status="failed",
                error_message=err_msg
            )
            raise
