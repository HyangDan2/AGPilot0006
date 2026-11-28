"""Knowledge service: revisions, optimistic concurrency, rollback, search and integrity."""

from __future__ import annotations

import json

import pytest

from app.knowledge.contracts import (
    ContentBlock,
    CreateRequest,
    KnowledgeError,
    KnowledgeErrorCode,
    OpKind,
    ReadRequest,
    SearchMode,
    SearchRequest,
    SearchFilters,
    UpdateOperation,
    UpdateRequest,
    ValidateRequest,
    compute_content_hash,
)
from app.knowledge.service import KnowledgeService
from app.knowledge.store import KnowledgeStore
from tests.knowledge_fixtures import make_docx, make_pdf

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture()
def service(tmp_path):
    return KnowledgeService(store=KnowledgeStore(tmp_path / "store"))


def test_create_read_update_validate_and_rollback(service):
    created = service.create(
        CreateRequest(
            name="report.docx",
            mime_type=DOCX,
            blocks=[
                ContentBlock(kind="heading", text="Quarterly Report", level=1),
                ContentBlock(kind="paragraph", text="Revenue grew by 12 percent."),
            ],
            metadata={"owner_id": "u1"},
        )
    )
    ref = created.document
    assert created.created is True and ref.revision == 1

    read = service.read(ReadRequest(document_id=ref.document_id))
    assert any("Revenue grew" in node.text for node in read.nodes)
    assert read.citations and read.citations[0].document.document_id == ref.document_id

    updated = service.update(
        UpdateRequest(
            document_id=ref.document_id,
            expected_revision=1,
            operations=[UpdateOperation(op=OpKind.REPLACE_TEXT, find="12 percent", replace="15 percent")],
        )
    )
    assert updated.document.revision == 2
    assert updated.previous_revision == 1
    after = service.read(ReadRequest(document_id=ref.document_id))
    assert any("15 percent" in node.text for node in after.nodes)

    validated = service.validate(ValidateRequest(document_id=ref.document_id))
    assert validated.ok is True
    assert {check.name for check in validated.checks} >= {"content_hash", "size", "office_archive", "parse_outline"}

    restored = service.rollback(ref.document_id, 1, expected_revision=2)
    assert restored.revision == 3
    current = service.read(ReadRequest(document_id=ref.document_id))
    assert any("12 percent" in node.text for node in current.nodes)


def test_second_writer_with_stale_revision_is_rejected(service):
    created = service.create(
        CreateRequest(name="a.docx", mime_type=DOCX, blocks=[ContentBlock(kind="paragraph", text="v1")])
    )
    document_id = created.document.document_id
    service.update(
        UpdateRequest(
            document_id=document_id,
            expected_revision=1,
            operations=[UpdateOperation(op=OpKind.REPLACE_TEXT, find="v1", replace="v2")],
        )
    )
    with pytest.raises(KnowledgeError) as excinfo:
        service.update(
            UpdateRequest(
                document_id=document_id,
                expected_revision=1,
                operations=[UpdateOperation(op=OpKind.REPLACE_TEXT, find="v2", replace="v3")],
            )
        )
    assert excinfo.value.body.code is KnowledgeErrorCode.REVISION_CONFLICT
    assert excinfo.value.body.retryable is True
    assert service.store.ref(document_id).revision == 2


def test_dry_run_reports_without_writing_a_revision(service):
    created = service.create(
        CreateRequest(name="a.docx", mime_type=DOCX, blocks=[ContentBlock(kind="paragraph", text="keep me")])
    )
    result = service.update(
        UpdateRequest(
            document_id=created.document.document_id,
            operations=[UpdateOperation(op=OpKind.REPLACE_TEXT, find="keep", replace="drop")],
            dry_run=True,
        )
    )
    assert result.dry_run is True and result.applied
    assert service.store.ref(created.document.document_id).revision == 1


def test_index_is_reused_for_same_revision(service):
    created = service.create(
        CreateRequest(name="a.docx", mime_type=DOCX, blocks=[ContentBlock(kind="paragraph", text="alpha beta")])
    )
    assert service.index_document(created.document) == 0  # already indexed at create time
    ref = service.store.ref(created.document.document_id)
    assert service.store.index_is_ready(ref.document_id, ref.revision)


