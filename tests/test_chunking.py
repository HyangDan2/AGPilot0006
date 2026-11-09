import pytest
from app.parsers.chunking_engine import ChunkingEngine, estimate_token_count
from app.models.document import SourceLocation

def test_korean_and_english_token_estimation():
    """QA-06: Verify token counting for Korean, English, and mixed technical texts."""
    kor_text = "한국어 문서 파싱 및 구조화 서비스 테스트입니다."
    tokens_kor = estimate_token_count(kor_text)
    # Approx 26 Korean characters -> ~17 tokens
    assert 10 <= tokens_kor <= 30

    eng_text = "Document Intelligence Service parsing PDF, DOCX, and PPTX for LLM context budget."
    tokens_eng = estimate_token_count(eng_text)
    # 13 words -> ~15-25 tokens
    assert 12 <= tokens_eng <= 35

    mixed_text = "Mini-LED 광학 설계 보고서: 1000개의 디밍 존을 지원하는 차세대 디스플레이 기술 아키텍처."
    tokens_mixed = estimate_token_count(mixed_text)
    assert 20 <= tokens_mixed <= 60

def test_long_section_token_limit_enforcement():
    """
    QA-05: Long Section must be partitioned into multiple token-bounded chunks.
    No chunk should exceed chunk_max_tokens (1500).
    """
    # Create long text of ~3000 tokens (multiple paragraphs)
    paragraphs = []
    for i in range(25):
        paragraphs.append(
            f"Paragraph {i + 1}: High-power semiconductor lasers require precise thermal dissipation and optical alignment. "
            f"광학 시스템의 방열 설계 및 빔 정렬 특성을 엄격하게 제어하여 신뢰성을 확보합니다. " * 3
        )
    long_content = "\n\n".join(paragraphs)

    engine = ChunkingEngine(target_tokens=1000, max_tokens=1500, overlap_tokens=100)
    source = SourceLocation(type="pdf", page=3)

    chunks = engine.chunk_section(
        document_id="doc_chunk_test",
        section_id="sec_long_001",
        title="Thermal and Optical Alignment",
        content=long_content,
        source=source,
        figure_ids=["fig_opt_01"]
    )

    # Must be split into multiple chunks
    assert len(chunks) >= 2

    # Verify bounds and metadata on every chunk
    for chk in chunks:
        assert chk.parent_section_id == "sec_long_001"
        assert chk.source.page == 3
        assert "fig_opt_01" in chk.figure_ids
        assert chk.token_count <= 1500
        assert chk.token_count > 0
