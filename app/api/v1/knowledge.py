"""`dk/v1` HTTP surface for the Document & Knowledge Engine.

The legacy `/api/v1/documents` API is untouched; this router is the contract endpoint AGCore
targets. Every response carries `contract_version`; errors are `{"error": {code, message,
detail, retryable}}` with the status code defined by the contract table.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.config import settings
from app.core.auth import CallerContext, get_current_caller
from app.core.resource_limits import read_upload_stream_safely
from app.knowledge.contracts import (
    CONTRACT_VERSION,
    CreateRequest,
    CreateResponse,
    DocumentRef,
    KnowledgeError,
    KnowledgeErrorCode,
    ReadInlineRequest,
    ReadRequest,
    ReadResponse,
    SearchRequest,
    SearchResponse,
    UpdateRequest,
    UpdateResponse,
    ValidateRequest,
    ValidateResponse,
    normalize_mime,
)
from app.knowledge.service import KnowledgeService
from app.knowledge.store import KnowledgeStore

router = APIRouter(prefix="/knowledge", tags=["Knowledge Engine (dk/v1)"])

_SERVICES: dict[str, KnowledgeService] = {}


def get_service() -> KnowledgeService:
    """One service per storage root.

    Tests (and any deployment that relocates `DATA_DIR`) get an isolated store; a process
    serving one root keeps a single service instance.
    """

    from pathlib import Path

    from app.knowledge.store import knowledge_root

    root = str(knowledge_root())
    if root not in _SERVICES:
        _SERVICES[root] = KnowledgeService(store=KnowledgeStore(Path(root)))
    return _SERVICES[root]


def _owner_metadata(caller: CallerContext) -> dict:
    return {"owner_id": caller.owner_id, "tenant_id": caller.tenant_id}


def _authorize(ref: DocumentRef, caller: CallerContext) -> None:
    metadata = ref.metadata or {}
    owner = metadata.get("owner_id")
    tenant = metadata.get("tenant_id")
    if caller.access_scope == "admin" or owner is None:
        return
    if owner != caller.owner_id or tenant != caller.tenant_id:
        raise KnowledgeError(
            KnowledgeErrorCode.PERMISSION_DENIED,
            "document belongs to another owner",
            detail={"document_id": ref.document_id},
        )


@router.get("/manifest")
def manifest(
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    """Capability manifest: formats, read/write depth, search modes, vision, limits, errors."""

    return service.manifest()


@router.get("/documents")
def list_documents(
    limit: int = Query(100, ge=1, le=500),
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    refs = []
    for ref in service.store.list_documents(limit=limit):
        metadata = ref.metadata or {}
        if caller.access_scope == "admin" or metadata.get("owner_id") in (None, caller.owner_id):
            if caller.access_scope == "admin" or metadata.get("tenant_id") in (None, caller.tenant_id):
                refs.append(ref)
    return {"contract_version": CONTRACT_VERSION, "documents": [ref.model_dump(mode="json") for ref in refs]}


@router.post("/documents", response_model=CreateResponse, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    content = await read_upload_stream_safely(file)
    if not content:
        raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "empty upload")
    mime = normalize_mime(file.content_type, filename=file.filename or "")
    ref = service.store.create(
        name=file.filename or "document",
        mime_type=mime,
        data=content,
        metadata=_owner_metadata(caller),
    )
    chunks = service.index_document(ref, force=True)
    response = CreateResponse(document=ref, created=True)
    if not chunks:
        response.warnings.append("not_indexed")
    return response


@router.post("/documents/from-blocks", response_model=CreateResponse, status_code=201)
def create_document(
    request: CreateRequest,
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    if not request.metadata.get("owner_id"):
        request.metadata.update(_owner_metadata(caller))
    return service.create(request)


@router.get("/documents/{document_id}")
def get_document(
    document_id: str,
    revision: Optional[int] = Query(None, ge=0),
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    ref = service.store.ref(document_id, revision)
    _authorize(ref, caller)
    return {"contract_version": CONTRACT_VERSION, "document": ref.model_dump(mode="json")}


@router.get("/documents/{document_id}/content")
def download_document(
    document_id: str,
    revision: Optional[int] = Query(None, ge=0),
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    data, ref = service.download(document_id, revision)
    _authorize(ref, caller)
    return Response(
        content=data,
        media_type=ref.mime_type,
        headers={"Content-Disposition": f'inline; filename="{ref.name}"'},
    )


@router.post("/read", response_model=ReadResponse)
def read_document(
    request: ReadRequest,
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    ref = service.store.ref(request.document_id, request.revision)
    _authorize(ref, caller)
    return service.read(request)


@router.post("/search", response_model=SearchResponse)
def search(
    request: SearchRequest,
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    response = service.search(request)
    if caller.access_scope != "admin":
        allowed = []
        for hit in response.results:
            metadata = hit.document.metadata or {}
            if metadata.get("owner_id") in (None, caller.owner_id) and metadata.get(
                "tenant_id"
            ) in (None, caller.tenant_id):
                allowed.append(hit)
        response.results = allowed
        response.total_matches = len(allowed)
    return response


@router.post("/documents/{document_id}/update", response_model=UpdateResponse)
def update_document(
    document_id: str,
    request: UpdateRequest,
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    if request.document_id != document_id:
        raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "document_id mismatch")
    ref = service.store.ref(document_id, request.expected_revision)
    _authorize(ref, caller)
    return service.update(request)


@router.get("/documents/{document_id}/revisions")
def list_revisions(
    document_id: str,
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    ref = service.store.ref(document_id)
    _authorize(ref, caller)
    meta = service.store._load_meta(document_id)
    return {
        "contract_version": CONTRACT_VERSION,
        "document_id": document_id,
        "current_revision": meta["current_revision"],
        "revisions": [
            {
                "revision": entry["revision"],
                "name": entry["name"],
                "content_hash": entry["content_hash"],
                "size_bytes": entry["size_bytes"],
                "created_at": entry["created_at"],
            }
            for entry in meta["revisions"]
        ],
    }


@router.post("/documents/{document_id}/rollback", response_model=CreateResponse)
def rollback_document(
    document_id: str,
    to_revision: int = Query(..., ge=1),
    expected_revision: Optional[int] = Query(None, ge=1),
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    ref = service.store.ref(document_id)
    _authorize(ref, caller)
    new_ref = service.rollback(document_id, to_revision, expected_revision=expected_revision)
    return CreateResponse(document=new_ref, created=False, warnings=[f"rollback_to:{to_revision}"])


@router.post("/documents/{document_id}/validate", response_model=ValidateResponse)
def validate_document(
    document_id: str,
    request: Optional[ValidateRequest] = None,
    deep: bool = Query(True),
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    if request is None:
        request = ValidateRequest(document_id=document_id, deep=deep)
    if request.document_id != document_id:
        raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "document_id mismatch")
    ref = service.store.ref(document_id, request.revision)
    _authorize(ref, caller)
    return service.validate(request)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(
    document_id: str,
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    ref = service.store.ref(document_id)
    _authorize(ref, caller)
    service.delete(document_id)
    return Response(status_code=204)

@router.post("/read-inline", response_model=ReadResponse)
def read_inline(
    body: ReadInlineRequest,
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    """Read caller-supplied bytes (AGCore workspace blob) without storing a second copy."""

    import base64 as _base64

    from app.knowledge.contracts import compute_content_hash

    try:
        data = _base64.b64decode(body.content_base64, validate=True)
    except Exception as exc:
        raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "invalid content_base64") from exc
    if not data:
        raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "empty payload")
    ref = DocumentRef(
        document_id=f"inline_{compute_content_hash(data).split(':', 1)[1][:16]}",
        name=body.name,
        mime_type=body.mime_type,
        revision=body.request.revision or 1,
        content_hash=compute_content_hash(data),
        size_bytes=len(data),
        metadata=_owner_metadata(caller),
    )
    return service.read_bytes(data, ref, body.request)


@router.post("/vision/analyze")
def vision_analyze(
    body: dict,
    caller: CallerContext = Depends(get_current_caller),
    service: KnowledgeService = Depends(get_service),
):
    """Vision analysis through the capability-driven engine; never invents a description."""

    import base64 as _base64

    image = body.get("image_base64")
    if not isinstance(image, str) or not image:
        raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "image_base64 required")
    try:
        data = _base64.b64decode(image.split(",", 1)[-1])
    except Exception as exc:
        raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "invalid image_base64") from exc
    result = service.vision.analyze(
        data, str(body.get("mime_type") or "image/png"), str(body.get("prompt") or "")
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "available": result.available,
        "text": result.text,
        "engine": result.engine,
        "model": result.model,
        "capability_source": result.capability_source,
        "error": result.error,
    }
