"""WP-03 PDF engine: page citations, partial reads, tables/images, encryption, corruption."""

from __future__ import annotations

import pytest

from app.knowledge.contracts import (
    DocumentRef,
    KnowledgeError,
    KnowledgeErrorCode,
    LocatorKind,
    ReadMode,
    ReadRequest,
)
from app.knowledge.engines import pdf_engine
from tests.knowledge_fixtures import make_encrypted_pdf, make_pdf


def _ref(mime: str = "application/pdf") -> DocumentRef:
    return DocumentRef(document_id="kd_pdftest0001", name="sample.pdf", mime_type=mime, revision=1)


def test_reads_every_page_with_page_citations(tmp_path):
    path = make_pdf(tmp_path / "sample.pdf", pages=3)
    response = pdf_engine.read_path(path, _ref(), ReadRequest(document_id="kd_pdftest0001"))
    assert response.stats.pages == 3
    assert {citation.locator.page for citation in response.citations} == {1, 2, 3}
    assert all(citation.locator.kind is LocatorKind.PAGE for citation in response.citations)
    texts = "\n".join(node.text for node in response.nodes)
    assert "Page 2 heading" in texts and "January" in texts
    # Every node carries the page it came from, which is what AGCore turns into a citation.
    assert all(node.locator.page for node in response.nodes if node.kind.value in ("heading", "paragraph"))


def test_partial_read_returns_page_window_and_cursor(tmp_path):
    path = make_pdf(tmp_path / "sample.pdf", pages=4)
    response = pdf_engine.read_path(
        path,
        _ref(),
        ReadRequest(document_id="kd_pdftest0001", mode=ReadMode.PAGES, pages=[2, 3]),
    )
    assert {citation.locator.page for citation in response.citations} == {2, 3}
    assert response.truncated is False
    assert response.next_cursor is None


def test_max_chars_truncates_and_offers_next_cursor(tmp_path):
    path = make_pdf(tmp_path / "sample.pdf", pages=5)
    response = pdf_engine.read_path(
        path,
        _ref(),
        ReadRequest(document_id="kd_pdftest0001", max_chars=256),
    )
    assert response.truncated is True
    assert response.next_cursor and response.next_cursor.startswith("page:")
    assert len(response.nodes) > 0


def test_outline_uses_pages_when_no_bookmarks(tmp_path):
    path = make_pdf(tmp_path / "sample.pdf", pages=2)
    response = pdf_engine.read_path(
        path, _ref(), ReadRequest(document_id="kd_pdftest0001", mode=ReadMode.OUTLINE)
    )
    assert [node.locator.page for node in response.nodes] == [1, 2]


def test_encrypted_pdf_is_rejected_with_contract_error(tmp_path):
    path = make_encrypted_pdf(tmp_path / "locked.pdf")
    with pytest.raises(KnowledgeError) as excinfo:
        pdf_engine.read_path(path, _ref(), ReadRequest(document_id="kd_pdftest0001"))
    assert excinfo.value.body.code is KnowledgeErrorCode.ENCRYPTED_DOCUMENT


def test_corrupt_pdf_is_rejected_with_contract_error(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7 not really a pdf")
    with pytest.raises(KnowledgeError) as excinfo:
        pdf_engine.read_path(path, _ref(), ReadRequest(document_id="kd_pdftest0001"))
    assert excinfo.value.body.code is KnowledgeErrorCode.CORRUPT_CONTENT


def test_page_limit_is_enforced(tmp_path, monkeypatch):
    from app.config import settings

    path = make_pdf(tmp_path / "sample.pdf", pages=3)
    monkeypatch.setattr(settings, "MAX_PAGES_LIMIT", 2)
    with pytest.raises(KnowledgeError) as excinfo:
        pdf_engine.read_path(path, _ref(), ReadRequest(document_id="kd_pdftest0001"))
    assert excinfo.value.body.code is KnowledgeErrorCode.PAGE_LIMIT_EXCEEDED
