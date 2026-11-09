import time
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
import pymupdf as fitz
import io

from app.main import app
from app.config import settings
from app.core.job_manager import JobManager
from app.core.database import DocumentRepository

@pytest.fixture
def auth_client():
    headers = {
        "Authorization": f"Bearer {settings.SERVICE_TOKEN}",
        "X-Tenant-Id": "qa_tenant",
        "X-Owner-Id": "qa_user"
    }
    return TestClient(app, headers=headers)

def test_fts5_search_snippets_and_ranking(auth_client):
    """
    QA-13: FTS search returns relevant chunks, highlighted snippets, and BM25 ranking.
    """
    doc = fitz.open()
    
    # Page 1: mentions waveguide lasers
    p1 = doc.new_page()
    p1.insert_text((50, 72), "Section 1: Quantum Dot Laser Waveguides\nWaveguide lasers provide high spectral purity and narrow linewidth.")
    
    # Page 2: mentions general electronics
    p2 = doc.new_page()
    p2.insert_text((50, 72), "Section 2: Microcontroller Specs\nStandard ARM Cortex architecture for basic control loop.")

    buf = io.BytesIO()
    doc.save(buf)
    doc.close()

    res = auth_client.post(
        "/api/v1/documents",
        files={"file": ("quantum_waveguide.pdf", buf.getvalue(), "application/pdf")}
    )
    doc_id = res.json()["document_id"]

    # Search for "Waveguide lasers"
    search_res = auth_client.post(
        f"/api/v1/documents/{doc_id}/search",
        json={"query": "Waveguide lasers", "limit": 5}
    )
    assert search_res.status_code == 200
    data = search_res.json()
    assert data["total_matches"] >= 1
    top_hit = data["results"][0]
    assert "<b>Waveguide</b>" in top_hit["snippet"] or "<b>lasers</b>" in top_hit["snippet"]
    assert top_hit["source"]["page"] == 1

def test_parsing_timeout_enforcement():
    """
    QA-15: Parsing jobs that exceed timeout are interrupted, recorded as failed,
    and update the document status cleanly.
    """
    doc_id = "doc_timeout_test"
    DocumentRepository.create_document(
        document_id=doc_id,
        filename="slow.pdf",
        file_hash="slow_hash",
        doc_type="pdf",
        mime_type="application/pdf",
        file_size=1024
    )

    def slow_parser(did: str):
        time.sleep(2.0)

    # Force a very short timeout of 0.2 seconds for the test
    with patch.object(settings, "PARSING_TIMEOUT_SECONDS", 0.2):
        JobManager.run_job(doc_id, slow_parser)

    doc = DocumentRepository.get_document(doc_id)
    assert doc["status"] == "failed"
    assert "timed out" in doc["error_message"].lower()
