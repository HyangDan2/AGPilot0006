"""R3 capability manifest for AGTools (AGPilot0006) — read-only.

WO-R3-PREDEV §1: describe the AGTools HTTP surface that already exists in this
service in the AGCore R3 *WP-07 Unified Tool Registry* vocabulary
(``UnifiedToolSpec``), so that the R3 planner / execution engines can see what
AGTools can actually do without importing a single line of R2.1 code.

The spec is aligned with the WP-07 implementation that owns it —
``AGPilot0005 app/workflow/registry.py`` (``UnifiedToolSpec``, ``extra="forbid"``):

* exactly the keys listed in :data:`UNIFIED_TOOL_SPEC_FIELDS`,
* ``permission`` ∈ ``read | write | admin``,
* ``side_effect`` ∈ ``none | read | write | external | destructive``,
* ``write``/``external``/``destructive`` require a non-``read`` permission,
* ``error_contract`` is a mapping, not an id,
* one live implementation per ``capability`` (a second tool for the same
  capability raises ``DuplicateCapabilityError`` in the registry).

Design constraints (do not relax):

* **Read-only.** The module only reads the route table of this service. It
  never creates a document, never writes a file, never touches storage, the
  database or the network.
* **No coupling to R2.1.** Nothing here imports ``app.parsers``,
  ``app.services``, ``app.storage`` or ``app.core``. The parser / writer / RAG
  code is inventoried as *routes*, not imported as modules, so an R3 change can
  never alter parser behaviour.
* **Route-true.** Every claimed endpoint is verified against the decorators
  actually present in ``app/api/v1/*.py`` (:func:`check_route_coverage`), so the
  manifest cannot silently drift from the running service.

Vocabulary used here is defined in ``docs/r3/AGTOOLS_R3_CAPABILITY_CONTRACT.md``.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "R3_CAPABILITY_CONTRACT",
    "MANIFEST_SCHEMA",
    "PROVIDER",
    "SERVICE_ID",
    "SERVICE_VERSION",
    "UNIFIED_TOOL_SPEC_FIELDS",
    "SIDE_EFFECTS",
    "PERMISSIONS",
    "ERROR_CONTRACTS",
    "SHARED_CAPABILITY_INTERFACE",
    "RouteCoverage",
    "list_r3_capabilities",
    "list_r3_capability_bindings",
    "capability_manifest",
    "get_r3_capability",
    "find_capabilities",
    "capability_coverage",
    "collect_api_prefix",
    "collect_declared_routes",
    "check_route_coverage",
]

# --------------------------------------------------------------------------- #
# Contract identity
# --------------------------------------------------------------------------- #

R3_CAPABILITY_CONTRACT = "r3-capability/v1"
MANIFEST_SCHEMA = 1
PROVIDER = "agtools"
SERVICE_ID = "AGPilot0006"
SERVICE_VERSION = "2.0.0"

#: The AGCore R3 WP-07 ``UnifiedToolSpec`` field set (``AGPilot0005
#: app/workflow/registry.py``). A capability entry carries exactly these keys and
#: nothing else, so a WP-07 consumer may build the spec directly
#: (``UnifiedToolSpec(**entry)``) without tolerating unknown fields.
UNIFIED_TOOL_SPEC_FIELDS: Tuple[str, ...] = (
    "name",
    "namespace",
    "version",
    "description",
    "input_schema",
    "output_schema",
    "capability",
    "permission",
    "side_effect",
    "timeout_seconds",
    "error_contract",
    "approval_required",
    "source",
)

#: WP-07 ``permission`` vocabulary. The AGTools *authentication* requirement
#: (service token, per-document owner isolation) is a transport fact and lives on
#: the binding, not here.
PERMISSIONS: Mapping[str, str] = {
    "read": "Reads service state only; no persistence.",
    "write": "May create or overwrite service state (documents, generated files) or reach an external system.",
    "admin": "Administrator console action. No AGTools capability uses this; admin surfaces are excluded.",
}

#: WP-07 ``side_effect`` vocabulary and how AGTools uses each value.
SIDE_EFFECTS: Mapping[str, str] = {
    "none": "No interaction beyond the request itself.",
    "read": "Reads AGTools state (documents, chunks, figures, manifests).",
    "write": "Persists a document or a generated file inside AGTools storage.",
    "external": "Leaves the AGTools boundary and calls a third-party endpoint (web search / web fetch).",
    "destructive": "Deletes or irreversibly overwrites. AGTools exposes no such capability.",
}

#: ``UnifiedToolSpec.error_contract`` payloads (a mapping in WP-07, not an id).
ERROR_CONTRACTS: Mapping[str, Dict[str, Any]] = {
    "agtools.http_error/v1": {
        "id": "agtools.http_error/v1",
        "shape": {"detail": "string"},
        "codes": {
            "400": "invalid or empty request payload",
            "401": "missing or invalid service token",
            "403": "caller is not allowed to touch this document",
            "404": "document / section / chunk / figure / generated file not found",
            "409": "document snapshot changed, parsing incomplete, or cursor invalid",
            "413": "upload or request body above the size ceiling",
            "422": "document could not be parsed or query parameters are out of range",
            "429": "rate limited (web search)",
            "502": "upstream provider failure (web search / web fetch)",
            "503": "upstream provider unavailable",
        },
        "retryable": ["429", "502", "503"],
        "non_retryable": ["400", "401", "403", "404", "409", "413", "422"],
    },
    "agtools.parse_job/v1": {
        "id": "agtools.parse_job/v1",
        "inherits": "agtools.http_error/v1",
        "shape": {"detail": "string"},
        "job_surface": {
            "endpoint": "document.status",
            "failed": {"status": "failed", "error_message": "string"},
        },
        "note": "POST /api/v1/documents answers 202; parse failures surface on the document status, not on the upload response.",
        "retryable": ["429", "502", "503"],
    },
}

#: R3 shared capability interface (AGCore R3 ``OFFICE_CAPABILITIES`` in
#: ``app/workflow/office_adapter.py``) and how far AGTools satisfies it. Anything
#: marked ``not_provided`` must be served by the R3 Mock adapter until a runtime
#: provider exists.
SHARED_CAPABILITY_INTERFACE: Tuple[Dict[str, Any], ...] = (
    {
        "capability": "workspace.rag.search",
        "status": "provided",
        "provider": "document.search",
        "runtime": "R2.1 hybrid retrieval (SQLite FTS5/BM25 + vector index, reciprocal-rank fusion)",
        "note": "Scoped to a single parsed document. Cross-document workspace search is not provided by AGTools.",
    },
    {
        "capability": "office.docx.compare",
        "status": "not_provided",
        "provider": None,
        "runtime": None,
        "note": "AGTools converts DOCX to Markdown but has no comparison capability; the R3 Mock adapter must supply it.",
    },
    {
        "capability": "office.xlsx.analyze",
        "status": "not_provided",
        "provider": None,
        "runtime": None,
        "note": "Spreadsheets are not an accepted upload type (PDF/DOCX/PPTX only).",
    },
    {
        "capability": "office.pptx.generate",
        "status": "not_provided",
        "provider": None,
        "runtime": None,
        "note": "PPTX is parse-only; rendering generates raster slide images, not editable decks.",
    },
    {
        "capability": "workspace.file.write",
        "status": "not_provided",
        "provider": None,
        "runtime": None,
        "note": "docx.from_markdown writes a generated download file inside AGTools storage; it cannot write into a managed workspace.",
    },
)

#: Admin console surfaces are deliberately excluded from the R3 capability
#: manifest: they hold embedding/search configuration, are bound to the
#: administrator SSO and are not executable by an R3 planner. Any declared
#: route containing this marker is classified as an admin surface.
ADMIN_PATH_MARKER = "/settings/"

_DEFAULT_API_PREFIX = "/api/v1"
_API_V1_DIR = Path(__file__).resolve().parent / "api" / "v1"
_CONFIG_PATH = Path(__file__).resolve().parent / "config.py"

# --------------------------------------------------------------------------- #
# Catalog (route-true; verified against app/api/v1/*.py by check_route_coverage)
# --------------------------------------------------------------------------- #

_DOC_ID_IN = {"doc_id": {"type": "string", "description": "Parsed document id returned by document.parse."}}

_SOURCE_LOCATION_OUT = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["pdf", "docx", "pptx", "unknown"]},
        "page": {"type": ["integer", "null"]},
        "slide": {"type": ["integer", "null"]},
        "slide_start": {"type": ["integer", "null"]},
        "slide_end": {"type": ["integer", "null"]},
    },
    "required": ["type"],
}

_STATUS_OUT = {
    "type": "object",
    "properties": {
        "document_id": {"type": "string"},
        "status": {"type": "string", "enum": ["queued", "processing", "completed", "failed"]},
        "page_count": {"type": "integer"},
        "section_count": {"type": "integer"},
        "figure_count": {"type": "integer"},
        "error_message": {"type": ["string", "null"]},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
    },
    "required": ["document_id", "status"],
}

_HTTP_ERROR = "agtools.http_error/v1"
_PARSE_JOB_ERROR = "agtools.parse_job/v1"


def _spec(
    *,
    name: str,
    namespace: str,
    capability: str,
    permission: str,
    side_effect: str,
    timeout_seconds: float,
    error_contract: str,
    approval_required: bool,
    description: str,
    input_schema: Dict[str, Any],
    output_schema: Dict[str, Any],
    method: str,
    path: str,
    request_body: str,
    response_body: str,
    auth: str = "caller",
    document_scoped: bool = True,
    response_content_type: str = "application/json",
    aliases: Sequence[Tuple[str, str]] = (),
    path_params: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Build one catalog entry (the unified spec plus AGTools transport details)."""
    return {
        "spec": {
            "name": name,
            "namespace": namespace,
            "version": "1.0.0",
            "description": description,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "capability": capability,
            "permission": permission,
            "side_effect": side_effect,
            "timeout_seconds": timeout_seconds,
            "error_contract": copy.deepcopy(dict(ERROR_CONTRACTS[error_contract])),
            "approval_required": approval_required,
            "source": PROVIDER,
        },
        "binding": {
            "name": name,
            "method": method,
            "path": path,
            "request_body": request_body,
            "response_body": response_body,
            "response_content_type": response_content_type,
            "auth": auth,
            "document_scoped": document_scoped,
            "aliases": [{"method": m, "path": p} for m, p in aliases],
            "path_params": dict(path_params or {}),
            "description": description,
        },
    }


