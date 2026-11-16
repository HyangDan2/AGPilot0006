import pytest

from app.config import settings
from app.core.database import get_db_connection, init_db
from app.services.document_search import DocumentSearchService
from app.services.vector_index import VectorIndexService
from app.storage.store import DocumentStore


def test_hybrid_rag_vector_search_and_lazy_backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "DATABASE_PATH", tmp_path / "db.sqlite3")
    monkeypatch.setattr(settings, "DOCUMENTS_DIR", tmp_path / "docs")
    init_db()

    doc_id = "doc_gan_test01"
    chunks = [
        {
            "chunk_id": "chk_0001",
            "parent_section_id": "sec_1",
            "content": "서론: 초해상도 복원 모델의 일반적인 배경과 데이터셋 구성을 설명한다.",
            "source": {"type": "pdf", "page": 1},
            "figure_ids": [],
        },
        {
            "chunk_id": "chk_0002",
            "parent_section_id": "sec_2",
            "content": "Real-ESRGAN에서는 GAN 학습 시 발생하는 불쾌한 아티팩트(ringing, overshoot)를 줄이기 위해 sinc 필터와 스펙트럴 정규화(SN)를 적용한 U-Net 판별자를 사용한다.",
            "source": {"type": "pdf", "page": 4},
            "figure_ids": [],
        },
        {
            "chunk_id": "chk_0003",
            "parent_section_id": "sec_3",
            "content": "결론 및 향후 연구 과제.",
            "source": {"type": "pdf", "page": 9},
            "figure_ids": [],
        },
    ]

    store = DocumentStore(doc_id)
    store.save_parsed_data({"sections": []}, "", chunks)

    # Search WITHOUT explicit VectorIndexService.index_document_chunks call -> triggers lazy backfill
    res = DocumentSearchService.search(doc_id, "GAN 특유의 불결한 아티팩트는 어떻게 처리하나?", limit=3)
    assert res.total_matches >= 1
    assert res.results[0].chunk_id == "chk_0002"

    # Confirm vectors are now persisted in SQLite
    with get_db_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM document_chunk_vectors WHERE document_id = ?",
            (doc_id,),
        ).fetchone()[0]
    assert count == 3

    # Also test explicit indexing + hybrid RRF boost
    indexed = VectorIndexService.index_document_chunks(doc_id, chunks)
    assert indexed == 3
    res2 = DocumentSearchService.search(doc_id, "아티팩트 sinc 필터 U-Net 판별자", limit=2)
    assert res2.results[0].chunk_id == "chk_0002"
