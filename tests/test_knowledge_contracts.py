"""dk/v1 contract surface: identity, hashing, locators, errors, manifest."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.knowledge.contracts import (
    CONTRACT_VERSION,
    Citation,
    DocumentRef,
    KnowledgeError,
    KnowledgeErrorCode,
    Locator,
    LocatorKind,
    ReadRequest,
    SearchRequest,
    compute_content_hash,
    format_family,
    normalize_mime,
)
from app.knowledge.service import KnowledgeService
from app.knowledge.store import KnowledgeStore

AUTH = {"Authorization": "Bearer hd2-doc-secret-token-key-2026"}


def test_mime_normalization_prefers_real_format_then_extension():
    assert normalize_mime("application/pdf; charset=binary") == "application/pdf"
    assert normalize_mime(None, filename="report.PDF") == "application/pdf"
    assert normalize_mime("application/zip", filename="deck.pptx").endswith("presentationml.presentation")
    assert normalize_mime("application/zip") == "application/octet-stream"
    assert format_family("text/markdown").value == "text"
    assert format_family("application/octet-stream").value == "unknown"


def test_document_ref_identity_and_hash_shape():
    digest = compute_content_hash(b"hello")
    ref = DocumentRef(document_id="kd_test1234", revision=3, content_hash=digest)
    assert ref.canonical_id == "agtools:kd_test1234@3"
    assert ref.document_key == "agtools:kd_test1234"
    assert ref.family.value == "unknown"
    with pytest.raises(ValidationError):
        DocumentRef(document_id="kd_test1234", content_hash="sha256:xyz")


def test_locator_requires_coordinates_for_typed_kinds():
    with pytest.raises(ValidationError):
        Locator(kind=LocatorKind.CELL, sheet="Data")
    cell = Locator(kind=LocatorKind.CELL, sheet="Data", cell="B3")
    assert cell.label() == "Data!B3"
    page = Locator(kind=LocatorKind.PAGE, page=7)
    assert page.label() == "p.7"


def test_read_request_mode_coordinates_are_enforced():
    with pytest.raises(ValidationError):
        ReadRequest(document_id="kd_x", mode="pages")
    request = ReadRequest(document_id="kd_x", mode="pages", pages=[1, 2])
    assert request.pages == [1, 2]


def test_extra_fields_are_rejected_on_wire_models():
    with pytest.raises(ValidationError):
        SearchRequest(query="x", unknown_field=True)  # type: ignore[call-arg]


def test_error_status_table_matches_contract():
    assert KnowledgeError(KnowledgeErrorCode.REVISION_CONFLICT).status_code == 409
    assert KnowledgeError(KnowledgeErrorCode.ENCRYPTED_DOCUMENT).status_code == 422
    assert KnowledgeError(KnowledgeErrorCode.UNSUPPORTED_FORMAT).status_code == 415
    body = KnowledgeError(KnowledgeErrorCode.DOCUMENT_NOT_FOUND, "missing").body
    assert body.code is KnowledgeErrorCode.DOCUMENT_NOT_FOUND
    assert body.retryable is False


def test_manifest_declares_every_contract_obligation(tmp_path):
    service = KnowledgeService(store=KnowledgeStore(tmp_path / "store"))
    manifest = service.manifest()
    payload = manifest.model_dump(mode="json")
    assert payload["contract_version"] == CONTRACT_VERSION
    families = {item["family"] for item in payload["formats"]}
    assert {"pdf", "docx", "xlsx", "pptx", "text", "csv", "image"} <= families
    assert payload["search"]["keyword_fallback"] is True
    assert payload["artifacts"]["optimistic_concurrency"] is True
    assert "revision_conflict" in payload["error_codes"]
    assert payload["vision"]["model"] is None  # never hard-coded
    json.dumps(payload)


def test_manifest_endpoint_served_with_auth():
    from app.main import app

    client = TestClient(app)
    unauthorized = client.get("/api/v1/knowledge/manifest")
    assert unauthorized.status_code == 401
    response = client.get("/api/v1/knowledge/manifest", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["contract_version"] == CONTRACT_VERSION
