import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from app.config import settings

def get_db_connection() -> sqlite3.Connection:
    settings.init_directories()
    conn = sqlite3.connect(settings.DATABASE_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    """Initialize the SQLite metadata schema, migrations, and indexes."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Main documents table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                status TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                page_count INTEGER DEFAULT 0,
                section_count INTEGER DEFAULT 0,
                figure_count INTEGER DEFAULT 0,
                error_message TEXT,
                owner_id TEXT DEFAULT 'default_user',
                tenant_id TEXT DEFAULT 'default_tenant',
                access_scope TEXT DEFAULT 'private',
                cache_key TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        
        # Safely run migrations for missing columns in existing tables
        cursor.execute("PRAGMA table_info(documents);")
        existing_cols = {row["name"] for row in cursor.fetchall()}
        
        migrations = [
            ("owner_id", "TEXT DEFAULT 'default_user'"),
            ("tenant_id", "TEXT DEFAULT 'default_tenant'"),
            ("access_scope", "TEXT DEFAULT 'private'"),
            ("cache_key", "TEXT"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE documents ADD COLUMN {col_name} {col_def};")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_hash ON documents(file_hash);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_cache_key ON documents(cache_key);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_status ON documents(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_owner_tenant ON documents(tenant_id, owner_id);")

        # 2. Persistent jobs queue table (Work Order A-4)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                heartbeat_at TEXT,
                finished_at TEXT,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_status ON jobs(status);")

        # 3. SQLite FTS5 table for Chunk Keyword Search (Work Order B-6)
        try:
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
                    chunk_id,
                    document_id,
                    section_id,
                    content,
                    tokenize='unicode61'
                );
            """)
        except sqlite3.OperationalError:
            # FTS5 fallback if disabled in custom sqlite builds
            pass

        conn.commit()

class DocumentRepository:
    @staticmethod
    def create_document(
        document_id: str,
        filename: str,
        file_hash: str,
        doc_type: str,
        mime_type: str,
        file_size: int,
        parser_version: str = "2.0.0",
        owner_id: str = "default_user",
        tenant_id: str = "default_tenant",
        access_scope: str = "private",
        cache_key: Optional[str] = None
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO documents (
                    document_id, filename, file_hash, doc_type, mime_type,
                    file_size, status, parser_version, owner_id, tenant_id,
                    access_scope, cache_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
            """, (
                document_id, filename, file_hash, doc_type, mime_type,
                file_size, parser_version, owner_id, tenant_id,
                access_scope, cache_key, now, now
            ))
            conn.commit()
            return DocumentRepository.get_document(document_id)

    @staticmethod
    def get_document(document_id: str) -> Optional[Dict[str, Any]]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    def find_cached_document(cache_key: str, tenant_id: str) -> Optional[Dict[str, Any]]:
        """
        Find completed document by immutable cache_key within same tenant.
        Ensures tenant isolation (Work Order A-3).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM documents 
                WHERE cache_key = ? AND status = 'completed' AND tenant_id = ?
                ORDER BY created_at DESC LIMIT 1
            """, (cache_key, tenant_id))
            row = cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    def update_status(
        document_id: str,
        status: str,
        page_count: int = 0,
        section_count: int = 0,
        figure_count: int = 0,
        error_message: Optional[str] = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE documents
                SET status = ?, page_count = ?, section_count = ?, figure_count = ?,
                    error_message = ?, updated_at = ?
                WHERE document_id = ?
            """, (status, page_count, section_count, figure_count, error_message, now, document_id))
            conn.commit()

    @staticmethod
    def list_documents(tenant_id: str, owner_id: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if owner_id:
                cursor.execute("""
                    SELECT * FROM documents 
                    WHERE tenant_id = ? AND owner_id = ? 
                    ORDER BY created_at DESC LIMIT ? OFFSET ?
                """, (tenant_id, owner_id, limit, offset))
            else:
                cursor.execute("""
                    SELECT * FROM documents 
                    WHERE tenant_id = ? 
                    ORDER BY created_at DESC LIMIT ? OFFSET ?
                """, (tenant_id, limit, offset))
            return [dict(row) for row in cursor.fetchall()]

class JobRepository:
    @staticmethod
    def create_job(job_id: str, document_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO jobs (job_id, document_id, status, created_at)
                VALUES (?, ?, 'queued', ?)
            """, (job_id, document_id, now))
            conn.commit()

    @staticmethod
    def mark_started(job_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE jobs SET status = 'processing', started_at = ?, heartbeat_at = ?
                WHERE job_id = ?
            """, (now, now, job_id))
            conn.commit()

    @staticmethod
    def mark_completed(job_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE jobs SET status = 'completed', finished_at = ?
                WHERE job_id = ?
            """, (now, job_id))
            conn.commit()

    @staticmethod
    def mark_failed(job_id: str, error_message: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE jobs SET status = 'failed', finished_at = ?, error_message = ?
                WHERE job_id = ?
            """, (now, error_message, job_id))
            conn.commit()

    @staticmethod
    def recover_interrupted_jobs() -> List[str]:
        """
        On server startup, detect jobs that were in 'processing' or 'queued' state
        when the server stopped, and recover their status (Work Order A-4).
        """
        now = datetime.now(timezone.utc).isoformat()
        recovered_doc_ids = []
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT job_id, document_id FROM jobs WHERE status IN ('processing', 'queued')")
            rows = cursor.fetchall()
            for row in rows:
                j_id, d_id = row["job_id"], row["document_id"]
                cursor.execute("""
                    UPDATE jobs SET status = 'failed', finished_at = ?,
                    error_message = 'Interrupted by server restart'
                    WHERE job_id = ?
                """, (now, j_id))
                cursor.execute("""
                    UPDATE documents SET status = 'failed',
                    error_message = 'Interrupted by server restart', updated_at = ?
                    WHERE document_id = ? AND status IN ('processing', 'queued')
                """, (now, d_id))
                recovered_doc_ids.append(d_id)
            conn.commit()
        return recovered_doc_ids
