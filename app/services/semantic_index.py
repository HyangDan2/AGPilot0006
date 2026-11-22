"""Model-backed document search and incremental indexing."""

import asyncio
import logging
import os
import sqlite3
import time
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from app.core.database import get_db_connection
from app.services import vector_index
from app.storage.store import DocumentStore

logger = logging.getLogger(__name__)
MODEL_REVISION = os.getenv("AGTOOLS_EMBEDDING_REVISION", "").strip()
BATCH_SIZE = 2
RETRY_SECONDS = 120
MAX_ATTEMPTS = 3
LEASE_SECONDS = 130
SEARCH_SECONDS = 7
DAY_BATCH_UNITS = max(1, int(os.getenv("AGTOOLS_INDEX_DAY_BATCH_UNITS", "550")))
DAY_BATCH_SECONDS = 4
_day_limit = DAY_BATCH_UNITS
QUIET_FROM = int(os.getenv("AGTOOLS_INDEX_QUIET_FROM", "1"))
QUIET_UNTIL = int(os.getenv("AGTOOLS_INDEX_QUIET_UNTIL", "6"))
KST = ZoneInfo("Asia/Seoul")
_gate = threading.Condition()
_searches = 0
_indexing = False

# Use the same bounded text sent to the provider. This is a scheduling estimate.
def batch_units(text: str) -> int:
    text = text[:4000]
    return max((len(text) + 1) // 2, (len(text.encode("utf-8")) + 2) // 3)


def quiet_hours() -> bool:
    hour = datetime.now(KST).hour
    return hour in (range(QUIET_FROM, QUIET_UNTIL) if QUIET_FROM <= QUIET_UNTIL
                    else (*range(QUIET_FROM, 24), *range(QUIET_UNTIL)))


def managed() -> bool:
    return bool(vector_index.EMBEDDING_URL)


def configured() -> bool:
    return managed() and bool(MODEL_REVISION and vector_index.EMBEDDING_MODEL)


def model_key() -> str:
    return f"{vector_index.EMBEDDING_MODEL}:{MODEL_REVISION}:retrieval-v1"


def revision_matches() -> bool:
    endpoint = vector_index.EMBEDDING_URL
    if not endpoint.endswith("/api/embed"):
        return True
    try:
        response = httpx.get(endpoint.removesuffix("/embed") + "/tags", timeout=1,
                             trust_env=False)
        response.raise_for_status()
        return any(item["name"] == vector_index.EMBEDDING_MODEL and item["digest"] == MODEL_REVISION
                   for item in response.json()["models"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return False


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS semantic_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL,
            min_similarity REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantic_documents (
            document_id TEXT PRIMARY KEY,
            document_version TEXT NOT NULL,
            model_key TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            retry_after INTEGER NOT NULL DEFAULT 0,
            next_offset INTEGER NOT NULL DEFAULT -1
        );
        CREATE TABLE IF NOT EXISTS semantic_vectors (
            document_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            vector BLOB NOT NULL,
            PRIMARY KEY (document_id, chunk_id)
        );
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(semantic_documents)")}
    migrations = {
        "next_offset": "INTEGER NOT NULL DEFAULT -1",
        "state": "TEXT NOT NULL DEFAULT ''",
        "claim": "TEXT NOT NULL DEFAULT ''",
        "lease_until": "INTEGER NOT NULL DEFAULT 0",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "total_chunks": "INTEGER",
        "started_at": "INTEGER",
        "last_activity": "INTEGER",
        "reason": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in migrations.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE semantic_documents ADD COLUMN {name} {ddl}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS semantic_queue_control (
            id INTEGER PRIMARY KEY CHECK (id = 1), paused INTEGER NOT NULL
        )
    """)
    # Preserve the effective worker state once, then manage it independently.
    conn.execute("""
        INSERT OR IGNORE INTO semantic_queue_control VALUES
        (1, 1 - COALESCE((SELECT enabled FROM semantic_settings WHERE id=1),0))
    """)


def read_settings(conn: sqlite3.Connection) -> tuple[bool, float]:
    row = conn.execute("SELECT enabled, min_similarity FROM semantic_settings WHERE id = 1").fetchone()
    return (bool(row["enabled"]), row["min_similarity"]) if row else (False, 0.40)


def save_settings(enabled: bool, min_similarity: float) -> None:
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO semantic_settings VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled,
                min_similarity = excluded.min_similarity
        """, (enabled, min_similarity))


QUEUE_ROWS = """
    SELECT d.document_id, d.filename, d.tenant_id, d.created_at,
           s.started_at, s.last_activity, s.reason, s.attempts, s.retry_after,
           CASE WHEN s.lease_until > ? THEN 'running'
                WHEN s.state IN ('failed', 'cancelled') THEN s.state
                WHEN s.reason = 'quiet_hours' AND NOT ? THEN 'scheduled'
                WHEN s.retry_after > 0 AND s.reason != 'quiet_hours' THEN 'retry'
                WHEN s.next_offset = -1 THEN 'done'
                ELSE 'waiting' END AS state,
           COALESCE(s.total_chunks, CASE WHEN s.next_offset = -1 THEN (
              SELECT COUNT(*) FROM semantic_vectors v WHERE v.document_id = d.document_id
           ) END) AS total_chunks,
           s.next_offset
    FROM documents d LEFT JOIN semantic_documents s
        ON s.document_id = d.document_id AND s.document_version = d.updated_at AND s.model_key = ?
    WHERE d.status = 'completed'
"""


def is_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT paused FROM semantic_queue_control WHERE id=1").fetchone()
    return bool(row and row["paused"])


def set_paused(paused: bool) -> None:
    with get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO semantic_queue_control VALUES (1, ?)", (paused,))


def status() -> dict:
    with get_db_connection() as conn:
        enabled, min_similarity = read_settings(conn)
        counts = _counts(conn)
    return {
        "enabled": enabled and configured(),
        "configured": configured(),
        "model": vector_index.EMBEDDING_MODEL if managed() else None,
        "model_revision": MODEL_REVISION or None,
        "min_similarity": min_similarity,
        "indexed_documents": counts.get("done", 0),
        "pending_documents": sum(counts.get(key, 0) for key in ("waiting", "scheduled", "running")),
        "retrying_documents": counts.get("retry", 0),
    }


def _counts(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute(
        f"SELECT state, COUNT(*) FROM ({QUEUE_ROWS}) GROUP BY state",
        (int(time.time()), quiet_hours(), model_key()),
    ).fetchall())


def queue_status(state: str, limit: int, offset: int) -> dict:
    with get_db_connection() as conn:
        parameters = [int(time.time()), quiet_hours(), model_key()]
        clause = {"all": "", "issues": "WHERE state IN ('retry','failed')",
                  "waiting": "WHERE state IN ('waiting','scheduled')"}.get(state, "WHERE state = ?")
        if clause == "WHERE state = ?":
            parameters.append(state)
        totals = _counts(conn)
        states = {"all": totals, "issues": ("retry", "failed"), "waiting": ("waiting", "scheduled")}
        total = sum(totals.get(key, 0) for key in states.get(state, (state,)))
        rows = conn.execute(
            f"""SELECT * FROM ({QUEUE_ROWS}) {clause}
                ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 WHEN 'scheduled' THEN 2
                    WHEN 'retry' THEN 3 WHEN 'failed' THEN 4 WHEN 'done' THEN 5 ELSE 6 END,
                    CASE WHEN state IN ('waiting', 'scheduled') THEN created_at END,
                    created_at DESC, document_id
                LIMIT ? OFFSET ?""", (*parameters, limit, offset),
        ).fetchall()
        jobs = []
        for row in rows:
            job = dict(row)
            offset_value = job.pop("next_offset")
            job["processed_chunks"] = job["total_chunks"] if offset_value == -1 else offset_value or 0
            jobs.append(job)
        return {
            "paused": is_paused(conn), "configured": configured(), "model": vector_index.EMBEDDING_MODEL if managed() else None,
            "counts": totals, "jobs": jobs, "total": total, "quiet_from": QUIET_FROM, "quiet_until": QUIET_UNTIL,
        }


def update_job(document_id: str, action: str) -> str:
    now = int(time.time())
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        doc = conn.execute("SELECT updated_at FROM documents WHERE document_id=? AND status='completed'",
                           (document_id,)).fetchone()
        if not doc:
            return "not_found"
        row = conn.execute(
            "SELECT * FROM semantic_documents WHERE document_id=? AND document_version=? AND model_key=?",
            (document_id, doc["updated_at"], model_key()),
        ).fetchone()
        if row and row["lease_until"] > now:
            return "conflict"
        if action == "retry":
            if not row or not (row["retry_after"] or row["state"] in ("failed", "cancelled")):
                return "conflict"
            conn.execute("""
                UPDATE semantic_documents SET state='', retry_after=0, reason='', attempts=0,
                    next_offset=MAX(next_offset,0), claim='', lease_until=0, last_activity=? WHERE document_id=?
            """, (now, document_id))
        elif row and (row["next_offset"] < 0 or row["state"] in ("failed", "cancelled") or
                      (row["retry_after"] and row["reason"] != "quiet_hours")):
            return "conflict"
        elif row:
            conn.execute("""
                UPDATE semantic_documents SET state='cancelled', retry_after=0, reason='', claim='',
                    lease_until=0, last_activity=? WHERE document_id=?
            """, (now, document_id))
        else:
            conn.execute("""
                INSERT OR REPLACE INTO semantic_documents
                  (document_id, document_version, model_key, dimension, next_offset, state, last_activity)
                  VALUES (?,?,?,0,0,'cancelled',?)
            """, (document_id, doc["updated_at"], model_key(), now))
        return "ok"


def _claim_batch():
    now = int(time.time())
    quiet = quiet_hours()
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if is_paused(conn):
            return None
        if conn.execute("SELECT 1 FROM semantic_documents WHERE lease_until > ? LIMIT 1", (now,)).fetchone():
            return None
        doc = conn.execute("""
            SELECT d.document_id, d.updated_at, COALESCE(s.dimension,0), COALESCE(MAX(s.next_offset,0),0),
                   COALESCE(s.attempts,0), s.started_at, s.total_chunks
            FROM documents d LEFT JOIN semantic_documents s
                ON s.document_id=d.document_id AND s.document_version=d.updated_at AND s.model_key=?
            WHERE d.status='completed' AND (
                s.document_id IS NULL OR (s.state NOT IN ('failed','cancelled')
                  AND (s.next_offset >= 0 OR s.retry_after > 0)
                  AND (s.retry_after <= ? OR (s.reason='quiet_hours' AND ?)))
            )
            ORDER BY d.created_at, d.document_id LIMIT 1
        """, (model_key(), now, quiet)).fetchone()
        if not doc:
            return None
        document_id, version, dimension, offset, attempts, started, total = doc
        claim = uuid.uuid4().hex
        conn.execute("""
            INSERT OR REPLACE INTO semantic_documents (document_id, document_version, model_key, dimension,
                next_offset, claim, lease_until, attempts, started_at, last_activity, total_chunks)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (document_id, version, model_key(), dimension, offset, claim, now + LEASE_SECONDS,
              attempts, started or now, now, total))
    return document_id, version, dimension, offset, attempts, claim, quiet


def index_next_batch() -> bool:
    global _indexing
    with _gate:
        if _indexing or _searches:
            return False
        _indexing = True
    try:
        return _index_batch()
    finally:
        with _gate:
            _indexing = False
            _gate.notify_all()


def _index_batch() -> bool:
    task = _claim_batch()
    if not task:
        return False
    global _day_limit
    document_id, version, dimension, offset, attempts, claim, quiet = task
    next_offset, retry_after, reason, state, rows, total = offset, 0, "", "", [], None
    try:
        chunks = [c for c in DocumentStore(document_id).load_chunks() if c["content"].strip()]
        total = len(chunks)
        batch = chunks[offset:offset + BATCH_SIZE]
        if not quiet and batch and batch_units(batch[0]["content"]) > _day_limit:
            reason = "quiet_hours"
            # Avoid repeatedly selecting deferred work. Quiet hours bypass this timestamp.
            retry_after = int(time.time()) + 86400
        elif batch:
            if not quiet and sum(batch_units(c["content"]) for c in batch) > _day_limit:
                batch = batch[:1]
            if not revision_matches():
                raise ValueError("model_revision")
            started = time.monotonic()
            vectors = vector_index.remote_embed_batch([c["content"] for c in batch], timeout=90)
            elapsed = time.monotonic() - started
            if not quiet and elapsed > 0 and vectors:
                units = sum(batch_units(c["content"]) for c in batch)
                measured_limit = max(1, int(units * DAY_BATCH_SECONDS / elapsed))
                # Reduce immediately after a slow batch, restore capacity gradually.
                _day_limit = min(DAY_BATCH_UNITS, measured_limit, (_day_limit * 3 + measured_limit) // 4)
            if not vectors or len(vectors) != len(batch):
                raise ValueError("embedding_unavailable")
            dimension = dimension or vectors[0].size
            if any(vector.size != dimension for vector in vectors):
                raise ValueError("embedding_dimension")
            rows = [(document_id, chunk["chunk_id"], vector.tobytes())
                    for chunk, vector in zip(batch, vectors)]
            next_offset += len(batch)
        if not reason:
            attempts = 0
            if next_offset >= total:
                next_offset = -1
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("Semantic index could not process an embedding batch")
        code = str(exc)
        reason = code if code in ("model_revision", "embedding_unavailable", "embedding_dimension") else "chunk_read_failed"
        attempts += 1
        retry_after = int(time.time()) + RETRY_SECONDS
        if attempts >= MAX_ATTEMPTS:
            state, retry_after = "failed", 0

    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute("""
            UPDATE semantic_documents SET dimension=?, retry_after=?, next_offset=?, state=?, claim='',
                lease_until=0, attempts=?, last_activity=?, total_chunks=COALESCE(?,total_chunks), reason=?
            WHERE document_id=? AND claim=? AND model_key=?
              AND EXISTS (SELECT 1 FROM documents d WHERE d.document_id=? AND d.status='completed' AND d.updated_at=?)
        """, (dimension, retry_after, next_offset, state, attempts, int(time.time()), total, reason,
              document_id, claim, model_key(), document_id, version)).rowcount
        if not changed:
            conn.execute("UPDATE semantic_documents SET claim='',lease_until=0 WHERE document_id=? AND claim=?",
                         (document_id, claim))
            return False
        if not reason:
            if offset == 0:
                conn.execute("DELETE FROM semantic_vectors WHERE document_id=?", (document_id,))
            conn.executemany("INSERT OR REPLACE INTO semantic_vectors VALUES (?,?,?)", rows)
    return not reason


async def run_worker() -> None:
    while True:
        try:
            indexed = await asyncio.to_thread(index_next_batch)
        except sqlite3.Error:
            logger.exception("Could not update semantic index")
            indexed = False
        await asyncio.sleep(0.3 if indexed else 5)


def search(document_id: str, query: str, limit: int) -> list[tuple[str, float]]:
    if not configured():
        return []
    with get_db_connection() as conn:
        enabled, min_similarity = read_settings(conn)
        if not enabled:
            return []
        rows = conn.execute("""
            SELECT v.chunk_id, v.vector, s.dimension
            FROM semantic_vectors v JOIN semantic_documents s USING (document_id)
            JOIN documents d USING (document_id)
            WHERE v.document_id = ? AND s.model_key = ? AND s.retry_after = 0 AND s.next_offset = -1
                AND d.status = 'completed' AND d.updated_at = s.document_version
        """, (document_id, model_key())).fetchall()
    if not rows:
        return []

    if vector_index.EMBEDDING_MODEL.startswith("qwen3-embedding"):
        query = f"Instruct: Given a question, retrieve relevant passages from the document\nQuery: {query}"
    vectors = search_embedding(query)
    dimension = rows[0]["dimension"]
    if not vectors or vectors[0].size != dimension:
        return []
    valid = [row for row in rows if len(row["vector"]) == dimension * 4]
    if not valid:
        return []
    matrix = np.vstack([np.frombuffer(row["vector"], dtype=np.float32) for row in valid])
    scores = matrix @ vectors[0]
    return [(valid[i]["chunk_id"], float(scores[i]))
            for i in np.argsort(scores)[::-1][:limit] if scores[i] >= min_similarity]


def search_embedding(query: str):
    global _searches
    deadline = time.monotonic() + SEARCH_SECONDS
    with _gate:
        _searches += 1
    try:
        with _gate:
            if not _gate.wait_for(lambda: not _indexing, timeout=max(0, deadline - time.monotonic())):
                return None
        if not revision_matches():
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return vector_index.remote_embed_batch([query], timeout=remaining)
    finally:
        with _gate:
            _searches -= 1
