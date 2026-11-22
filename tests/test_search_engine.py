import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.database import DocumentRepository, init_db
from app.main import app
from app.services import web_search
from app.services.document_search import DocumentSearchService
from app.storage.store import DocumentStore

AUTH_HEADERS = {
    "X-Service-Token": settings.SERVICE_TOKEN,
    "X-Owner-Id": "admiral-user",
    "X-Tenant-Id": "default_tenant",
}
ADMIN_HEADERS = {
    **AUTH_HEADERS,
    "X-HD2Lab-User": "hd2service",
    "X-HD2Lab-Role": "admiral",
}


def test_ssrf_url_guard():
    assert web_search.validate_safe_url("https://example.com/docs") is True
    assert web_search.validate_safe_url("http://127.0.0.1:8006/api") is False
    assert web_search.validate_safe_url("http://192.168.219.110:11435") is False
    assert web_search.validate_safe_url("http://localhost:8888/search") is False
    assert web_search.validate_safe_url("file:///etc/passwd") is False
    assert web_search.validate_safe_url("ftp://example.com/file") is False


def test_track_b_korean_trigram_fts_and_overview(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(settings, "DATABASE_PATH", tmp_path / "metadata.sqlite3")
    init_db()

    doc_id = "doc-korean-trigram-1"
    DocumentRepository.create_document(
        document_id=doc_id,
        filename="시설매뉴얼.pdf",
        file_hash="hash-kr-1",
        doc_type="pdf",
        mime_type="application/pdf",
        file_size=1234,
    )
    DocumentRepository.update_status(doc_id, "completed", page_count=3, section_count=1)
    chunks = [
        {
            "chunk_id": "chk-1",
            "document_id": doc_id,
            "parent_section_id": "sec-1",
            "content": "열회수환기장치의 소모품 필터를 6개월마다 교체합니다. 식별코드 M7R-624.",
            "figure_ids": [],
            "source": {"type": "pdf", "page": 2},
            "token_count": 30,
        },
        {
            "chunk_id": "chk-2",
            "document_id": doc_id,
            "parent_section_id": "sec-1",
            "content": "주차장 출입 통제 및 방문객 안내 데스크 운영 시간입니다.",
            "figure_ids": [],
            "source": {"type": "pdf", "page": 3},
            "token_count": 20,
        },
    ]
    DocumentStore(doc_id).save_parsed_data({"sections": []}, "", chunks)
    DocumentSearchService.index_document_chunks(doc_id, chunks)

    # Korean partial/compound match ("환기장치" inside "열회수환기장치의")
    res = DocumentSearchService.search(doc_id, "환기장치 필터", limit=5)
    assert res.total_matches >= 1
    assert res.results[0].chunk_id == "chk-1"

    status_data = DocumentSearchService.hybrid_status(log_limit=10)
    assert status_data["total_documents"] == 1
    assert status_data["fts_indexed_documents"] == 1
    assert status_data["trigram_indexed_documents"] == 1
    assert status_data["total_queries"] >= 1
    assert len(status_data["recent_logs"]) >= 1


def test_track_a_web_search_fallback_cache_and_admin_api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(settings, "DATABASE_PATH", tmp_path / "metadata.sqlite3")
    web_search._rate_buckets.clear()
    init_db()

    calls = []

    def fake_call_provider(provider, query, limit, lang, cfg):
        calls.append((provider, query))
        if provider == "duckduckgo":
            raise RuntimeError("simulated_429")
        if provider == "wikipedia":
            return [
                {
                    "title": "SQLite FTS5",
                    "url": "https://en.wikipedia.org/?curid=1",
                    "snippet": "FTS5 is an SQLite virtual table module.",
                    "source": "wikipedia-en",
                }
            ]
        return []

    monkeypatch.setattr(web_search, "_call_provider", fake_call_provider)

    with TestClient(app) as client:
        # Non-admin denied on settings endpoints
        r_denied = client.get("/api/v1/settings/search/overview", headers=AUTH_HEADERS)
        assert r_denied.status_code == 403

        # Public tenant blocked by default
        pub_headers = {
            "X-Service-Token": settings.SERVICE_TOKEN,
            "X-Owner-Id": "public-abc",
            "X-Tenant-Id": "public_tenant",
        }
        r_pub_blocked = client.post(
            "/api/v1/web/search",
            json={"query": "SQLite FTS5"},
            headers=pub_headers,
        )
        assert r_pub_blocked.status_code == 403

        # Admin test search triggers fallback duckduckgo -> wikipedia
        r_test = client.post(
            "/api/v1/settings/search/web/test",
            json={"query": "SQLite FTS5", "limit": 3},
            headers=ADMIN_HEADERS,
        )
        assert r_test.status_code == 200
        body1 = r_test.json()
        assert body1["provider"] == "wikipedia"
        assert body1["fallback_used"] is True
        assert body1["cache_hit"] is False
        assert len(body1["results"]) == 1

        # Second identical search hits SQLite TTL cache without calling providers again
        call_count_before = len(calls)
        r_cached = client.post(
            "/api/v1/settings/search/web/test",
            json={"query": "SQLite FTS5", "limit": 3},
            headers=ADMIN_HEADERS,
        )
        assert r_cached.status_code == 200
        body2 = r_cached.json()
        assert body2["cache_hit"] is True
        assert len(calls) == call_count_before

        # Overview reflects logs and cache hit rate
        r_ov = client.get("/api/v1/settings/search/overview", headers=ADMIN_HEADERS)
        assert r_ov.status_code == 200
        ov = r_ov.json()
        assert ov["web_search"]["total_queries"] == 2
        assert ov["web_search"]["cache_hits"] == 1
        assert ov["web_search"]["active_cache_entries"] == 1

        # Update web search settings to allow public tenant (1 rpm) and verify rate limit
        r_put = client.put(
            "/api/v1/settings/search/web",
            json={
                "enabled": True,
                "public_enabled": True,
                "primary_provider": "duckduckgo",
                "fallback_provider": "wikipedia",
                "searxng_url": "",
                "cache_ttl_seconds": 600,
                "public_rpm": 1,
                "admin_rpm": 20,
                "timeout_seconds": 5.0,
            },
            headers=ADMIN_HEADERS,
        )
        assert r_put.status_code == 200
        assert r_put.json()["settings"]["public_enabled"] is True

        r_pub_ok = client.post(
            "/api/v1/web/search",
            json={"query": "SQLite FTS5", "limit": 3},
            headers=pub_headers,
        )
        assert r_pub_ok.status_code == 200
        r_pub_429 = client.post(
            "/api/v1/web/search",
            json={"query": "SQLite FTS5", "limit": 3},
            headers=pub_headers,
        )
        assert r_pub_429.status_code == 429

        # Purge cache
        r_del = client.delete("/api/v1/settings/search/web/cache", headers=ADMIN_HEADERS)
        assert r_del.status_code == 200
        assert r_del.json()["removed"] >= 1

        # Backfill endpoint
        r_bf = client.post("/api/v1/settings/search/fts-backfill", headers=ADMIN_HEADERS)
        assert r_bf.status_code == 200
        assert r_bf.json()["ok"] is True