_CATALOG: Tuple[Dict[str, Any], ...] = (
    _spec(
        name="document.parse",
        namespace="agtools",
        capability="document.ingest.parse",
        permission="write",
        side_effect="write",
        timeout_seconds=120,
        error_contract=_PARSE_JOB_ERROR,
        approval_required=False,
        description=(
            "Upload a PDF, DOCX or PPTX (max 100MB, 500 pages) and start asynchronous parsing. "
            "Returns 202 with a document_id; parsing progress is read through document.status."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "format": "binary", "description": "Document bytes (multipart/form-data)."},
            },
            "required": ["file"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "filename": {"type": "string"},
                "status": {"type": "string", "enum": ["queued", "processing", "completed", "failed"]},
                "message": {"type": "string"},
            },
            "required": ["document_id", "filename", "status"],
        },
        method="POST",
        path="/api/v1/documents",
        request_body="multipart/form-data",
        response_body="json",
        document_scoped=False,
    ),
    _spec(
        name="document.get",
        namespace="agtools",
        capability="document.metadata.read",
        permission="read",
        side_effect="read",
        timeout_seconds=10,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Read parsed document metadata, status and page/section/figure counts.",
        input_schema={"type": "object", "properties": dict(_DOC_ID_IN), "required": ["doc_id"]},
        output_schema=_STATUS_OUT,
        method="GET",
        path="/api/v1/documents/{id}",
        request_body="none",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="document.status",
        namespace="agtools",
        capability="document.job.status",
        permission="read",
        side_effect="read",
        timeout_seconds=10,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Poll the asynchronous parse job of a document (queued, processing, completed, failed).",
        input_schema={"type": "object", "properties": dict(_DOC_ID_IN), "required": ["doc_id"]},
        output_schema=_STATUS_OUT,
        method="GET",
        path="/api/v1/documents/{id}/status",
        request_body="none",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="document.get_outline",
        namespace="agtools",
        capability="document.structure.outline",
        permission="read",
        side_effect="read",
        timeout_seconds=10,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Read the hierarchical section outline (chapter to subsection) of a parsed document.",
        input_schema={
            "type": "object",
            "properties": {
                **_DOC_ID_IN,
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["doc_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "status": {"type": "string"},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section_id": {"type": "string"},
                            "parent_id": {"type": ["string", "null"]},
                            "title": {"type": "string"},
                            "level": {"type": "integer"},
                            "source": _SOURCE_LOCATION_OUT,
                        },
                        "required": ["section_id", "title"],
                    },
                },
            },
            "required": ["document_id", "sections"],
        },
        method="GET",
        path="/api/v1/documents/{id}/outline",
        request_body="none",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="document.get_text",
        namespace="agtools",
        capability="document.text.read",
        permission="read",
        side_effect="read",
        timeout_seconds=20,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Read the complete parsed body of a document as Markdown. Prefer outline/chunk reads for large documents.",
        input_schema={"type": "object", "properties": dict(_DOC_ID_IN), "required": ["doc_id"]},
        output_schema={
            "type": "object",
            "properties": {"document_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["document_id", "text"],
        },
        method="GET",
        path="/api/v1/documents/{id}/text",
        request_body="none",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="document.read_section",
        namespace="agtools",
        capability="document.structure.section",
        permission="read",
        side_effect="read",
        timeout_seconds=10,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Read one section body together with its linked figure ids and source page/slide.",
        input_schema={
            "type": "object",
            "properties": {**_DOC_ID_IN, "section_id": {"type": "string"}},
            "required": ["doc_id", "section_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "section_id": {"type": "string"},
                "parent_id": {"type": ["string", "null"]},
                "title": {"type": "string"},
                "level": {"type": "integer"},
                "content": {"type": "string"},
                "figure_ids": {"type": "array", "items": {"type": "string"}},
                "table_count": {"type": "integer"},
                "source": _SOURCE_LOCATION_OUT,
            },
            "required": ["section_id", "title", "content"],
        },
        method="GET",
        path="/api/v1/documents/{id}/sections/{section_id}",
        request_body="none",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="document.list_chunks",
        namespace="agtools",
        capability="document.chunk.list",
        permission="read",
        side_effect="read",
        timeout_seconds=15,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Enumerate the token-bounded chunks of a document in parser order, with a stable snapshot cursor.",
        input_schema={
            "type": "object",
            "properties": {
                **_DOC_ID_IN,
                "cursor": {"type": "string", "description": "next_cursor from the previous page."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            "required": ["doc_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "snapshot_id": {"type": "string"},
                "parser_version": {"type": "string"},
                "total_chunks": {"type": "integer"},
                "chunks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "chunk_id": {"type": "string"},
                            "section_id": {"type": ["string", "null"]},
                            "source": _SOURCE_LOCATION_OUT,
                            "token_count": {"type": "integer"},
                            "prompt_units": {"type": "integer"},
                            "content_hash": {"type": "string"},
                        },
                        "required": ["chunk_id", "token_count"],
                    },
                },
                "next_cursor": {"type": ["string", "null"]},
            },
            "required": ["document_id", "snapshot_id", "total_chunks", "chunks"],
        },
        method="GET",
        path="/api/v1/documents/{id}/chunks",
        request_body="none",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="document.read_chunk",
        namespace="agtools",
        capability="document.chunk.read",
        permission="read",
        side_effect="read",
        timeout_seconds=10,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Read the full text of one chunk, with its section, figure ids and source location.",
        input_schema={
            "type": "object",
            "properties": {**_DOC_ID_IN, "chunk_id": {"type": "string"}},
            "required": ["doc_id", "chunk_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string"},
                "document_id": {"type": "string"},
                "parent_section_id": {"type": ["string", "null"]},
                "content": {"type": "string"},
                "figure_ids": {"type": "array", "items": {"type": "string"}},
                "source": _SOURCE_LOCATION_OUT,
                "token_count": {"type": "integer"},
            },
            "required": ["chunk_id", "content"],
        },
        method="GET",
        path="/api/v1/documents/{id}/chunks/{chunk_id}",
        request_body="none",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="document.get_figure",
        namespace="agtools",
        capability="document.figure.read",
        permission="read",
        side_effect="read",
        timeout_seconds=10,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Read metadata, caption and page/slide reference of one figure, chart or rendered slide.",
        input_schema={
            "type": "object",
            "properties": {**_DOC_ID_IN, "figure_id": {"type": "string"}},
            "required": ["doc_id", "figure_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "figure_id": {"type": "string"},
                "document_id": {"type": "string"},
                "page_or_slide": {"type": "integer"},
                "asset_id": {"type": "string"},
                "caption": {"type": ["string", "null"]},
                "visual_description": {"type": ["string", "null"]},
                "analysis_status": {
                    "type": "string",
                    "enum": ["not_requested", "processing", "completed", "failed"],
                },
                "source_type": {
                    "type": "string",
                    "enum": ["embedded_image", "slide_render", "page_render", "chart"],
                },
            },
            "required": ["figure_id", "asset_id", "page_or_slide"],
        },
        method="GET",
        path="/api/v1/documents/{id}/figures/{figure_id}",
        request_body="none",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="document.get_figure_asset",
        namespace="agtools",
        capability="document.asset.read",
        permission="read",
        side_effect="read",
        timeout_seconds=30,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Stream the original binary of a figure or the high-resolution render of a page/slide.",
        input_schema={
            "type": "object",
            "properties": {**_DOC_ID_IN, "figure_id": {"type": "string"}},
            "required": ["doc_id", "figure_id"],
        },
        output_schema={"type": "string", "format": "binary", "description": "Image bytes (image/png, image/jpeg, ...)."},
        method="GET",
        path="/api/v1/documents/{id}/figures/{figure_id}/asset",
        request_body="none",
        response_body="binary",
        response_content_type="image/*",
        aliases=(("GET", "/api/v1/documents/{id}/assets/{asset_id}"),),
        path_params={"doc_id": "id", "figure_id": "figure_id"},
    ),
    _spec(
        name="document.get_manifest",
        namespace="agtools",
        capability="document.manifest.read",
        permission="read",
        side_effect="read",
        timeout_seconds=15,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Read the document manifest: asset catalogue with checksums and parser metadata.",
        input_schema={"type": "object", "properties": dict(_DOC_ID_IN), "required": ["doc_id"]},
        output_schema={"type": "object", "description": "Manifest document; keys are parser-version specific."},
        method="GET",
        path="/api/v1/documents/{id}/manifest",
        request_body="none",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="document.search",
        namespace="agtools",
        capability="workspace.rag.search",
        permission="read",
        side_effect="read",
        timeout_seconds=30,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description=(
            "Hybrid retrieval inside one parsed document: SQLite FTS5/BM25 keyword search fused with a "
            "vector index by reciprocal rank, returning snippets with source page/slide and figure ids."
        ),
        input_schema={
            "type": "object",
            "properties": {
                **_DOC_ID_IN,
                "query": {"type": "string", "minLength": 1, "maxLength": 4000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "token_budget": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 262144,
                    "description": "Optional context budget; snippet length is allocated from it.",
                },
            },
            "required": ["doc_id", "query"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "query": {"type": "string"},
                "total_matches": {"type": "integer"},
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "chunk_id": {"type": "string"},
                            "section_id": {"type": ["string", "null"]},
                            "snippet": {"type": "string"},
                            "score": {"type": "number"},
                            "source": _SOURCE_LOCATION_OUT,
                            "figure_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["chunk_id", "snippet"],
                    },
                },
            },
            "required": ["document_id", "query", "results"],
        },
        method="POST",
        path="/api/v1/documents/{id}/search",
        request_body="json",
        response_body="json",
        path_params={"doc_id": "id"},
    ),
    _spec(
        name="docx.to_markdown",
        namespace="agtools",
        capability="document.convert.docx_to_markdown",
        permission="read",
        side_effect="read",
        timeout_seconds=25,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description=(
            "Convert an already uploaded DOCX document into clean Markdown, preserving headings, tables "
            "and figure references. Read-only: it writes nothing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                **_DOC_ID_IN,
                "table_mode": {"type": "string", "enum": ["auto", "gfm", "html"], "default": "auto"},
                "image_mode": {"type": "string", "enum": ["assets", "base64", "none"], "default": "none"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 100000, "default": 20000},
            },
            "required": ["doc_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "filename": {"type": "string"},
                "markdown": {"type": "string"},
                "chars": {"type": "integer"},
                "total_chars": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "stats": {"type": "object"},
                "sections": {"type": "array", "items": {"type": "object"}},
                "meta": {"type": "object"},
            },
            "required": ["doc_id", "markdown", "truncated"],
        },
        method="POST",
        path="/api/v1/tools/docx-to-md/by-doc",
        request_body="json",
        response_body="json",
        path_params={"doc_id": "doc_id (body field)"},
    ),
    _spec(
        name="docx.to_markdown_upload",
        namespace="agtools",
        capability="document.convert.docx_to_markdown_inline",
        permission="read",
        side_effect="read",
        timeout_seconds=60,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description=(
            "Convert an uploaded DOCX file straight to Markdown in one call, without storing a document. "
            "Separate capability from docx.to_markdown because the result has no document id, so its "
            "figures and chunks cannot be referenced later."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "format": "binary"},
                "table_mode": {"type": "string", "enum": ["auto", "gfm", "html"], "default": "auto"},
                "image_mode": {"type": "string", "enum": ["assets", "base64", "none"], "default": "assets"},
                "response_format": {"type": "string", "enum": ["json", "zip"], "default": "json"},
                "include_previews": {"type": "boolean", "default": False},
            },
            "required": ["file"],
        },
        output_schema={
            "description": "Markdown conversion report (json) or a zip archive of Markdown plus extracted assets.",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "markdown": {"type": "string"},
                        "stats": {"type": "object"},
                        "sections": {"type": "array", "items": {"type": "object"}},
                        "figures": {"type": "array", "items": {"type": "object"}},
                    },
                },
                {"type": "string", "format": "binary"},
            ],
        },
        method="POST",
        path="/api/v1/tools/docx-to-md",
        request_body="multipart/form-data",
        response_body="json_or_zip",
        document_scoped=False,
    ),
    _spec(
        name="docx.from_markdown",
        namespace="agtools",
        capability="document.convert.markdown_to_docx",
        permission="write",
        side_effect="write",
        timeout_seconds=30,
        error_contract=_HTTP_ERROR,
        approval_required=True,
        description=(
            "Render Markdown (headings, tables, lists, code blocks) into a Word .docx. When return_format is "
            "'json' the file is kept for download for 24h and a markdown_link is returned. This is the only "
            "AGTools capability that persists a file, so an R3 workflow must obtain human approval first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "markdown": {"type": "string", "minLength": 1, "maxLength": 400000},
                "filename": {"type": "string", "maxLength": 160, "default": "document.docx"},
                "title": {"type": ["string", "null"], "maxLength": 200},
                "return_format": {"type": "string", "enum": ["json", "docx"], "default": "json"},
            },
            "required": ["markdown"],
        },
        output_schema={
            "description": "JSON descriptor (return_format=json) or the .docx binary (return_format=docx).",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "filename": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "download_path": {"type": "string"},
                        "download_url": {"type": "string"},
                        "markdown_link": {"type": "string"},
                        "stats": {"type": "object"},
                        "meta": {"type": "object"},
                    },
                    "required": ["file_id", "filename", "download_path"],
                },
                {"type": "string", "format": "binary"},
            ],
        },
        method="POST",
        path="/api/v1/tools/md-to-docx",
        request_body="json",
        response_body="json_or_docx",
        document_scoped=False,
    ),
    _spec(
        name="docx.download",
        namespace="agtools",
        capability="document.generated_file.read",
        permission="read",
        side_effect="read",
        timeout_seconds=15,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description="Download a generated .docx produced by docx.from_markdown while it is still retained (24h TTL).",
        input_schema={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
        output_schema={"type": "string", "format": "binary", "description": ".docx bytes"},
        method="GET",
        path="/api/v1/tools/md-to-docx/{file_id}",
        request_body="none",
        response_body="binary",
        response_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        document_scoped=False,
    ),
    _spec(
        name="web.search",
        namespace="agtools",
        capability="web.search",
        permission="write",
        side_effect="external",
        timeout_seconds=20,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description=(
            "Search the live web (SearXNG and fallbacks) for current facts, news and external references. "
            "Results are cached per tenant/owner; the snippet size follows the context budget when given."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "lang": {"type": "string", "maxLength": 16, "default": "auto"},
                "category": {"type": "string", "enum": ["general", "news"], "default": "general"},
                "time_range": {"type": "string", "enum": ["all", "day", "week", "month"], "default": "all"},
                "max_snippet_chars": {"type": "integer", "minimum": 100, "maximum": 3000, "default": 800},
                "bypass_cache": {"type": "boolean", "default": False},
                "token_budget": {"type": "integer", "minimum": 64, "maximum": 131072},
            },
            "required": ["query"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "provider": {"type": "string"},
                "total_results": {"type": "integer"},
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "snippet": {"type": "string"},
                        },
                        "required": ["title", "url"],
                    },
                },
                "cached": {"type": "boolean"},
            },
            "required": ["results"],
        },
        method="POST",
        path="/api/v1/web/search",
        request_body="json",
        response_body="json",
        document_scoped=False,
    ),
    _spec(
        name="web.fetch",
        namespace="agtools",
        capability="web.fetch",
        permission="write",
        side_effect="external",
        timeout_seconds=30,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description=(
            "Fetch one public URL (SSRF-guarded, 1MB ceiling) and return its readable text. Short client-side "
            "rendered pages fall back to a bounded headless Chromium render."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 3, "maxLength": 500},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 100000, "default": 8000},
            },
            "required": ["url"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "status_code": {"type": "integer"},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "truncated": {"type": "boolean"},
                "rendered_js": {"type": "boolean"},
                "meta": {"type": "object"},
            },
            "required": ["url", "content"],
        },
        method="POST",
        path="/api/v1/web/fetch",
        request_body="json",
        response_body="json",
        document_scoped=False,
    ),
    _spec(
        name="tools.manifest",
        namespace="agtools",
        capability="registry.tool_manifest.read",
        permission="read",
        side_effect="read",
        timeout_seconds=10,
        error_contract=_HTTP_ERROR,
        approval_required=False,
        description=(
            "Read the AGCore v1 declarative tool manifest published by AGTools (per-tool ON/OFF state). "
            "Use it to cross-check which AGTools tools the platform currently advertises."
        ),
        input_schema={"type": "object", "properties": {}},
        output_schema={
            "type": "object",
            "properties": {
                "schema": {"type": "integer"},
                "provider": {"type": "string"},
                "version": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["schema", "provider", "tools"],
        },
        method="GET",
        path="/api/v1/tools/manifest",
        request_body="none",
        response_body="json",
        document_scoped=False,
    ),
)


