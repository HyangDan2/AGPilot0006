import io
import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings

@pytest.fixture
def test_client():
    return TestClient(app)

def create_sample_pdf() -> bytes:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((50, 72), "Confidential Document Content")
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()

def test_unauthenticated_request_rejected(test_client):
    """QA-10: Requests without valid token must be rejected with 401."""
    pdf_bytes = create_sample_pdf()
    
    # Upload without auth header
    res = test_client.post(
        "/api/v1/documents",
        files={"file": ("secret.pdf", pdf_bytes, "application/pdf")}
    )
    assert res.status_code == 401

    # Get without auth header
    res_get = test_client.get("/api/v1/documents/doc_123456789012")
    assert res_get.status_code == 401

def test_invalid_service_token_rejected(test_client):
    """QA-10: Requests with invalid token must receive 401."""
    res = test_client.get(
        "/api/v1/documents/doc_123456789012",
        headers={"Authorization": "Bearer totally_wrong_secret_token"}
    )
    assert res.status_code == 401

def test_cross_tenant_isolation(test_client):
    """QA-11: Documents belonging to tenant A cannot be accessed by tenant B (returns 404)."""
    pdf_bytes = create_sample_pdf()
    
    # Upload under Tenant Alpha
    res_alpha = test_client.post(
        "/api/v1/documents",
        files={"file": ("tenant_a.pdf", pdf_bytes, "application/pdf")},
        headers={
            "Authorization": f"Bearer {settings.SERVICE_TOKEN}",
            "X-Tenant-Id": "tenant_alpha",
            "X-Owner-Id": "alice"
        }
    )
    assert res_alpha.status_code == 202
    doc_id = res_alpha.json()["document_id"]

    # Attempt access under Tenant Beta
    res_beta = test_client.get(
        f"/api/v1/documents/{doc_id}",
        headers={
            "Authorization": f"Bearer {settings.SERVICE_TOKEN}",
            "X-Tenant-Id": "tenant_beta",
            "X-Owner-Id": "bob"
        }
    )
    assert res_beta.status_code == 404

def test_owner_private_scope_isolation(test_client):
    """QA-11: Private documents cannot be viewed by other owners in the same tenant (returns 403)."""
    pdf_bytes = create_sample_pdf()
    
    # Upload as Alice (private)
    res_alice = test_client.post(
        "/api/v1/documents",
        files={"file": ("alice_private.pdf", pdf_bytes, "application/pdf")},
        headers={
            "Authorization": f"Bearer {settings.SERVICE_TOKEN}",
            "X-Tenant-Id": "common_tenant",
            "X-Owner-Id": "alice",
            "X-Access-Scope": "private"
        }
    )
    doc_id = res_alice.json()["document_id"]

    # Bob tries to access
    res_bob = test_client.get(
        f"/api/v1/documents/{doc_id}",
        headers={
            "Authorization": f"Bearer {settings.SERVICE_TOKEN}",
            "X-Tenant-Id": "common_tenant",
            "X-Owner-Id": "bob",
            "X-Access-Scope": "read_write"
        }
    )
    assert res_bob.status_code == 403
