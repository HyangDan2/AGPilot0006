"""Lightweight persistent Vector DB & Hybrid RAG embedding index on SQLite + NumPy."""
import hashlib
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np

from app.core.database import get_db_connection
from app.storage.store import DocumentStore

DEFAULT_VECTOR_DIM = 512
EMBEDDING_URL = os.environ.get("AGTOOLS_EMBEDDING_URL", "").strip()
EMBEDDING_MODEL = os.environ.get("AGTOOLS_EMBEDDING_MODEL", "text-embedding-3-small").strip()
EMBEDDING_API_KEY = os.environ.get("AGTOOLS_EMBEDDING_API_KEY", "").strip()
OLLAMA_NUM_GPU = os.getenv("AGTOOLS_EMBEDDING_OLLAMA_NUM_GPU", "").strip()


def _signed_hash_embed(text: str, dim: int = DEFAULT_VECTOR_DIM) -> np.ndarray:
    """Multilingual word + character n-gram signed feature hashing (L2-normalized float32)."""
    vec = np.zeros(dim, dtype=np.float32)
    cleaned = re.sub(r"\s+", " ", (text or "").casefold()).strip()
    if not cleaned:
        return vec

    tokens = re.findall(r"[0-9a-z가-힣_.-]{2,}", cleaned)
    for tok in tokens:
        digest = hashlib.blake2b(f"w:{tok}".encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign * 2.0

        # Character 2-gram and 3-gram subwords capture Korean particles/conjugations & technical stems
        if len(tok) >= 3:
            for n in (2, 3):
                for i in range(len(tok) - n + 1):
                    gram = tok[i : i + n]
                    gd = hashlib.blake2b(f"g{n}:{gram}".encode("utf-8"), digest_size=8).digest()
                    gidx = int.from_bytes(gd[:4], "little") % dim
                    gsign = 1.0 if (gd[4] & 1) == 0 else -1.0
                    vec[gidx] += gsign

    norm = float(np.linalg.norm(vec))
    if norm > 0.0:
        vec /= norm
    return vec


def remote_embed_batch(texts: List[str], timeout: float = 10.0) -> Optional[List[np.ndarray]]:
    """Call the configured Ollama or OpenAI-compatible endpoint."""
    if not EMBEDDING_URL or not texts:
        return None
    headers = {"Content-Type": "application/json"}
    if EMBEDDING_API_KEY:
        headers["Authorization"] = f"Bearer {EMBEDDING_API_KEY}"
    payload = {"model": EMBEDDING_MODEL, "input": [t[:4000] for t in texts]}
    ollama = EMBEDDING_URL.endswith("/api/embed")
    if ollama:
        payload.update(keep_alive="5m", truncate=True)
        if OLLAMA_NUM_GPU:
            payload["options"] = {"num_gpu": int(OLLAMA_NUM_GPU)}
    try:
        resp = httpx.post(
            EMBEDDING_URL,
            json=payload,
            headers=headers,
            timeout=timeout,
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json()
        if ollama:
            embeddings = data["embeddings"]
        else:
            items = sorted(data["data"], key=lambda item: item["index"])
            if [item["index"] for item in items] != list(range(len(texts))):
                return None
            embeddings = [item["embedding"] for item in items]
        if len(embeddings) != len(texts):
            return None
        vectors: List[np.ndarray] = []
        for embedding in embeddings:
            arr = np.asarray(embedding, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            if arr.ndim != 1 or not np.isfinite(norm) or norm <= 0.0:
                return None
            arr /= norm
            vectors.append(arr)
        return vectors
    except Exception:
        return None


def embed_texts(texts: List[str]) -> List[np.ndarray]:
    remote = remote_embed_batch(texts)
    if remote is not None:
        return remote
    return [_signed_hash_embed(t) for t in texts]


def embed_query(query: str) -> np.ndarray:
    remote = remote_embed_batch([query])
    if remote is not None and len(remote) == 1:
        return remote[0]
    return _signed_hash_embed(query)


def _ensure_vector_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_chunk_vectors (
            document_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL,
            PRIMARY KEY (document_id, chunk_id)
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vec_doc ON document_chunk_vectors(document_id);")


class VectorIndexService:
    """Stores, lazily backfills, and queries chunk vectors in SQLite."""

    @classmethod
    def index_document_chunks(cls, document_id: str, chunks: List[Dict[str, Any]]) -> int:
        if not chunks:
            return 0
        texts = [c.get("content", "") for c in chunks]
        vectors = embed_texts(texts)
        with get_db_connection() as conn:
            _ensure_vector_table(conn)
            conn.execute("DELETE FROM document_chunk_vectors WHERE document_id = ?", (document_id,))
            rows = [
                (document_id, c["chunk_id"], int(v.shape[0]), v.astype(np.float32).tobytes())
                for c, v in zip(chunks, vectors)
                if c.get("chunk_id")
            ]
            conn.executemany(
                """
                INSERT INTO document_chunk_vectors (document_id, chunk_id, dim, vector)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            return len(rows)

    @classmethod
    def _ensure_indexed(cls, document_id: str) -> List[Tuple[str, np.ndarray]]:
        with get_db_connection() as conn:
            _ensure_vector_table(conn)
            rows = conn.execute(
                "SELECT chunk_id, dim, vector FROM document_chunk_vectors WHERE document_id = ?",
                (document_id,),
            ).fetchall()

        if rows:
            parsed: List[Tuple[str, np.ndarray]] = []
            for row in rows:
                arr = np.frombuffer(row["vector"], dtype=np.float32)
                if arr.shape[0] == row["dim"]:
                    parsed.append((row["chunk_id"], arr))
            if parsed:
                return parsed

        # Lazy backfill for documents parsed before Vector DB deployment
        chunks = DocumentStore(document_id).load_chunks()
        if not chunks:
            return []
        cls.index_document_chunks(document_id, chunks)
        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, dim, vector FROM document_chunk_vectors WHERE document_id = ?",
                (document_id,),
            ).fetchall()
            return [
                (row["chunk_id"], np.frombuffer(row["vector"], dtype=np.float32))
                for row in rows
                if len(row["vector"]) == row["dim"] * 4
            ]

    @classmethod
    def search_vectors(cls, document_id: str, query: str, limit: int = 10) -> List[Tuple[str, float]]:
        entries = cls._ensure_indexed(document_id)
        if not entries:
            return []
        q_vec = embed_query(query)
        q_dim = int(q_vec.shape[0])
        compatible = [(cid, vec) for cid, vec in entries if int(vec.shape[0]) == q_dim]
        if not compatible:
            return []
        matrix = np.vstack([vec for _, vec in compatible])
        scores = matrix @ q_vec
        top_indices = np.argsort(scores)[::-1][:limit]
        results: List[Tuple[str, float]] = []
        for idx in top_indices:
            score = float(scores[int(idx)])
            if score > 0.0:
                results.append((compatible[int(idx)][0], score))
        return results
