import re
import sqlite3
from typing import List, Dict, Any, Optional
from fastapi import HTTPException, status
from app.core.database import get_db_connection
from app.models.document import SearchResponse, SearchResultItem, SourceLocation
from app.storage.store import DocumentStore

class DocumentSearchService:
    @classmethod
    def index_document_chunks(cls, document_id: str, chunks: List[Dict[str, Any]]) -> None:
        """Indexes or re-indexes document chunks into the SQLite FTS5 table."""
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

    @classmethod
    def search(
        cls,
        document_id: str,
        query: str,
        limit: int = 10,
        max_snippet_chars: int = 300
    ) -> SearchResponse:
        """
        Executes full-text keyword search for a document.
        Uses FTS5 with BM25 ranking when available, and falls back to chunk scan.
        """
        clean_query = query.strip()
        if not clean_query:
            return SearchResponse(document_id=document_id, query=query, total_matches=0, results=[])

        # Validate limit bounds
        limit = max(1, min(limit, 50))

        # Check FTS5
        results = cls._search_fts5(document_id, clean_query, limit, max_snippet_chars)
        if results is None:
            # Fallback
            results = cls._search_fallback(document_id, clean_query, limit, max_snippet_chars)

        return SearchResponse(
            document_id=document_id,
            query=clean_query,
            total_matches=len(results),
            results=results[:limit]
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
