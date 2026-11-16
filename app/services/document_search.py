import re
import sqlite3
from typing import List, Dict, Any, Optional
from fastapi import HTTPException, status
from app.core.database import get_db_connection
from app.models.document import SearchResponse, SearchResultItem, SourceLocation
from app.services.vector_index import VectorIndexService
from app.storage.store import DocumentStore

class DocumentSearchService:
    @classmethod
    def index_document_chunks(cls, document_id: str, chunks: List[Dict[str, Any]]) -> None:
        """Indexes or re-indexes document chunks into SQLite FTS5 and Vector DB."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                # Remove existing chunks for this document
                cursor.execute("DELETE FROM document_chunks_fts WHERE document_id = ?", (document_id,))
                
                # Insert chunks
                for chk in chunks:
                    cursor.execute("""
                        INSERT INTO document_chunks_fts (chunk_id, document_id, section_id, content)
                        VALUES (?, ?, ?, ?)
                    """, (
                        chk["chunk_id"],
                        document_id,
                        chk.get("parent_section_id") or "",
                        chk["content"]
                    ))
                conn.commit()
            except sqlite3.OperationalError:
                # FTS table might not be active, fallback will handle searches
                pass

        try:
            VectorIndexService.index_document_chunks(document_id, chunks)
        except Exception:
            # Never fail document parsing if vector indexing encounters an issue
            pass

    @classmethod
    def search(
        cls,
        document_id: str,
        query: str,
        limit: int = 10,
        max_snippet_chars: int = 300
    ) -> SearchResponse:
        """
        Hybrid RAG search combining FTS5/BM25 keyword search and SQLite Vector DB
        cosine similarity via Reciprocal Rank Fusion (RRF).
        """
        clean_query = query.strip()
        if not clean_query:
            return SearchResponse(document_id=document_id, query=query, total_matches=0, results=[])

        # Validate limit bounds
        limit = max(1, min(limit, 50))

        # 1. Keyword search (FTS5 or fallback scan)
        keyword_results = cls._search_fts5(document_id, clean_query, limit * 2, max_snippet_chars)
        if keyword_results is None:
            keyword_results = cls._search_fallback(document_id, clean_query, limit * 2, max_snippet_chars)

        # 2. Vector similarity search (with automatic lazy backfill for existing docs)
        vector_hits: List[tuple[str, float]] = []
        try:
            vector_hits = VectorIndexService.search_vectors(document_id, clean_query, limit=limit * 2)
        except Exception:
            vector_hits = []

        if not vector_hits:
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
        chunks_map = {c["chunk_id"]: c for c in store.load_chunks()}

        # Escape special FTS syntax characters for safety
        safe_query = re.sub(r'["*^:]', ' ', query).strip()
        if not safe_query:
            return []

        # Form FTS query terms
        terms = [t for t in safe_query.split() if t]
        fts_match_expr = " OR ".join(f'"{t}"' for t in terms)

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
                rows = cursor.fetchall()

                if not rows and not chunks_map:
                    return None

                items: List[SearchResultItem] = []
                for row in rows:
                    cid = row["chunk_id"]
                    orig_chk = chunks_map.get(cid, {})
                    content = row["content"] or orig_chk.get("content", "")
                    snippet = cls._extract_snippet(content, terms, max_snippet_chars)
                    
                    source = SourceLocation(**orig_chk["source"]) if "source" in orig_chk else SourceLocation(type="unknown")
                    items.append(SearchResultItem(
                        chunk_id=cid,
                        section_id=row["section_id"] or orig_chk.get("parent_section_id"),
                        snippet=snippet,
                        score=abs(float(row["rank"])),
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
