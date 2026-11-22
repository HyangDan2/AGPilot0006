import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from fastapi import HTTPException, status
from app.core.database import get_db_connection
from app.models.document import SearchResponse, SearchResultItem, SourceLocation
from app.services import semantic_index
from app.services.vector_index import VectorIndexService
from app.storage.store import DocumentStore

class DocumentSearchService:
    @classmethod
    def _record_search_log(
        cls,
        *,
        tenant_id: str,
        query: str,
        mode: str,
        provider: str,
        result_count: int,
        latency_ms: int,
        fallback_used: bool = False,
        detail: str = "",
    ) -> None:
        try:
            now = datetime.now(timezone.utc).isoformat()
            with get_db_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO search_engine_logs (
                        engine_type, tenant_id, query, mode, provider,
                        result_count, latency_ms, cache_hit, fallback_used, detail, created_at
                    ) VALUES ('document', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        query[:200],
                        mode,
                        provider,
                        int(result_count),
                        int(latency_ms),
                        1 if fallback_used else 0,
                        detail[:200],
                        now,
                    ),
                )
                # Keep log table bounded (latest 500 entries per type)
                conn.execute(
                    """
                    DELETE FROM search_engine_logs
                    WHERE engine_type = 'document'
                      AND id NOT IN (
                          SELECT id FROM search_engine_logs
                          WHERE engine_type = 'document'
                          ORDER BY id DESC LIMIT 500
                      )
                    """
                )
                conn.commit()
        except Exception:
            pass

    @classmethod
    def index_document_chunks(cls, document_id: str, chunks: List[Dict[str, Any]]) -> None:
        """Indexes or re-indexes document chunks into SQLite FTS5 (unicode61 + trigram) and Vector DB."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            for table in ("document_chunks_fts", "document_chunks_fts_trigram"):
                try:
                    cursor.execute(f"DELETE FROM {table} WHERE document_id = ?", (document_id,))
                    for chk in chunks:
                        cursor.execute(
                            f"""
                            INSERT INTO {table} (chunk_id, document_id, section_id, content)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                chk["chunk_id"],
                                document_id,
                                chk.get("parent_section_id") or "",
                                chk["content"],
                            ),
                        )
                except sqlite3.OperationalError:
                    # FTS table might not be active, fallback will handle searches
                    pass
            conn.commit()

        try:
            if not semantic_index.managed():
                VectorIndexService.index_document_chunks(document_id, chunks)
        except Exception:
            # Never fail document parsing if vector indexing encounters an issue
            pass

    @classmethod
    def _ensure_fts_indexed(cls, document_id: str, chunks: List[Dict[str, Any]]) -> None:
        """Lazily backfill FTS5/trigram rows for existing documents that have chunks on disk."""
        if not chunks:
            return
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                missing_tables = []
                for table in ("document_chunks_fts", "document_chunks_fts_trigram"):
                    try:
                        row = cur.execute(
                            f"SELECT 1 FROM {table} WHERE document_id = ? LIMIT 1",
                            (document_id,),
                        ).fetchone()
                        if row is None:
                            missing_tables.append(table)
                    except sqlite3.OperationalError:
                        continue
                if not missing_tables:
                    return
                for table in missing_tables:
                    for chk in chunks:
                        cur.execute(
                            f"INSERT INTO {table} (chunk_id, document_id, section_id, content) VALUES (?, ?, ?, ?)",
                            (
                                chk["chunk_id"],
                                document_id,
                                chk.get("parent_section_id") or "",
                                chk["content"],
                            ),
                        )
                conn.commit()
        except Exception:
            pass

    @classmethod
    def backfill_fts_indexes(cls) -> Dict[str, int]:
        """Backfill FTS5 (unicode61 + trigram) indexes for all completed documents missing entries."""
        scanned = 0
        indexed = 0
        skipped = 0
        with get_db_connection() as conn:
            docs = conn.execute(
                "SELECT document_id FROM documents WHERE status = 'completed' ORDER BY created_at DESC"
            ).fetchall()
        for row in docs:
            scanned += 1
            doc_id = row["document_id"]
            try:
                chunks = DocumentStore(doc_id).load_chunks()
            except Exception:
                skipped += 1
                continue
            if not chunks:
                skipped += 1
                continue
            with get_db_connection() as conn:
                cur = conn.cursor()
                needs = False
                for table in ("document_chunks_fts", "document_chunks_fts_trigram"):
                    try:
                        hit = cur.execute(
                            f"SELECT 1 FROM {table} WHERE document_id = ? LIMIT 1",
                            (doc_id,),
                        ).fetchone()
                        if hit is None:
                            needs = True
                            break
                    except sqlite3.OperationalError:
                        continue
            if needs:
                cls._ensure_fts_indexed(doc_id, chunks)
                indexed += 1
            else:
                skipped += 1
        return {"scanned": scanned, "indexed": indexed, "skipped": skipped}

    @classmethod
    def hybrid_status(cls, log_limit: int = 15) -> Dict[str, Any]:
        """Return Track B hybrid search index coverage and recent search activity."""
        with get_db_connection() as conn:
            cur = conn.cursor()
            total_docs = cur.execute(
                "SELECT COUNT(*) FROM documents WHERE status = 'completed'"
            ).fetchone()[0]
            try:
                fts_docs = cur.execute(
                    "SELECT COUNT(DISTINCT document_id) FROM document_chunks_fts"
                ).fetchone()[0]
                fts_available = True
            except sqlite3.OperationalError:
                fts_docs = 0
                fts_available = False
            try:
                trigram_docs = cur.execute(
                    "SELECT COUNT(DISTINCT document_id) FROM document_chunks_fts_trigram"
                ).fetchone()[0]
                trigram_available = True
            except sqlite3.OperationalError:
                trigram_docs = 0
                trigram_available = False

            stats_row = cur.execute(
                """
                SELECT
                    COUNT(*) AS total_queries,
                    COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                    SUM(CASE WHEN mode = 'hybrid' THEN 1 ELSE 0 END) AS hybrid_queries,
                    SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) AS fallback_queries
                FROM search_engine_logs
                WHERE engine_type = 'document'
                """
            ).fetchone()
            recent_rows = cur.execute(
                """
                SELECT id, tenant_id, query, mode, provider, result_count, latency_ms, fallback_used, detail, created_at
                FROM search_engine_logs
                WHERE engine_type = 'document'
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(log_limit, 50)),),
            ).fetchall()

        return {
            "total_documents": int(total_docs or 0),
            "fts_indexed_documents": int(fts_docs or 0),
            "trigram_indexed_documents": int(trigram_docs or 0),
            "fts_available": fts_available,
            "trigram_available": trigram_available,
            "rrf_k": 60,
            "total_queries": int(stats_row["total_queries"] or 0),
            "avg_latency_ms": round(float(stats_row["avg_latency_ms"] or 0), 1),
            "hybrid_queries": int(stats_row["hybrid_queries"] or 0),
            "fallback_queries": int(stats_row["fallback_queries"] or 0),
            "recent_logs": [dict(r) for r in recent_rows],
        }

    @classmethod
    def search(
        cls,
        document_id: str,
        query: str,
        limit: int = 10,
        max_snippet_chars: int = 300
    ) -> SearchResponse:
        """
        Hybrid RAG search combining FTS5/BM25 (unicode61 + trigram) keyword search
        and SQLite Vector DB cosine similarity via Reciprocal Rank Fusion (RRF).
        """
        started = time.perf_counter()
        clean_query = query.strip()
        if not clean_query:
            return SearchResponse(document_id=document_id, query=query, total_matches=0, results=[])

        # Validate limit bounds
        limit = max(1, min(limit, 50))

        # 1. Keyword search (FTS5 unicode61 + trigram or fallback scan)
        keyword_results = cls._search_fts5(document_id, clean_query, limit * 2, max_snippet_chars)
        fts_used = keyword_results is not None
        if keyword_results is None:
            keyword_results = cls._search_fallback(document_id, clean_query, limit * 2, max_snippet_chars)

        # 2. Vector similarity search (with automatic lazy backfill for existing docs)
        vector_hits: List[tuple[str, float]] = []
        vector_failed = False
        vector_attempted = False
        try:
            if semantic_index.managed():
                vector_attempted = True
                vector_hits = semantic_index.search(document_id, clean_query, limit * 2)
            else:
                vector_attempted = True
                vector_hits = VectorIndexService.search_vectors(document_id, clean_query, limit=limit * 2)
        except Exception:
            vector_hits = []
            vector_failed = True

        # Resolve tenant for observability log (best-effort)
        tenant_id = "default_tenant"
        try:
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT tenant_id FROM documents WHERE document_id = ?",
                    (document_id,),
                ).fetchone()
                if row and row["tenant_id"]:
                    tenant_id = row["tenant_id"]
        except Exception:
            pass

        if not vector_hits:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            fallback_used = vector_failed or (vector_attempted and semantic_index.managed())
            mode = "fts5_fallback" if fallback_used else ("fts5_only" if fts_used else "scan_fallback")
            cls._record_search_log(
                tenant_id=tenant_id,
                query=clean_query,
                mode=mode,
                provider="sqlite-fts5" if fts_used else "python-scan",
                result_count=len(keyword_results[:limit]),
                latency_ms=elapsed_ms,
                fallback_used=fallback_used,
                detail=f"doc={document_id} fts_hits={len(keyword_results)} vec_hits=0",
            )
            return SearchResponse(
                document_id=document_id,
                query=clean_query,
                total_matches=len(keyword_results),
                results=keyword_results[:limit],
            )

        # 3. Reciprocal Rank Fusion (RRF) combining keyword + semantic/subword ranks
        rrf_k = 60.0
        fused_scores: Dict[str, float] = {}
        item_map: Dict[str, SearchResultItem] = {}

        for rank, item in enumerate(keyword_results):
            cid = item.chunk_id
            fused_scores[cid] = fused_scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
            item_map[cid] = item

        for rank, (cid, vec_score) in enumerate(vector_hits):
            # Weight semantic/subword match slightly higher when keyword exact match is absent
            fused_scores[cid] = fused_scores.get(cid, 0.0) + (1.0 / (rrf_k + rank + 1)) + (0.01 * max(0.0, vec_score))

        # Materialize any vector-only hits from DocumentStore
        missing_cids = [cid for cid in fused_scores if cid not in item_map]
        if missing_cids:
            chunks_map = {c["chunk_id"]: c for c in DocumentStore(document_id).load_chunks()}
            terms = [t for t in re.sub(r'["*^:]', ' ', clean_query).split() if t]
            for cid in missing_cids:
                chk = chunks_map.get(cid)
                if not chk:
                    continue
                content = chk.get("content", "")
                source = SourceLocation(**chk["source"]) if "source" in chk else SourceLocation(type="unknown")
                item_map[cid] = SearchResultItem(
                    chunk_id=cid,
                    section_id=chk.get("parent_section_id"),
                    snippet=cls._extract_snippet(content, terms, max_snippet_chars),
                    score=0.0,
                    source=source,
                    figure_ids=chk.get("figure_ids", []),
                )

        ranked_cids = sorted(
            [cid for cid in fused_scores if cid in item_map],
            key=lambda cid: fused_scores[cid],
            reverse=True,
        )
        merged: List[SearchResultItem] = []
        for cid in ranked_cids[:limit]:
            item = item_map[cid]
            merged.append(item.model_copy(update={"score": round(fused_scores[cid], 6)}))

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        cls._record_search_log(
            tenant_id=tenant_id,
            query=clean_query,
            mode="hybrid",
            provider="fts5+vector-rrf",
            result_count=len(merged),
            latency_ms=elapsed_ms,
            fallback_used=False,
            detail=f"doc={document_id} fts_hits={len(keyword_results)} vec_hits={len(vector_hits)}",
        )

        return SearchResponse(
            document_id=document_id,
            query=clean_query,
            total_matches=len(ranked_cids),
            results=merged,
        )

    @classmethod
    def _search_fts5(
        cls,
        document_id: str,
        query: str,
        limit: int,
        max_snippet_chars: int
    ) -> Optional[List[SearchResultItem]]:
        store = DocumentStore(document_id)
        chunks_list = store.load_chunks()
        chunks_map = {c["chunk_id"]: c for c in chunks_list}

        # Lazily backfill FTS5/trigram for older documents if needed
        if chunks_list:
            cls._ensure_fts_indexed(document_id, chunks_list)

        # Escape special FTS syntax characters for safety
        safe_query = re.sub(r'["*^:]', ' ', query).strip()
        if not safe_query:
            return []

        # Form FTS query terms
        terms = [t for t in safe_query.split() if t]
        fts_match_expr = " OR ".join(f'"{t}"' for t in terms)
        trigram_terms = [t for t in terms if len(t) >= 3]
        trigram_expr = " OR ".join(f'"{t}"' for t in trigram_terms) if trigram_terms else ""

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT chunk_id, section_id, content, rank
                    FROM document_chunks_fts
                    WHERE document_id = ? AND document_chunks_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (document_id, fts_match_expr, limit * 2))
                uni_rows = cursor.fetchall()

                tri_rows = []
                if trigram_expr:
                    try:
                        cursor.execute("""
                            SELECT chunk_id, section_id, content, rank
                            FROM document_chunks_fts_trigram
                            WHERE document_id = ? AND document_chunks_fts_trigram MATCH ?
                            ORDER BY rank
                            LIMIT ?
                        """, (document_id, trigram_expr, limit * 2))
                        tri_rows = cursor.fetchall()
                    except sqlite3.OperationalError:
                        tri_rows = []

                if not uni_rows and not tri_rows and not chunks_map:
                    return None

                # Merge unicode61 + trigram BM25 results (preferring higher combined relevance)
                merged_rows: Dict[str, Dict[str, Any]] = {}
                for row in uni_rows:
                    cid = row["chunk_id"]
                    merged_rows[cid] = {
                        "chunk_id": cid,
                        "section_id": row["section_id"],
                        "content": row["content"],
                        "score": abs(float(row["rank"])) + 1.0,
                    }
                for row in tri_rows:
                    cid = row["chunk_id"]
                    tri_score = abs(float(row["rank"])) * 0.85
                    if cid in merged_rows:
                        merged_rows[cid]["score"] += tri_score
                    else:
                        merged_rows[cid] = {
                            "chunk_id": cid,
                            "section_id": row["section_id"],
                            "content": row["content"],
                            "score": tri_score,
                        }

                ordered = sorted(merged_rows.values(), key=lambda r: r["score"], reverse=True)[: limit * 2]
                items: List[SearchResultItem] = []
                for row in ordered:
                    cid = row["chunk_id"]
                    orig_chk = chunks_map.get(cid, {})
                    content = row["content"] or orig_chk.get("content", "")
                    snippet = cls._extract_snippet(content, terms, max_snippet_chars)

                    source = SourceLocation(**orig_chk["source"]) if "source" in orig_chk else SourceLocation(type="unknown")
                    items.append(SearchResultItem(
                        chunk_id=cid,
                        section_id=row["section_id"] or orig_chk.get("parent_section_id"),
                        snippet=snippet,
                        score=round(float(row["score"]), 6),
                        source=source,
                        figure_ids=orig_chk.get("figure_ids", [])
                    ))
                return items
        except sqlite3.OperationalError:
            return None

    @classmethod
    def _search_fallback(
        cls,
        document_id: str,
        query: str,
        limit: int,
        max_snippet_chars: int
    ) -> List[SearchResultItem]:
        store = DocumentStore(document_id)
        chunks = store.load_chunks()
        terms = [t.lower() for t in query.split() if t]
        
        matches: List[SearchResultItem] = []
        for chk in chunks:
            content = chk["content"]
            content_lower = content.lower()
            term_matches = sum(content_lower.count(t) for t in terms)
            if term_matches > 0:
                snippet = cls._extract_snippet(content, terms, max_snippet_chars)
                matches.append(SearchResultItem(
                    chunk_id=chk["chunk_id"],
                    section_id=chk.get("parent_section_id"),
                    snippet=snippet,
                    score=float(term_matches),
                    source=SourceLocation(**chk["source"]),
                    figure_ids=chk.get("figure_ids", [])
                ))

        matches.sort(key=lambda x: x.score, reverse=True)
        return matches[:limit]

    @classmethod
    def _extract_snippet(cls, content: str, terms: List[str], max_chars: int) -> str:
        """Extracts context window surrounding the first match with highlighting."""
        content_lower = content.lower()
        first_pos = -1
        for t in terms:
            pos = content_lower.find(t.lower())
            if pos != -1 and (first_pos == -1 or pos < first_pos):
                first_pos = pos

        if first_pos == -1:
            snippet = content[:max_chars]
        else:
            start = max(0, first_pos - (max_chars // 3))
            end = min(len(content), start + max_chars)
            snippet = content[start:end]
            if start > 0:
                snippet = "..." + snippet
            if end < len(content):
                snippet = snippet + "..."

        # Apply simple bold tags
        for t in terms:
            snippet = re.sub(f"({re.escape(t)})", r"<b>\1</b>", snippet, flags=re.IGNORECASE)

        return snippet
