"""Knowledge service: format dispatch, revisions, citations, search and the capability manifest.

Everything the API layer needs lives here so the HTTP surface stays a thin translation of
`dk/v1` models. The service is synchronous; FastAPI runs it in a worker thread.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.knowledge import chunking
from app.knowledge.contracts import (
    CONTRACT_EXTENSIONS,
    CONTRACT_VERSION,
    CapabilityManifest,
    ChartData,
    Citation,
    CreateRequest,
    CreateResponse,
    DocNode,
    DocumentRef,
    FormatCapability,
    FormatFamily,
    ImageData,
    IndexState,
    IntegrityCheck,
    KnowledgeError,
    KnowledgeErrorCode,
    KnowledgeErrorBody,
    LimitCapability,
    Locator,
    LocatorKind,
    NodeKind,
    OpKind,
    ProviderStatus,
    ReadCapability,
    ReadMode,
    ReadRequest,
    ReadResponse,
    ReadStats,
    SearchCapability,
    SearchFilters,
    SearchHit,
    SearchMode,
    SearchRequest,
    SearchResponse,
    UpdateRequest,
    UpdateResponse,
    ValidateRequest,
    ValidateResponse,
    WriteCapability,
    compute_content_hash,
    format_family,
    normalize_mime,
    utc_now_iso,
)
from app.knowledge.engines import docx_engine, pdf_engine, pptx_engine, text_engine, xlsx_engine
from app.knowledge.engines.base import ApplyResult
from app.knowledge.store import KnowledgeStore
from app.knowledge.vision import VisionEngine

Embedder = Callable[[str], Optional[Sequence[float]]]
SemanticSearch = Callable[[str, int], List[Tuple[str, str, float]]]  # (document_id, chunk_id, score)


class KnowledgeService:
    def __init__(
        self,
        store: Optional[KnowledgeStore] = None,
        vision: Optional[VisionEngine] = None,
    ) -> None:
        self.store = store or KnowledgeStore()
        self.vision = vision or VisionEngine.from_settings()
        self._embedder: Optional[Embedder] = None
        self._semantic: Optional[SemanticSearch] = None

    # ------------------------------------------------------------------ capabilities
    def set_embedder(self, embedder: Optional[Embedder], semantic: Optional[SemanticSearch] = None) -> None:
        self._embedder = embedder
        self._semantic = semantic

    def manifest(self) -> CapabilityManifest:
        formats = [
            _pdf_capability(),
            _docx_capability(),
            _xlsx_capability(),
            _pptx_capability(),
            _text_capability(),
            _csv_capability(),
            _image_capability(),
        ]
        modes = [SearchMode.KEYWORD]
        if self._embedder is not None or self._semantic is not None:
            modes.extend([SearchMode.VECTOR, SearchMode.HYBRID])
        return CapabilityManifest(
            contract_version=CONTRACT_VERSION,
            contracts_accepted=[CONTRACT_VERSION],
            extensions=list(CONTRACT_EXTENSIONS),
            service="agtools",
            build=str(getattr(settings, "PROJECT_VERSION", "")),
            generated_at=utc_now_iso(),
            formats=formats,
            search=SearchCapability(
                modes=modes,
                metadata_filters=[
                    "mime_types", "families", "document_ids", "names", "path_prefix",
                    "updated_after", "metadata",
                ],
                incremental_index=True,
                ready_reuse=True,
                keyword_fallback=True,
            ),
            vision=self.vision.status(),
            limits=LimitCapability(
                max_file_bytes=int(getattr(settings, "MAX_FILE_SIZE_BYTES", 0) or 0),
                max_pages=int(getattr(settings, "MAX_PAGES_LIMIT", 0) or 0),
                max_slides=int(getattr(settings, "MAX_PAGES_LIMIT", 0) or 0),
                max_sheets=200,
                max_chars_read=int(getattr(settings, "KNOWLEDGE_MAX_READ_CHARS", 400000) or 400000),
                max_search_top_k=50,
                max_cells_read=int(getattr(settings, "KNOWLEDGE_MAX_CELLS", 50000) or 50000),
            ),
            error_codes=[code.value for code in KnowledgeErrorCode],
        )

    # ------------------------------------------------------------------ create
    def create(self, request: CreateRequest) -> CreateResponse:
        family = format_family(request.mime_type)
        template_path = None
        if request.template_document_id:
            template_bytes, template_ref = self.store.read_bytes(request.template_document_id)
            template_path = self.store.revision_path(
                template_ref.document_id, template_ref.revision, template_ref.name
            )
            if format_family(template_ref.mime_type) is not family:
                raise KnowledgeError(
                    KnowledgeErrorCode.INVALID_REQUEST,
                    "template format does not match requested format",
                    detail={"template": template_ref.mime_type, "requested": request.mime_type},
                )
        warnings: List[str] = []
        if family is FormatFamily.DOCX:
            data = docx_engine.create_docx(
                request.blocks, template=template_path, metadata=request.metadata
            )
        elif family is FormatFamily.XLSX:
            data = xlsx_engine.create_xlsx(
                request.blocks, template=template_path, metadata=request.metadata
            )
        elif family is FormatFamily.PPTX:
            data = pptx_engine.create_pptx(
                request.blocks, template=template_path, metadata=request.metadata
            )
        elif family in (FormatFamily.TEXT, FormatFamily.CSV):
            text = _blocks_to_text(request.blocks)
            data = text.encode("utf-8")
        else:
            raise KnowledgeError(
                KnowledgeErrorCode.UNSUPPORTED_FORMAT,
                f"cannot create {request.mime_type}",
                detail={"family": family.value},
            )
        if request.overwrite_document_id:
            existing = self.store.ref(request.overwrite_document_id)
            ref = self.store.write_revision(
                existing.document_id,
                data,
                expected_revision=existing.revision,
                name=request.name or existing.name,
                mime_type=request.mime_type,
                metadata=request.metadata,
            )
            created = False
        else:
            ref = self.store.create(
                name=request.name,
                mime_type=request.mime_type,
                data=data,
                metadata=request.metadata,
            )
            created = True
        self.index_document(ref)
        return CreateResponse(document=ref, created=created, warnings=warnings)

    # ------------------------------------------------------------------ read
    def read(self, request: ReadRequest) -> ReadResponse:
        data, ref = self.store.read_bytes(request.document_id, request.revision)
        response = self.read_bytes(data, ref, request)
        return response

    def read_bytes(self, data: bytes, ref: DocumentRef, request: ReadRequest) -> ReadResponse:
        family = format_family(ref.mime_type)
        path = self.store.revision_path(ref.document_id, ref.revision, ref.name)
        if not path.exists():
            path = self._materialise(data, ref)
        if family is FormatFamily.PDF:
            response = pdf_engine.read_path(path, ref, request)
        elif family is FormatFamily.DOCX:
            response = docx_engine.read_path(path, ref, request)
        elif family is FormatFamily.XLSX:
            response = xlsx_engine.read_path(path, ref, request)
        elif family is FormatFamily.PPTX:
            response = pptx_engine.read_path(path, ref, request)
        elif family in (FormatFamily.TEXT, FormatFamily.CSV, FormatFamily.IMAGE):
            response = text_engine.read_text(data, ref, request)
        else:
            raise KnowledgeError(
                KnowledgeErrorCode.UNSUPPORTED_FORMAT,
                f"no reader for {ref.mime_type}",
                detail={"mime_type": ref.mime_type},
            )
        if family is FormatFamily.PDF and any(
            warning.endswith("no_text_layer") for warning in response.warnings
        ) and request.include_images:
            response = self._augment_ocr(data, ref, request, response)
        response.stats.elapsed_ms = response.stats.elapsed_ms or 0
        return response

    def _materialise(self, data: bytes, ref: DocumentRef) -> Path:
        path = self.store.document_dir(ref.document_id) / "scratch" / f"{ref.revision:04d}-{ref.name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _augment_ocr(
        self, data: bytes, ref: DocumentRef, request: ReadRequest, response: ReadResponse
    ) -> ReadResponse:
        if not self.vision.status().available:
            if "vision_unavailable" not in response.warnings:
                response.warnings.append("vision_unavailable")
            return response
        try:
            import fitz

            document = fitz.open(self.store.revision_path(ref.document_id, ref.revision, ref.name))
            for page_number, warning in enumerate(
                [w for w in response.warnings if w.endswith("no_text_layer")], start=1
            ):
                pass
            for page_index in range(document.page_count):
                page = document.load_page(page_index)
                if (page.get_text("text") or "").strip():
                    continue
                pixmap = page.get_pixmap(dpi=144)
                result_vision = self.vision.analyze(pixmap.tobytes("png"), "image/png", "Extract all text verbatim.")
                if result_vision.available and result_vision.text:
                    response.nodes.append(
                        DocNode(
                            node_id=f"ocr-p{page_index + 1}",
                            kind=NodeKind.PARAGRAPH,
                            text=result_vision.text,
                            locator=Locator(kind=LocatorKind.PAGE, page=page_index + 1),
                            meta={"engine": result_vision.engine, "model": result_vision.model},
                        )
                    )
                    response.warnings.append(f"page_{page_index + 1}_ocr_used")
                else:
                    response.warnings.append(f"page_{page_index + 1}_vision_unavailable")
            document.close()
        except Exception as exc:  # pragma: no cover - defensive
            response.warnings.append(f"ocr_failed:{type(exc).__name__}")
        return response

    # ------------------------------------------------------------------ update
    def update(self, request: UpdateRequest) -> UpdateResponse:
        data, ref = self.store.read_bytes(request.document_id)
        if request.expected_revision is not None and request.expected_revision != ref.revision:
            raise KnowledgeError(
                KnowledgeErrorCode.REVISION_CONFLICT,
                f"expected revision {request.expected_revision}, current is {ref.revision}",
                detail={"expected": request.expected_revision, "current": ref.revision},
                retryable=True,
            )
        family = format_family(ref.mime_type)
        path = self.store.revision_path(ref.document_id, ref.revision, ref.name)
        if family is FormatFamily.DOCX:
            result = docx_engine.apply_docx(path, ref, request.operations)
        elif family is FormatFamily.XLSX:
            result = xlsx_engine.apply_xlsx(path, ref, request.operations)
        elif family is FormatFamily.PPTX:
            result = pptx_engine.apply_pptx(path, ref, request.operations)
        elif family in (FormatFamily.TEXT, FormatFamily.CSV):
            result = _apply_text(data, ref, request)
        else:
            raise KnowledgeError(
                KnowledgeErrorCode.WRITE_NOT_SUPPORTED,
                f"no writer for {ref.mime_type}",
                detail={"family": family.value},
            )

        warnings = list(result.warnings)
        if not result.applied:
            warnings.append("no_operations_applied")
        if request.dry_run:
            return UpdateResponse(
                document=ref,
                previous_revision=ref.revision,
                applied=result.applied,
                skipped=result.skipped,
                dry_run=True,
                warnings=warnings,
                diff_summary="; ".join(result.diff),
            )

        updated = self.store.write_revision(
            ref.document_id,
            result.data,
            expected_revision=ref.revision,
            name=request.output_name or ref.name,
            mime_type=ref.mime_type,
            metadata={"last_ops": result.applied[:20]},
        )
        self.index_document(updated)
        return UpdateResponse(
            document=updated,
            previous_revision=ref.revision,
            applied=result.applied,
            skipped=result.skipped,
            dry_run=False,
            warnings=warnings,
            diff_summary="; ".join(result.diff),
        )

    def rollback(self, document_id: str, to_revision: int, *, expected_revision: Optional[int]) -> DocumentRef:
        ref = self.store.rollback(document_id, to_revision, expected_revision=expected_revision)
        self.index_document(ref)
        return ref

    # ------------------------------------------------------------------ validate
    def validate(self, request: ValidateRequest) -> ValidateResponse:
        ref, checks = self.store.validate(request.document_id, request.revision)
        errors: List[KnowledgeErrorBody] = []
        if request.deep:
            data, _ = self.store.read_bytes(ref.document_id, ref.revision)
            try:
                read_request = ReadRequest(document_id=ref.document_id, mode=ReadMode.OUTLINE)
                self.read_bytes(data, ref, read_request)
                checks.append(IntegrityCheck(name="parse_outline", ok=True, detail="reader accepted file"))
            except KnowledgeError as exc:
                checks.append(IntegrityCheck(name="parse_outline", ok=False, detail=exc.body.code.value))
                errors.append(exc.body)
        failed = [check for check in checks if not check.ok]
        if failed and not errors:
            errors.append(
                KnowledgeErrorBody(
                    code=KnowledgeErrorCode.INTEGRITY_MISMATCH,
                    message="; ".join(f"{check.name}: {check.detail}" for check in failed),
                )
            )
        return ValidateResponse(document=ref, ok=all(check.ok for check in checks), checks=checks, errors=errors)

    # ------------------------------------------------------------------ search
    def index_document(self, ref: DocumentRef, *, force: bool = False) -> int:
        if not force and self.store.index_is_ready(ref.document_id, ref.revision):
            return 0
        data, _ = self.store.read_bytes(ref.document_id, ref.revision)
        try:
            response = self.read_bytes(
                data, ref, ReadRequest(document_id=ref.document_id, mode=ReadMode.FULL)
            )
        except KnowledgeError:
            return 0
        chunks = chunking.chunk_nodes(response.nodes, document_key=ref.document_key)
        self.store.index_document(ref, chunks)
        return len(chunks)

    def search(self, request: SearchRequest) -> SearchResponse:
        mime_types = list(request.filters.mime_types)
        if request.filters.families:
            for family in request.filters.families:
                mime_types.extend(_mimes_for_family(family))
        document_ids = list(request.filters.document_ids) or list(request.document_ids) or None

        provider_status = ProviderStatus(
            keyword="ok",
            embedding="ok" if (self._embedder or self._semantic) else "unavailable",
            vision="ok" if self.vision.status().available else "unavailable",
        )
        degraded = False
        reason: Optional[str] = None
        effective = request.mode

        rows = self.store.search_chunks(
            request.query,
            top_k=request.top_k,
            document_ids=document_ids,
            mime_types=mime_types,
            mode=request.mode.value,
        )
        semantic_rows: List[Dict[str, Any]] = []
        if request.mode in (SearchMode.VECTOR, SearchMode.HYBRID):
            if self._semantic is not None:
                try:
                    for document_id, chunk_id, score in self._semantic(request.query, request.top_k * 3):
                        if document_ids and document_id not in document_ids:
                            continue
                        semantic_rows.append(
                            {"document_id": document_id, "chunk_id": chunk_id, "score": float(score)}
                        )
                except Exception:
                    provider_status.embedding = "error"
                    degraded = True
                    reason = "semantic_search_failed"
            else:
                degraded = True
                reason = "embedding_unavailable"
                effective = SearchMode.KEYWORD
        if request.mode is SearchMode.VECTOR and not semantic_rows:
            degraded = True
            effective = SearchMode.KEYWORD
            reason = reason or "vector_index_empty"

        hits: Dict[str, SearchHit] = {}
        for row in rows:
            document = self._safe_ref(row["document_id"], row["revision"])
            if document is None:
                continue
            filters_ok = _passes_filters(document, request.filters)
            if not filters_ok:
                continue
            locator_payload = row.get("locator") or {}
            hit = SearchHit(
                hit_id=f"{document.document_id}:{row['chunk_id']}",
                document=document,
                locator=Locator(**locator_payload) if locator_payload else Locator(),
                excerpt=_excerpt(row["content"], request.query),
                score=float(row.get("score", 0.0)),
                keyword_score=float(row.get("score", 0.0)),
                matched_by=["keyword"],
                citation=None,
                chunk_id=row["chunk_id"],
            )
            hits[hit.hit_id] = hit

        if semantic_rows:
            existing_by_chunk = {hit.chunk_id: hit for hit in hits.values()}
            for semantic in semantic_rows:
                document = self._safe_ref(semantic["document_id"])
                if document is None or not _passes_filters(document, request.filters):
                    continue
                key = f"{document.document_id}:{semantic['chunk_id']}"
                if key in hits:
                    hit = hits[key]
                    hit.vector_score = semantic["score"]
                    hit.score = 0.5 * (hit.keyword_score or 0.0) + 0.5 * semantic["score"]
                    hit.matched_by.append("vector")
                elif key in existing_by_chunk:
                    hit = existing_by_chunk[key]
                    hit.vector_score = semantic["score"]
                    hit.score = max(hit.score, semantic["score"])
                    if "vector" not in hit.matched_by:
                        hit.matched_by.append("vector")

        ordered = sorted(hits.values(), key=lambda hit: hit.score, reverse=True)
        deduped: List[SearchHit] = []
        seen_content: set[str] = set()
        duplicates = 0
        for hit in ordered:
            fingerprint = compute_content_hash(hit.excerpt.strip().encode("utf-8"))
            if fingerprint in seen_content:
                duplicates += 1
                continue
            seen_content.add(fingerprint)
            hit.citation = _citation_for(hit)
            deduped.append(hit)
            if len(deduped) >= request.top_k:
                break

        response = SearchResponse(
            query=request.query,
            mode_requested=request.mode,
            mode_effective=effective,
            degraded=degraded,
            degradation_reason=reason,
            provider_status=provider_status,
            total_matches=len(hits),
            duplicates_removed=duplicates,
            results=deduped,
            warnings=[] if not degraded else [f"degraded:{reason}"] if reason else [],
        )
        return response

    def _safe_ref(self, document_id: str, revision: Optional[int] = None) -> Optional[DocumentRef]:
        try:
            return self.store.ref(document_id, revision)
        except KnowledgeError:
            return None

    # ------------------------------------------------------------------ files
    def download(self, document_id: str, revision: Optional[int] = None) -> Tuple[bytes, DocumentRef]:
        return self.store.read_bytes(document_id, revision)

    def delete(self, document_id: str) -> None:
        self.store.delete(document_id)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _citation_for(hit: SearchHit) -> Citation:
    from app.knowledge.contracts import make_citation

    return make_citation(hit.document, hit.locator, hit.excerpt, content_hash=hit.document.content_hash)


def _excerpt(content: str, query: str, *, limit: int = 320) -> str:
    lowered = content.lower()
    positions = [lowered.find(term.lower()) for term in query.split() if term.strip()]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return content[:limit].strip()
    start = max(0, min(positions) - 80)
    return content[start : start + limit].strip()


def _passes_filters(document: DocumentRef, filters: SearchFilters) -> bool:
    if filters.names and not any(name.lower() in document.name.lower() for name in filters.names):
        return False
    if filters.path_prefix and not document.metadata.get("path", "").startswith(filters.path_prefix):
        return False
    if filters.updated_after:
        stamp = document.metadata.get("updated_at")
        if stamp and str(stamp) < filters.updated_after.isoformat():
            return False
    for key, value in filters.metadata.items():
        if str(document.metadata.get(key, "")) != value:
            return False
    return True


def _mimes_for_family(family: FormatFamily) -> List[str]:
    from app.knowledge.contracts import FAMILY_BY_MIME

    return [mime for mime, item in FAMILY_BY_MIME.items() if item is family]


def _blocks_to_text(blocks) -> str:
    parts: List[str] = []
    for block in blocks:
        if block.kind.value == "heading":
            parts.append("#" * max(1, block.level) + " " + block.text)
        elif block.kind.value == "paragraph":
            parts.append(block.text)
        elif block.kind.value == "bullets":
            parts.extend(f"- {item}" for item in block.bullets)
        elif block.kind.value == "table":
            parts.extend("\t".join(str(cell) for cell in row) for row in block.rows)
    return "\n\n".join(parts) + "\n"


def _apply_text(data: bytes, ref: DocumentRef, request: UpdateRequest) -> ApplyResult:
    text = data.decode("utf-8", errors="replace")
    applied: List[str] = []
    skipped: List[str] = []
    warnings: List[str] = []
    diff: List[str] = []
    for operation in request.operations:
        if operation.op is OpKind.REPLACE_TEXT and operation.find:
            occurrences = text.count(operation.find)
            if occurrences:
                text = (
                    text.replace(operation.find, operation.replace or "")
                    if operation.all_occurrences
                    else text.replace(operation.find, operation.replace or "", 1)
                )
                applied.append(f"replace_text:{occurrences}")
                diff.append(f"replaced {occurrences} occurrence(s)")
            else:
                skipped.append("replace_text:not_found")
        elif operation.op is OpKind.INSERT_PARAGRAPH and operation.text is not None:
            text = text.rstrip("\n") + "\n\n" + operation.text + "\n"
            applied.append("insert_paragraph:append")
        else:
            skipped.append(f"{operation.op.value}:unsupported_for_text")
    return ApplyResult(text.encode("utf-8"), applied, skipped, warnings, diff)


# --------------------------------------------------------------------------------------
# capability tables
# --------------------------------------------------------------------------------------


def _pdf_capability() -> FormatCapability:
    available, reason = _module_available("fitz")
    return FormatCapability(
        mime_type="application/pdf",
        family=FormatFamily.PDF,
        extensions=[".pdf"],
        read=ReadCapability(
            charts=False, formulas=False, notes=False, styles=True, page_range=True, outline=True
        ),
        write=WriteCapability(create=False, edit=False),
        citation_kinds=[LocatorKind.PAGE, LocatorKind.DOCUMENT],
        available=available,
        unavailable_reason=reason,
        notes="page-addressed read, tables and image metadata; encrypted/corrupt documents rejected",
    )


def _docx_capability() -> FormatCapability:
    available, reason = _module_available("docx")
    return FormatCapability(
        mime_type=docx_engine.DOCX_MIME,
        family=FormatFamily.DOCX,
        extensions=[".docx"],
        read=ReadCapability(tables=True, images=True, styles=True, notes=False, partial=True),
        write=WriteCapability(
            create=True, edit=True, insert_image=True, replace_image=True, tables=True,
            preserve_styles=True, template_based=True,
        ),
        citation_kinds=[LocatorKind.PARAGRAPH, LocatorKind.SECTION, LocatorKind.DOCUMENT],
        available=available,
        unavailable_reason=reason,
        notes="headers, footers, runs and section properties are preserved on edit",
    )


def _xlsx_capability() -> FormatCapability:
    available, reason = _module_available("openpyxl")
    return FormatCapability(
        mime_type=xlsx_engine.XLSX_MIME,
        family=FormatFamily.XLSX,
        extensions=[".xlsx"],
        read=ReadCapability(
            tables=True, charts=True, formulas=True, styles=True, merged_cells=True, partial=True
        ),
        write=WriteCapability(
            create=True, edit=True, tables=True, charts=True, preserve_styles=True, template_based=True
        ),
        citation_kinds=[LocatorKind.SHEET, LocatorKind.CELL, LocatorKind.RANGE],
        available=available,
        unavailable_reason=reason,
        notes="formula and cached value are reported separately; written formulas need recalculation",
    )


def _pptx_capability() -> FormatCapability:
    available, reason = _module_available("pptx")
    return FormatCapability(
        mime_type=pptx_engine.PPTX_MIME,
        family=FormatFamily.PPTX,
        extensions=[".pptx"],
        read=ReadCapability(tables=True, images=True, charts=True, notes=True, styles=True, partial=True),
        write=WriteCapability(
            create=True, edit=True, insert_image=True, tables=True, preserve_styles=True, template_based=True
        ),
        citation_kinds=[LocatorKind.SLIDE, LocatorKind.DOCUMENT],
        available=available,
        unavailable_reason=reason,
        notes="layouts, masters and theme are preserved; slide add/delete/reorder supported",
    )


def _text_capability() -> FormatCapability:
    return FormatCapability(
        mime_type="text/plain",
        family=FormatFamily.TEXT,
        extensions=[".txt", ".md"],
        read=ReadCapability(tables=False, images=False, partial=True),
        write=WriteCapability(create=True, edit=True),
        citation_kinds=[LocatorKind.PARAGRAPH, LocatorKind.SECTION, LocatorKind.DOCUMENT],
        notes="markdown headings are used as the section path",
    )


def _csv_capability() -> FormatCapability:
    return FormatCapability(
        mime_type="text/csv",
        family=FormatFamily.CSV,
        extensions=[".csv"],
        read=ReadCapability(tables=True, partial=True),
        write=WriteCapability(create=True, edit=True),
        citation_kinds=[LocatorKind.DOCUMENT],
    )


def _image_capability() -> FormatCapability:
    available = VisionEngine.from_settings().status().available
    return FormatCapability(
        mime_type="image/png",
        family=FormatFamily.IMAGE,
        extensions=[".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"],
        read=ReadCapability(body=False, structure=False, tables=False, images=True),
        write=WriteCapability(create=False, edit=False),
        citation_kinds=[LocatorKind.IMAGE, LocatorKind.DOCUMENT],
        available=True,
        notes="image content is only described when a vision capability is bound" if not available else "vision enabled",
    )


def _module_available(module: str) -> Tuple[bool, Optional[str]]:
    import importlib.util

    if importlib.util.find_spec(module) is None:
        return False, f"{module}_not_installed"
    return True, None
