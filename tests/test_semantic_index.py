import numpy as np
from fastapi.testclient import TestClient

from app.config import settings
from app.core.database import DocumentRepository, get_db_connection
from app.main import app
from app.services import semantic_index, vector_index
from app.storage.store import DocumentStore


def test_model_index_fallback_and_admin_settings(monkeypatch):
    monkeypatch.setattr(vector_index, "EMBEDDING_URL", "http://internal/v1/embeddings")
    monkeypatch.setattr(vector_index, "EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    monkeypatch.setattr(semantic_index, "MODEL_REVISION", "test-revision")
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "test-service")
    monkeypatch.setattr(settings, "AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "BYPASS_AUTH_IN_DEV", False)

    def mock_embed(texts, timeout):
        assert len(texts) <= semantic_index.BATCH_SIZE
        return [np.array([1, 0], dtype=np.float32) for _ in texts]

    monkeypatch.setattr(vector_index, "remote_embed_batch", mock_embed)
    headers = {"Authorization": "Bearer test-service", "X-HD2Lab-Role": "admiral", "X-HD2Lab-User": "admin"}
    settings_url = "/api/v1/settings/embeddings"
    search_url = "/api/v1/documents/doc_abc/search"
    chunk = {
        "chunk_id": "chk_1", "content": "열 회수 환기 장치의 필터는 여섯 달에 한 번 교체한다.",
        "source": {"type": "pdf", "page": 2},
    }

    with TestClient(app) as client:
        assert client.get(settings_url, headers={"X-HD2Lab-Role": "admiral"}).status_code == 401
        assert client.get(settings_url, headers={**headers, "X-HD2Lab-Role": "crew"}).status_code == 403
        assert client.get(settings_url, headers=headers).json()["enabled"] is False
        body = {"enabled": True, "min_similarity": 0.4}
        assert client.put(settings_url, headers=headers, json={**body, "min_similarity": 1.1}).status_code == 422
        assert client.put(settings_url, headers=headers, json=body).json()["enabled"] is True

        DocumentRepository.create_document("doc_abc", "test.pdf", "hash", "pdf", "application/pdf", 10)
        DocumentStore("doc_abc").save_parsed_data({}, "", [chunk])
        DocumentRepository.update_status("doc_abc", "completed")
        assert semantic_index.index_next_batch()
        assert client.get(settings_url, headers=headers).json()["indexed_documents"] == 1

        response = client.post(search_url, headers=headers, json={"query": "공조 설비의 소모품 교환 주기"})
        assert response.status_code == 200
        assert response.json()["results"][0]["source"]["page"] == 2

        monkeypatch.setattr(semantic_index, "MODEL_REVISION", "updated-model")
        assert semantic_index.search("doc_abc", "공조 설비", 5) == []
        monkeypatch.setattr(vector_index, "remote_embed_batch", lambda *args, **kwargs: None)
        assert not semantic_index.index_next_batch()
        assert client.get(settings_url, headers=headers).json()["retrying_documents"] == 1

        with get_db_connection() as conn:
            conn.execute("INSERT INTO document_chunks_fts VALUES (?, ?, ?, ?)",
                         ("chk_1", "doc_abc", "", chunk["content"]))
        fallback = client.post(search_url, headers=headers, json={"query": "필터는"})
        assert fallback.json()["results"][0]["chunk_id"] == "chk_1"
        assert client.put(settings_url, headers=headers, json={**body, "enabled": False}).json()["enabled"] is False


def test_partial_index_resumes_after_failure(monkeypatch):
    monkeypatch.setattr(vector_index, "EMBEDDING_URL", "http://internal/v1/embeddings")
    monkeypatch.setattr(semantic_index, "MODEL_REVISION", "test-revision")
    with get_db_connection() as conn:
        semantic_index.init_schema(conn)
    semantic_index.save_settings(True, 0.4)
    DocumentRepository.create_document("doc_resume", "test.pdf", "hash", "pdf", "application/pdf", 10)
    chunks = [{"chunk_id": f"chunk_{index}", "content": f"paragraph {index}"} for index in range(3)]
    DocumentStore("doc_resume").save_parsed_data({}, "", chunks)
    DocumentRepository.update_status("doc_resume", "completed")

    calls = []

    def embed(texts, timeout):
        calls.append(texts)
        if len(calls) == 2:
            return None
        return [np.array([1, 0], dtype=np.float32) for _ in texts]

    monkeypatch.setattr(vector_index, "remote_embed_batch", embed)
    assert semantic_index.index_next_batch()
    assert semantic_index.status()["pending_documents"] == 1
    assert not semantic_index.index_next_batch()
    assert semantic_index.status()["retrying_documents"] == 1
    with get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_vectors").fetchone()[0] == 2
        conn.execute("UPDATE semantic_documents SET retry_after = 1")
    assert semantic_index.index_next_batch()
    assert calls == [["paragraph 0", "paragraph 1"], ["paragraph 2"], ["paragraph 2"]]
    assert semantic_index.status()["indexed_documents"] == 1
    assert len(semantic_index.search("doc_resume", "query", 3)) == 3


def test_queue_controls_preserve_existing_search(monkeypatch):
    monkeypatch.setattr(vector_index, "EMBEDDING_URL", "http://internal/v1/embeddings")
    monkeypatch.setattr(semantic_index, "MODEL_REVISION", "queue-test")
    monkeypatch.setattr(settings, "SERVICE_TOKEN", "queue-test-token")
    monkeypatch.setattr(settings, "AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "BYPASS_AUTH_IN_DEV", False)
    monkeypatch.setattr(semantic_index, "quiet_hours", lambda: False)
    monkeypatch.setattr(vector_index, "remote_embed_batch", lambda texts, timeout: [
        np.array([1, 0], dtype=np.float32) for _ in texts])
    DocumentRepository.create_document("doc_queue", "queue.pdf", "hash", "pdf", "application/pdf", 10)
    DocumentStore("doc_queue").save_parsed_data({}, "", [{"chunk_id": "chk_1", "content": "this is evidence"}])
    DocumentRepository.update_status("doc_queue", "completed")
    headers = {"Authorization": "Bearer queue-test-token", "X-HD2Lab-Role": "admiral", "X-HD2Lab-User": "admin"}
    url = "/api/v1/settings/embeddings/queue"
    with TestClient(app) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers={**headers, "X-HD2Lab-Role": "crew"}).status_code == 403
        semantic_index.save_settings(True, 0.4)
        assert client.put(url, json={"paused": True}, headers=headers).json()["paused"]
        assert not semantic_index.index_next_batch()
        assert client.get("/api/v1/settings/embeddings", headers=headers).json()["enabled"]
        assert client.post(url + "/doc_queue", json={"action": "cancel"}, headers=headers).status_code == 200
        assert client.get(url + "?state=cancelled", headers=headers).json()["jobs"][0]["state"] == "cancelled"
        assert client.post(url + "/doc_queue", json={"action": "retry"}, headers=headers).status_code == 200
        assert client.get(url + "?state=waiting", headers=headers).json()["total"] == 1
        assert not client.put(url, json={"paused": False}, headers=headers).json()["paused"]
        assert semantic_index.index_next_batch()
        assert client.put(url, json={"paused": True}, headers=headers).is_success
        assert semantic_index.search("doc_queue", "search query", 1) == [("chk_1", 1.0)]
        assert client.post(url + "/doc_queue", json={"action": "cancel"}, headers=headers).status_code == 409


def test_large_batch_defers_during_busy_hours(monkeypatch):
    monkeypatch.setattr(vector_index, "EMBEDDING_URL", "http://internal/v1/embeddings")
    monkeypatch.setattr(semantic_index, "MODEL_REVISION", "queue-test")
    monkeypatch.setattr(semantic_index, "quiet_hours", lambda: False)
    monkeypatch.setattr(semantic_index, "_day_limit", 5)
    with get_db_connection() as conn:
        semantic_index.init_schema(conn)
    semantic_index.save_settings(True, 0.4)
    DocumentRepository.create_document("doc_long", "long.pdf", "hash", "pdf", "application/pdf", 10)
    DocumentStore("doc_long").save_parsed_data({}, "", [{"chunk_id":"chk_1", "content":"large body text"}])
    DocumentRepository.update_status("doc_long", "completed")
    assert not semantic_index.index_next_batch()
    assert semantic_index.queue_status("waiting", 10, 0)["jobs"][0]["state"] == "scheduled"
    assert not semantic_index.index_next_batch()
    monkeypatch.setattr(semantic_index, "quiet_hours", lambda: True)
    monkeypatch.setattr(vector_index, "remote_embed_batch", lambda texts, timeout: [np.array([1, 0], dtype=np.float32)])
    assert semantic_index.index_next_batch()
    assert semantic_index.queue_status("done", 10, 0)["total"] == 1


def test_search_priority_prevents_another_background_batch(monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()
    calls = []

    def embed(texts, timeout):
        calls.append(texts[0])
        started.set()
        assert release.wait(2)
        return [np.array([1, 0], dtype=np.float32)]

    monkeypatch.setattr(semantic_index, "revision_matches", lambda: True)
    monkeypatch.setattr(vector_index, "remote_embed_batch", embed)

    thread = threading.Thread(target=semantic_index.search_embedding, args=("foreground",))
    thread.start()
    try:
        assert started.wait(2)
        assert not semantic_index.index_next_batch()
        assert calls == ["foreground"]
    finally:
        release.set()
        thread.join(2)