# --------------------------------------------------------------------------- #
# Public manifest
# --------------------------------------------------------------------------- #


def list_r3_capabilities() -> List[Dict[str, Any]]:
    """Return the WP-07 ``UnifiedToolSpec`` entries for AGTools.

    Each call returns fresh deep copies; mutating the result never changes the
    catalog, so a caller cannot corrupt the manifest of another request.
    """
    return [copy.deepcopy(entry["spec"]) for entry in _CATALOG]


def list_r3_capability_bindings() -> List[Dict[str, Any]]:
    """Return the AGTools transport binding of every capability."""
    return [copy.deepcopy(entry["binding"]) for entry in _CATALOG]


def get_r3_capability(name: str) -> Dict[str, Any]:
    """Return one capability by name. Raises ``KeyError`` when it is unknown."""
    for entry in _CATALOG:
        if entry["spec"]["name"] == name:
            return copy.deepcopy(entry["spec"])
    raise KeyError(f"unknown R3 capability: {name}")


def find_capabilities(
    *,
    namespace: Optional[str] = None,
    capability: Optional[str] = None,
    side_effect: Optional[str] = None,
    permission: Optional[str] = None,
    approval_required: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Filter capabilities on the WP-07 dimensions used by the registry."""
    out: List[Dict[str, Any]] = []
    for entry in _CATALOG:
        spec = entry["spec"]
        if namespace is not None and spec["namespace"] != namespace:
            continue
        if capability is not None and spec["capability"] != capability:
            continue
        if side_effect is not None and spec["side_effect"] != side_effect:
            continue
        if permission is not None and spec["permission"] != permission:
            continue
        if approval_required is not None and spec["approval_required"] != approval_required:
            continue
        out.append(copy.deepcopy(spec))
    return out


def capability_coverage() -> List[Dict[str, Any]]:
    """Return the R3 shared-capability interface coverage of this service."""
    return [dict(item) for item in SHARED_CAPABILITY_INTERFACE]


def capability_manifest() -> Dict[str, Any]:
    """Return the complete, JSON-serializable AGTools capability manifest."""
    return {
        "schema": MANIFEST_SCHEMA,
        "contract": R3_CAPABILITY_CONTRACT,
        "provider": PROVIDER,
        "service": {
            "id": SERVICE_ID,
            "version": SERVICE_VERSION,
            "runtime": "R2.1 document intelligence service (parsers / writers / hybrid RAG)",
        },
        "spec_fields": list(UNIFIED_TOOL_SPEC_FIELDS),
        "permissions": dict(PERMISSIONS),
        "side_effects": dict(SIDE_EFFECTS),
        "error_contracts": copy.deepcopy(dict(ERROR_CONTRACTS)),
        "shared_capability_interface": capability_coverage(),
        "admin_surfaces_excluded": {"marker": ADMIN_PATH_MARKER, "reason": "administrator SSO console, not planable"},
        "capabilities": list_r3_capabilities(),
        "bindings": list_r3_capability_bindings(),
    }


# --------------------------------------------------------------------------- #
# Route-truth verification (stdlib AST only — the FastAPI app is never imported)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RouteCoverage:
    """Result of comparing the manifest with the routes declared in this service."""

    #: ``"METHOD /path"`` entries claimed by the manifest but missing in source.
    missing: Tuple[str, ...]
    #: Declared non-admin routes that the manifest does not describe.
    uncovered: Tuple[str, ...]
    #: Declared administrator routes, excluded from the manifest on purpose.
    admin_excluded: Tuple[str, ...]
    #: Manifest capabilities whose ``(method, path)`` appears more than once.
    duplicates: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.missing or self.uncovered or self.duplicates)


def collect_api_prefix(config_path: Optional[Path] = None) -> str:
    """Read ``API_V1_STR`` from ``app/config.py`` without importing the app."""
    path = Path(config_path) if config_path else _CONFIG_PATH
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return _DEFAULT_API_PREFIX
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "API_V1_STR":
            value = node.value
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "API_V1_STR" not in names:
                continue
            value = node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return _DEFAULT_API_PREFIX


def _module_prefix(tree: ast.AST) -> str:
    """Return the ``APIRouter(prefix=...)`` of a module (empty when absent)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "router" for t in node.targets):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
    return ""


