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
    """Initialize the SQLite metadata schema and indexes."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_hash ON documents(file_hash);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_status ON documents(status);")
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
        parser_version: str = "1.0.0"
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO documents (
                    document_id, filename, file_hash, doc_type, mime_type,
                    file_size, status, parser_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """, (document_id, filename, file_hash, doc_type, mime_type, file_size, parser_version, now, now))
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
    def find_by_hash(file_hash: str) -> Optional[Dict[str, Any]]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM documents WHERE file_hash = ? AND status = 'completed'", (file_hash,))
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
    def list_documents(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM documents ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))
            return [dict(row) for row in cursor.fetchall()]
