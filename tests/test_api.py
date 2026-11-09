import io
import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.config import settings
from app.core.database import init_db
from app.storage.store import DocumentStore

AUTH_HEADERS = {"Authorization": f"Bearer {settings.SERVICE_TOKEN}", "X-Owner-Id": "test_user", "X-Tenant-Id": "test_tenant"}

@pytest.fixture
def client():
    return TestClient(app, headers=AUTH_HEADERS)

def create_sample_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), "Title: Quantum Dot Display Report\nQuantum dot materials are widely used.")
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()

def test_root_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "HD2Lab" in response.text
    assert "Document Intelligence API is Active" in response.text

def test_upload_and_pipeline(client):
    pdf_bytes = create_sample_pdf_bytes()
    
    # 1. Upload
    response = client.post(
        "/api/v1/documents",
        files={"file": ("quantum_report.pdf", pdf_bytes, "application/pdf")}
    )
    assert response.status_code == 202
    data = response.json()
    doc_id = data["document_id"]
    assert doc_id.startswith("doc_")
    
    # In TestClient with BackgroundTasks, the background task has run synchronously
    # 2. Status
    res_status = client.get(f"/api/v1/documents/{doc_id}/status")
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["status"] == "completed"
    assert status_data["page_count"] == 1
    assert status_data["section_count"] >= 1

    # 3. Outline
    res_outline = client.get(f"/api/v1/documents/{doc_id}/outline")
    assert res_outline.status_code == 200
    outline_data = res_outline.json()
    assert len(outline_data["sections"]) >= 1
    sec_id = outline_data["sections"][0]["section_id"]

    # 4. Section detail
    res_sec = client.get(f"/api/v1/documents/{doc_id}/sections/{sec_id}")
    assert res_sec.status_code == 200
    sec_data = res_sec.json()
    assert "Quantum" in sec_data["content"] or "Quantum" in sec_data["title"]

    # 5. Search
    res_search = client.post(f"/api/v1/documents/{doc_id}/search", json={"query": "Quantum dot"})
    assert res_search.status_code == 200
    search_data = res_search.json()
    assert search_data["total_matches"] >= 1
    assert "Quantum" in search_data["results"][0]["snippet"]

def test_cache_deduplication(client):
    pdf_bytes = create_sample_pdf_bytes()
    # First upload
    res1 = client.post(
        "/api/v1/documents",
        files={"file": ("sample1.pdf", pdf_bytes, "application/pdf")}
    )
    assert res1.status_code == 202
    doc_id1 = res1.json()["document_id"]

    # Second upload with identical bytes
    res2 = client.post(
        "/api/v1/documents",
        files={"file": ("sample2.pdf", pdf_bytes, "application/pdf")}
    )
    assert res2.status_code == 202
    doc_id2 = res2.json()["document_id"]
    assert doc_id1 != doc_id2
    assert "cache" in res2.json()["message"].lower()
    
    # Check that second document status is immediately completed
    res_status = client.get(f"/api/v1/documents/{doc_id2}/status")
    assert res_status.status_code == 200
    assert res_status.json()["status"] == "completed"