def _join_paths(*parts: str) -> str:
    joined = "/".join(part.strip("/") for part in parts if part and part.strip("/"))
    return "/" + joined if joined else "/"


def collect_declared_routes(
    api_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """Return every route declared by ``app/api/v1/*.py`` as method/path/module.

    Parsing the decorators with :mod:`ast` keeps this check honest without
    importing FastAPI, the parsers or any service dependency.
    """
    directory = Path(api_dir) if api_dir else _API_V1_DIR
    prefix = collect_api_prefix(config_path)
    routes: List[Dict[str, str]] = []
    for module_path in sorted(directory.glob("*.py")):
        if module_path.name.startswith("__"):
            continue
        try:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        module_prefix = _module_prefix(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                    continue
                if func.value.id != "router":
                    continue
                method = func.attr.upper()
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                relative = ""
                if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                    relative = decorator.args[0].value
                routes.append(
                    {
                        "method": method,
                        "path": _join_paths(prefix, module_prefix, relative),
                        "module": f"app/api/v1/{module_path.name}",
                        "handler": node.name,
                    }
                )
    return routes


def check_route_coverage(
    routes: Optional[Sequence[Mapping[str, str]]] = None,
) -> RouteCoverage:
    """Verify the manifest against the routes actually declared by this service."""
    declared = list(routes) if routes is not None else collect_declared_routes()
    declared_keys = {f"{r['method'].upper()} {r['path']}" for r in declared}

    claimed: List[str] = []
    for binding in list_r3_capability_bindings():
        claimed.append(f"{binding['method']} {binding['path']}")
        for alias in binding["aliases"]:
            claimed.append(f"{alias['method']} {alias['path']}")

    missing = tuple(sorted(key for key in claimed if key not in declared_keys))
    duplicates = tuple(sorted({key for key in claimed if claimed.count(key) > 1}))
    admin_excluded = tuple(sorted(key for key in declared_keys if ADMIN_PATH_MARKER in key.split(" ", 1)[1]))
    uncovered = tuple(
        sorted(key for key in declared_keys if key not in set(claimed) and ADMIN_PATH_MARKER not in key.split(" ", 1)[1])
    )
    return RouteCoverage(missing=missing, uncovered=uncovered, admin_excluded=admin_excluded, duplicates=duplicates)
