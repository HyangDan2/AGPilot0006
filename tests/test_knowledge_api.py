"""`dk/v1` HTTP surface: upload, read, search, update, validate, rollback, errors."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.knowledge_fixtures import make_docx, make_xlsx

AUTH = {"Authorization": "Bearer hd2-doc-secret-token-key-2026"}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _upload(client: TestClient, path, mime: str) -> dict:
    with open(path, "rb") as handle:
        response = client.post(
            "/api/v1/knowledge/documents",
            headers=AUTH,
            files={"file": (path.name, handle, mime)},
        )
    assert response.status_code == 201, response.text
    return response.json()


def test_upload_read_search_update_rollback_flow(tmp_path):
    client = TestClient(app)
    docx_path = make_docx(tmp_path / "sample.docx")
    created = _upload(client, docx_path, DOCX)
    document = created["document"]
    assert created["contract_version"] == "dk/v1"
    assert document["revision"] == 1

    read = client.post(
        "/api/v1/knowledge/read",
        headers=AUTH,
        json={"document_id": document["document_id"], "include_images": True},
    )
    assert read.status_code == 200, read.text
    payload = read.json()
    assert payload["citations"]
    assert any(node["kind"] == "table" for node in payload["nodes"])

    search = client.post(
        "/api/v1/knowledge/search",
        headers=AUTH,
        json={"query": "structured citations", "mode": "hybrid"},
    )
    assert search.status_code == 200
    search_payload = search.json()
    assert search_payload["degraded"] is True
    assert search_payload["results"][0]["citation"]["document"]["document_id"] == document["document_id"]

    update = client.post(
        f"/api/v1/knowledge/documents/{document['document_id']}/update",
        headers=AUTH,
        json={
            "document_id": document["document_id"],
            "expected_revision": 1,
            "operations": [{"op": "replace_text", "find": "closing", "replace": "ending"}],
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["document"]["revision"] == 2

    stale = client.post(
        f"/api/v1/knowledge/documents/{document['document_id']}/update",
        headers=AUTH,
        json={
            "document_id": document["document_id"],
            "expected_revision": 1,
            "operations": [{"op": "replace_text", "find": "ending", "replace": "final"}],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"

    rollback = client.post(
        f"/api/v1/knowledge/documents/{document['document_id']}/rollback",
        headers=AUTH,
        params={"to_revision": 1, "expected_revision": 2},
    )
    assert rollback.status_code == 200
    assert rollback.json()["document"]["revision"] == 3

    validate = client.post(
        f"/api/v1/knowledge/documents/{document['document_id']}/validate", headers=AUTH
    )
    assert validate.status_code == 200
    assert validate.json()["ok"] is True

    download = client.get(
        f"/api/v1/knowledge/documents/{document['document_id']}/content",
        headers=AUTH,
        params={"revision": 1},
    )
    assert download.status_code == 200
    assert download.content.startswith(b"PK")


def test_create_from_blocks_and_update_sheet(tmp_path):
    client = TestClient(app)
    create = client.post(
        "/api/v1/knowledge/documents/from-blocks",
        headers=AUTH,
        json={
            "name": "plan.xlsx",
            "mime_type": XLSX,
            "blocks": [
                {"kind": "heading", "text": "Plan"},
                {"kind": "table", "rows": [["Task", "Owner"], ["Ship R21", "HD2"]]},
            ],
        },
    )
    assert create.status_code == 201, create.text
    document_id = create.json()["document"]["document_id"]

    update = client.post(
        f"/api/v1/knowledge/documents/{document_id}/update",
        headers=AUTH,
        json={
            "document_id": document_id,
            "operations": [
                {"op": "set_cell", "sheet": "Sheet1", "cell": "A4", "value": "Done"},
                {"op": "add_sheet", "sheet": "Extra"},
            ],
        },
    )
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["document"]["revision"] == 2
    assert "set_cell:Sheet1!A4" in body["applied"]

    revisions = client.get(
        f"/api/v1/knowledge/documents/{document_id}/revisions", headers=AUTH
    )
    assert [entry["revision"] for entry in revisions.json()["revisions"]] == [1, 2]


def test_error_contract_over_http(tmp_path):
    client = TestClient(app)
    missing = client.post(
        "/api/v1/knowledge/read", headers=AUTH, json={"document_id": "kd_doesnotexist"}
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "document_not_found"

    xlsx_path = make_xlsx(tmp_path / "book.xlsx")
    created = _upload(client, xlsx_path, XLSX)
    document_id = created["document"]["document_id"]
    bad_op = client.post(
        f"/api/v1/knowledge/documents/{document_id}/update",
        headers=AUTH,
        json={"document_id": document_id, "operations": [{"op": "add_slide"}]},
    )
    assert bad_op.status_code == 200
    assert bad_op.json()["skipped"][0].startswith("add_slide:unsupported_for_xlsx")


def test_upload_rejects_unknown_auth_and_empty_file(tmp_path):
    client = TestClient(app)
    assert client.get("/api/v1/knowledge/documents").status_code == 401
    empty = client.post(
        "/api/v1/knowledge/documents",
        headers=AUTH,
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "invalid_request"


def test_read_inline_reads_caller_bytes_without_storing(tmp_path):
    import base64

    client = TestClient(app)
    payload = base64.b64encode(b"inline body text").decode()
    response = client.post(
        "/api/v1/knowledge/read-inline",
        headers=AUTH,
        json={
            "name": "workspace-note.txt",
            "mime_type": "text/plain",
            "content_base64": payload,
            "request": {"document_id": "inline_probe"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document"]["name"] == "workspace-note.txt"
    assert body["document"]["content_hash"].startswith("sha256:")
    assert "inline body text" in body["nodes"][0]["text"]
    listing = client.get("/api/v1/knowledge/documents", headers=AUTH).json()
    assert all(item["document_id"] != body["document"]["document_id"] for item in listing["documents"])


def test_vision_endpoint_is_honest_when_no_capability(tmp_path, monkeypatch):
    import base64

    monkeypatch.delenv("KNOWLEDGE_VISION_CAPABILITY_FILE", raising=False)
    client = TestClient(app)
    response = client.post(
        "/api/v1/knowledge/vision/analyze",
        headers=AUTH,
        json={"image_base64": base64.b64encode(b"png").decode(), "mime_type": "image/png"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["text"] == ""
    assert body["error"]
