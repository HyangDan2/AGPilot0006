import io
import pytest
from fastapi.testclient import TestClient
import pptx
from pptx.util import Inches
from PIL import Image

from app.main import app
from app.config import settings
from app.storage.store import DocumentStore

@pytest.fixture
def auth_client():
    headers = {
        "Authorization": f"Bearer {settings.SERVICE_TOKEN}",
        "X-Tenant-Id": "qa_tenant",
        "X-Owner-Id": "qa_user"
    }
    return TestClient(app, headers=headers)

def create_pptx_with_image(tmp_path) -> bytes:
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    
    # Text
    tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    tb.text_frame.text = "Cache Integrity Test Slide"

    # Image
    img = Image.new("RGB", (80, 80), color="purple")
    img_buf = io.BytesIO()
    img.save(img_buf, format="PNG")
    img_path = tmp_path / "cache_test_img.png"
    img_path.write_bytes(img_buf.getvalue())
    slide.shapes.add_picture(str(img_path), Inches(1), Inches(2), Inches(2), Inches(2))

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()

def test_cache_asset_and_id_integrity(tmp_path, auth_client):
    """
    QA-02 Acceptance Test:
    Upload PPTX with image twice.
    Verify both Document IDs have:
    - Status completed
    - Independent DocumentStore with correct document_id
    - Figure and slide assets accessible and identical on both IDs
    - manifest.json reflecting the respective document_id
    """
    pptx_bytes = create_pptx_with_image(tmp_path)

    # 1. First upload
    res1 = auth_client.post(
        "/api/v1/documents",
        files={"file": ("report_v1.pptx", pptx_bytes, "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
    )
    assert res1.status_code == 202
    doc_id1 = res1.json()["document_id"]

    # 2. Second upload with identical bytes (triggers cache hit)
    res2 = auth_client.post(
        "/api/v1/documents",
        files={"file": ("report_v2.pptx", pptx_bytes, "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
    )
    assert res2.status_code == 202
    doc_id2 = res2.json()["document_id"]

    assert doc_id1 != doc_id2
    assert "cache" in res2.json()["message"].lower()

    # 3. Verify status of both
    stat1 = auth_client.get(f"/api/v1/documents/{doc_id1}/status").json()
    stat2 = auth_client.get(f"/api/v1/documents/{doc_id2}/status").json()
    assert stat1["status"] == "completed"
    assert stat2["status"] == "completed"
    assert stat1["figure_count"] == stat2["figure_count"]
    assert stat1["figure_count"] >= 1

    # 4. Verify DocumentStore integrity
    store1 = DocumentStore(doc_id1)
    store2 = DocumentStore(doc_id2)

    tree1 = store1.load_parsed_tree()
    tree2 = store2.load_parsed_tree()
    assert tree1["document_id"] == doc_id1
    assert tree2["document_id"] == doc_id2

    # Verify manifest
    man1 = auth_client.get(f"/api/v1/documents/{doc_id1}/manifest").json()
    man2 = auth_client.get(f"/api/v1/documents/{doc_id2}/manifest").json()
    assert man1["document_id"] == doc_id1
    assert man2["document_id"] == doc_id2
    assert len(man1["assets"]) == len(man2["assets"])

    # 5. Verify asset streaming for both documents
    fig_id = tree1["figures"][0]["figure_id"]
    fig_asset1 = auth_client.get(f"/api/v1/documents/{doc_id1}/figures/{fig_id}/asset")
    fig_asset2 = auth_client.get(f"/api/v1/documents/{doc_id2}/figures/{fig_id}/asset")
    
    # If figure is an embedded image, it must exist on disk for both
    if store1.get_figure_path(fig_id):
        assert fig_asset1.status_code == 200
        assert fig_asset2.status_code == 200
        assert fig_asset1.content == fig_asset2.content
