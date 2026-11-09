import io
import pymupdf as fitz
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.core.database import DocumentRepository, JobRepository, get_db_connection

@pytest.fixture
def auth_client():
    headers = {
        "Authorization": f"Bearer {settings.SERVICE_TOKEN}",
        "X-Tenant-Id": "qa_tenant",
        "X-Owner-Id": "qa_user"
    }
    return TestClient(app, headers=headers)

def test_server_restart_orphaned_job_recovery():
    """
    QA-12: When the server restarts, dangling jobs in 'processing' or 'queued' state
    must be safely identified and transitioned to failed with clear reason.
    """
    doc_id = "doc_orphaned_001"
    job_id = "job_orphaned_001"

    # Insert an artificial orphaned job and document
    DocumentRepository.create_document(
        document_id=doc_id,
        filename="dangling.pdf",
        file_hash="fake_hash_123",
        doc_type="pdf",
        mime_type="application/pdf",
        file_size=1024
    )
    JobRepository.create_job(job_id, doc_id)
    JobRepository.mark_started(job_id)

    # Verify initial processing state
    doc_before = DocumentRepository.get_document(doc_id)
    assert doc_before is not None

    # Trigger recovery
    recovered_ids = JobRepository.recover_interrupted_jobs()
    assert doc_id in recovered_ids

    # Verify recovery result
    doc_after = DocumentRepository.get_document(doc_id)
    assert doc_after["status"] == "failed"
    assert "Interrupted by server restart" in doc_after["error_message"]

def test_concurrent_uploads_integrity(auth_client):
    """
    QA-16: Concurrent document uploads must process safely without database locks
    or state collisions.
    """
    def make_pdf(idx: int) -> bytes:
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((50, 72), f"Concurrent PDF {idx}: Testing database transaction isolation.")
        buf = io.BytesIO()
        doc.save(buf)
        doc.close()
        return buf.getvalue()

    def upload_one(idx: int):
        data = make_pdf(idx)
        res = auth_client.post(
            "/api/v1/documents",
            files={"file": (f"concurrent_{idx}.pdf", data, "application/pdf")}
        )
        assert res.status_code == 202
        return res.json()["document_id"]

    # Execute 5 concurrent uploads
    with ThreadPoolExecutor(max_workers=5) as executor:
        doc_ids = list(executor.map(upload_one, range(5)))

    assert len(doc_ids) == 5
    assert len(set(doc_ids)) == 5  # all IDs unique

    # Check that all 5 completed
    for d_id in doc_ids:
        status_res = auth_client.get(f"/api/v1/documents/{d_id}/status")
        assert status_res.status_code == 200
        assert status_res.json()["status"] == "completed"