def test_search_filters_deduplicates_and_degrades_explicitly(service, tmp_path):
    docx_path = make_docx(tmp_path / "sample.docx")
    first = service.store.create(
        name="sample.docx", mime_type=DOCX, data=docx_path.read_bytes(), metadata={"owner_id": "u1", "team": "alpha"}
    )
    service.index_document(first, force=True)
    duplicate = service.store.create(
        name="copy.docx", mime_type=DOCX, data=docx_path.read_bytes(), metadata={"owner_id": "u1", "team": "alpha"}
    )
    service.index_document(duplicate, force=True)

    response = service.search(SearchRequest(query="structured citations", mode=SearchMode.HYBRID))
    assert response.mode_effective is SearchMode.KEYWORD
    assert response.degraded is True
    assert response.degradation_reason == "embedding_unavailable"
    assert response.results
    assert response.duplicates_removed >= 1
    assert all(hit.citation for hit in response.results)

    filtered = service.search(
        SearchRequest(query="structured citations", filters=SearchFilters(mime_types=["application/pdf"]))
    )
    assert filtered.results == []

    by_meta = service.search(
        SearchRequest(query="structured citations", filters=SearchFilters(metadata={"team": "beta"}))
    )
    assert by_meta.results == []


def test_semantic_search_enables_hybrid(service, tmp_path):
    docx_path = make_docx(tmp_path / "sample.docx")
    ref = service.store.create(name="sample.docx", mime_type=DOCX, data=docx_path.read_bytes())
    service.index_document(ref, force=True)
    meta = service.store._load_meta(ref.document_id)
    chunk_rows = service.store.search_chunks("structured", top_k=5)
    assert chunk_rows
    chunk_id = chunk_rows[0]["chunk_id"]

    service.set_embedder(lambda text: [1.0, 0.0], lambda query, top_k: [(ref.document_id, chunk_id, 0.9)])
    response = service.search(SearchRequest(query="structured citations", mode=SearchMode.HYBRID))
    assert response.degraded is False
    assert response.mode_effective is SearchMode.HYBRID
    assert any("vector" in hit.matched_by for hit in response.results)
    assert response.provider_status.embedding == "ok"


def test_tampered_revision_fails_integrity(service, tmp_path):
    docx_path = make_docx(tmp_path / "sample.docx")
    ref = service.store.create(name="sample.docx", mime_type=DOCX, data=docx_path.read_bytes())
    path = service.store.revision_path(ref.document_id, ref.revision, ref.name)
    path.write_bytes(path.read_bytes() + b"tamper")
    result = service.validate(ValidateRequest(document_id=ref.document_id))
    assert result.ok is False
    assert any(check.name == "content_hash" and not check.ok for check in result.checks)
    assert result.errors and result.errors[0].code is KnowledgeErrorCode.INTEGRITY_MISMATCH


def test_unknown_document_and_format_errors(service, tmp_path):
    with pytest.raises(KnowledgeError) as missing:
        service.read(ReadRequest(document_id="kd_missing00001"))
    assert missing.value.body.code is KnowledgeErrorCode.DOCUMENT_NOT_FOUND

    xml = b"<root/>"
    ref = service.store.create(name="data.xml", mime_type="application/xml", data=xml)
    with pytest.raises(KnowledgeError) as unsupported:
        service.read(ReadRequest(document_id=ref.document_id))
    assert unsupported.value.body.code is KnowledgeErrorCode.UNSUPPORTED_FORMAT


def test_manifest_reports_both_sides_of_the_contract(service):
    manifest = service.manifest()
    docx_cap = next(item for item in manifest.formats if item.family.value == "docx")
    assert docx_cap.write.edit and docx_cap.write.preserve_styles
    pdf_cap = next(item for item in manifest.formats if item.family.value == "pdf")
    assert pdf_cap.read.page_range and pdf_cap.write.create is False


def test_search_hits_carry_page_locators_and_hashes(service, tmp_path):
    pdf_path = make_pdf(tmp_path / "sample.pdf", pages=2)
    ref = service.store.create(name="sample.pdf", mime_type="application/pdf", data=pdf_path.read_bytes())
    service.index_document(ref, force=True)
    response = service.search(SearchRequest(query="January"))
    assert response.results
    hit = response.results[0]
    assert hit.locator.page == 1
    assert hit.citation and hit.citation.locator.page == 1
    assert hit.document.content_hash == compute_content_hash(pdf_path.read_bytes())
