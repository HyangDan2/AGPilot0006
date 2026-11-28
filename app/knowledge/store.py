"""Revisioned artifact storage for the Knowledge Engine.

WP-08 requires Workspace storage, atomic writes, revisions, optimistic concurrency, rollback
and integrity checks without standing up a second storage engine. This module is the AGTools
side of that: a small content store whose every write is atomic and revision-checked. AGCore
does not copy bytes into its own blob store for artifacts it generated here; it records a
reference (see `app/knowledge/artifacts.py` in AGPilot0005).

Layout
------
``{root}/{document_id}/meta.json``                 metadata (written atomically)
``{root}/{document_id}/revisions/{n:04d}/{name}``  immutable revision payloads
``{root}/{document_id}/.lock``                     cross-process write lock
``{root}/index.sqlite3``                           search index (FTS5 when available)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.config import settings
from app.knowledge.contracts import (
    CONTRACT_VERSION,
    DocumentRef,
    IntegrityCheck,
    KnowledgeError,
    KnowledgeErrorCode,
    RevisionStatus,
    compute_content_hash,
    normalize_mime,
    utc_now_iso,
)

try:  # POSIX (Raspberry Pi / Linux deployments)
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

try:  # Windows fallback for the same lock
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore

_PROCESS_LOCKS: Dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def knowledge_root() -> Path:
    override = os.getenv("HD2_KNOWLEDGE_DIR")
    root = Path(override) if override else settings.DATA_DIR / "knowledge"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(name: str) -> str:
    cleaned = os.path.basename(name or "document").strip().replace("\x00", "")
    cleaned = "".join(ch for ch in cleaned if ch.isalnum() or ch in "._- ()[]가-힣" ).strip()
    cleaned = cleaned[:120].strip(". ") or "document"
    return cleaned


def _process_lock(path: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(path, threading.Lock())


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Cross-thread and cross-process exclusive lock around one document's metadata."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = _process_lock(str(lock_path))
    with lock:
        handle = open(lock_path, "a+b")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` so a reader never observes a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:  # pragma: no cover - platforms without directory fsync
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class KnowledgeStore:
    """Content-addressed, revision-immutable document store."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else knowledge_root()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def document_dir(self, document_id: str) -> Path:
        return self.root / _safe_name(document_id)

    def meta_path(self, document_id: str) -> Path:
        return self.document_dir(document_id) / "meta.json"

    def revision_path(self, document_id: str, revision: int, name: str = "") -> Path:
        base = self.document_dir(document_id) / "revisions" / f"{revision:04d}"
        candidates = sorted(base.glob("*")) if base.exists() else []
        if candidates:
            return candidates[0]
        return base / (_safe_name(name) or "document.bin")

    # ------------------------------------------------------------------ metadata
    def _load_meta(self, document_id: str) -> Dict[str, Any]:
        path = self.meta_path(document_id)
        if not path.exists():
            raise KnowledgeError(
                KnowledgeErrorCode.DOCUMENT_NOT_FOUND, document_id, detail={"document_id": document_id}
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeError(
                KnowledgeErrorCode.CORRUPT_CONTENT,
                "metadata unreadable",
                detail={"document_id": document_id, "reason": type(exc).__name__},
            ) from exc

    def _save_meta(self, document_id: str, meta: Dict[str, Any]) -> None:
        _atomic_write(
            self.meta_path(document_id),
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )

    def exists(self, document_id: str) -> bool:
        return self.meta_path(document_id).exists()

    def ref(self, document_id: str, revision: Optional[int] = None) -> DocumentRef:
        meta = self._load_meta(document_id)
        target = meta["current_revision"] if revision is None else int(revision)
        entry = next((r for r in meta["revisions"] if r["revision"] == target), None)
        if entry is None:
            raise KnowledgeError(
                KnowledgeErrorCode.REVISION_NOT_FOUND,
                f"revision {target} not found",
                detail={"document_id": document_id, "revision": target},
            )
        status = RevisionStatus.CURRENT if target == meta["current_revision"] else RevisionStatus.SUPERSEDED
        if meta.get("deleted"):
            status = RevisionStatus.DELETED
        return DocumentRef(
            document_id=document_id,
            name=entry.get("name") or meta.get("name") or document_id,
            mime_type=entry["mime_type"],
            revision=target,
            content_hash=entry["content_hash"],
            size_bytes=entry["size_bytes"],
            status=status,
            version=meta.get("current_revision"),
            metadata=dict(entry.get("metadata") or {}),
        )

    def list_documents(self, limit: int = 200) -> List[DocumentRef]:
        refs: List[DocumentRef] = []
        for meta_path in sorted(self.root.glob("*/meta.json")):
            document_id = meta_path.parent.name
            try:
                refs.append(self.ref(document_id))
            except KnowledgeError:
                continue
            if len(refs) >= limit:
                break
        return refs

    def read_bytes(self, document_id: str, revision: Optional[int] = None) -> Tuple[bytes, DocumentRef]:
        ref = self.ref(document_id, revision)
        path = self.revision_path(document_id, ref.revision, ref.name)
        if not path.exists():
            raise KnowledgeError(
                KnowledgeErrorCode.CORRUPT_CONTENT,
                "revision payload missing",
                detail={"document_id": document_id, "revision": ref.revision},
            )
        return path.read_bytes(), ref

    # ------------------------------------------------------------------ writes
    def create(
        self,
        *,
        name: str,
        mime_type: str,
        data: bytes,
        metadata: Optional[Dict[str, Any]] = None,
        document_id: Optional[str] = None,
    ) -> DocumentRef:
        if not data:
            raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "empty payload")
        doc_id = document_id or f"kd_{uuid.uuid4().hex[:16]}"
        with _file_lock(self.document_dir(doc_id) / ".lock"):
            if self.meta_path(doc_id).exists():
                raise KnowledgeError(
                    KnowledgeErrorCode.REVISION_CONFLICT,
                    "document already exists",
                    detail={"document_id": doc_id},
                )
            entry = self._write_revision_payload(
                doc_id, 1, name, mime_type, data, metadata or {}
            )
            meta = {
                "contract_version": CONTRACT_VERSION,
                "document_id": doc_id,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "current_revision": 1,
                "revisions": [entry],
            }
            # meta.json is the commit point: the payload is already durable here.
            self._save_meta(doc_id, meta)
        return self.ref(doc_id)

    def _write_revision_payload(
        self,
        document_id: str,
        revision: int,
        name: str,
        mime_type: str,
        data: bytes,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        stored_name = _safe_name(name)
        target = self.document_dir(document_id) / "revisions" / f"{revision:04d}" / stored_name
        _atomic_write(target, data)
        return {
            "revision": revision,
            "name": stored_name,
            "mime_type": normalize_mime(mime_type, filename=stored_name),
            "size_bytes": len(data),
            "content_hash": compute_content_hash(data),
            "sha256_hex": compute_content_hash(data).split(":", 1)[1],
            "created_at": utc_now_iso(),
            "metadata": metadata,
        }

    def write_revision(
        self,
        document_id: str,
        data: bytes,
        *,
        expected_revision: Optional[int],
        name: Optional[str] = None,
        mime_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DocumentRef:
        """Append a new revision, refusing to overwrite another writer's work."""

        with _file_lock(self.document_dir(document_id) / ".lock"):
            meta = self._load_meta(document_id)
            current = int(meta["current_revision"])
            if expected_revision is not None and int(expected_revision) != current:
                raise KnowledgeError(
                    KnowledgeErrorCode.REVISION_CONFLICT,
                    f"expected revision {expected_revision}, current is {current}",
                    detail={"expected": int(expected_revision), "current": current},
                    retryable=True,
                )
            previous = next(r for r in meta["revisions"] if r["revision"] == current)
            entry = self._write_revision_payload(
                document_id,
                current + 1,
                name or previous.get("name") or document_id,
                mime_type or previous["mime_type"],
                data,
                {**dict(previous.get("metadata") or {}), **(metadata or {})},
            )
            meta["revisions"].append(entry)
            meta["current_revision"] = current + 1
            meta["updated_at"] = utc_now_iso()
            self._save_meta(document_id, meta)
            self._prune(document_id, meta)
        return self.ref(document_id)

    def _prune(self, document_id: str, meta: Dict[str, Any]) -> None:
        keep = int(getattr(settings, "KNOWLEDGE_MAX_REVISIONS", 20) or 20)
        revisions = meta["revisions"]
        if len(revisions) <= keep:
            return
        drop = revisions[:-keep]
        meta["revisions"] = revisions[-keep:]
        self._save_meta(document_id, meta)
        for entry in drop:
            path = self.document_dir(document_id) / "revisions" / f"{entry['revision']:04d}"
            shutil.rmtree(path, ignore_errors=True)

    def rollback(self, document_id: str, to_revision: int, *, expected_revision: Optional[int]) -> DocumentRef:
        """Reinstate an older payload as a new revision (history is never rewritten)."""

        data, ref = self.read_bytes(document_id, to_revision)
        return self.write_revision(
            document_id,
            data,
            expected_revision=expected_revision,
            name=ref.name,
            mime_type=ref.mime_type,
            metadata={"rollback_from": expected_revision, "rollback_to": to_revision},
        )

    def delete(self, document_id: str) -> None:
        with _file_lock(self.document_dir(document_id) / ".lock"):
            meta = self._load_meta(document_id)
            meta["deleted"] = True
            meta["updated_at"] = utc_now_iso()
            self._save_meta(document_id, meta)
        self.remove_from_index(document_id)

    # ------------------------------------------------------------------ integrity
    def validate(self, document_id: str, revision: Optional[int] = None) -> Tuple[DocumentRef, List[IntegrityCheck]]:
        ref = self.ref(document_id, revision)
        checks: List[IntegrityCheck] = []
        path = self.revision_path(document_id, ref.revision, ref.name)
        checks.append(IntegrityCheck(name="payload_present", ok=path.exists(), detail=str(path.name)))
        if not path.exists():
            return ref, checks
        data = path.read_bytes()
        actual = compute_content_hash(data)
        checks.append(
            IntegrityCheck(
                name="content_hash",
                ok=actual == ref.content_hash,
                detail="" if actual == ref.content_hash else f"expected {ref.content_hash}",
            )
        )
        checks.append(
            IntegrityCheck(
                name="size",
                ok=len(data) == ref.size_bytes,
                detail=f"{len(data)} bytes",
            )
        )
        if ref.mime_type.endswith(("document", "sheet", "presentation")):
            import zipfile
            import io

            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    bad = archive.testzip()
                checks.append(IntegrityCheck(name="office_archive", ok=bad is None, detail=bad or "ok"))
            except zipfile.BadZipFile as exc:
                checks.append(IntegrityCheck(name="office_archive", ok=False, detail=str(exc)))
        return ref, checks

    # ------------------------------------------------------------------ search index
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.root / "index.sqlite3"), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def init_index(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    document_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    chunk_id TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    content TEXT NOT NULL,
                    name TEXT,
                    mime_type TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (document_id, chunk_id)
                )
                """
            )
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5("
                    "chunk_id UNINDEXED, document_id UNINDEXED, content, tokenize='unicode61')"
                )
            except sqlite3.OperationalError:
                # FTS5 is optional: `search_chunks` falls back to LIKE scans.
                pass

    def index_document(
        self,
        ref: DocumentRef,
        chunks: List[Dict[str, Any]],
    ) -> None:
        self.init_index()
        with self._connect() as conn:
            conn.execute("DELETE FROM knowledge_chunks WHERE document_id=?", (ref.document_id,))
            try:
                conn.execute("DELETE FROM knowledge_fts WHERE document_id=?", (ref.document_id,))
            except sqlite3.OperationalError:
                pass
            now = utc_now_iso()
            for chunk in chunks:
                conn.execute(
                    "INSERT OR REPLACE INTO knowledge_chunks"
                    "(document_id, revision, chunk_id, locator, content, name, mime_type, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        ref.document_id,
                        ref.revision,
                        chunk["chunk_id"],
                        json.dumps(chunk["locator"], ensure_ascii=False),
                        chunk["content"],
                        ref.name,
                        ref.mime_type,
                        now,
                    ),
                )
                try:
                    conn.execute(
                        "INSERT INTO knowledge_fts(chunk_id, document_id, content) VALUES (?,?,?)",
                        (chunk["chunk_id"], ref.document_id, chunk["content"]),
                    )
                except sqlite3.OperationalError:
                    pass

    def remove_from_index(self, document_id: str) -> None:
        if not (self.root / "index.sqlite3").exists():
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM knowledge_chunks WHERE document_id=?", (document_id,))
            try:
                conn.execute("DELETE FROM knowledge_fts WHERE document_id=?", (document_id,))
            except sqlite3.OperationalError:
                pass

    def index_is_ready(self, document_id: str, revision: int) -> bool:
        if not (self.root / "index.sqlite3").exists():
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM knowledge_chunks WHERE document_id=? AND revision=?",
                (document_id, revision),
            ).fetchone()
        return bool(row and row["c"])

    def search_chunks(
        self,
        query: str,
        *,
        top_k: int,
        document_ids: Optional[List[str]] = None,
        mime_types: Optional[List[str]] = None,
        mode: str = "keyword",
    ) -> List[Dict[str, Any]]:
        """Keyword search with an FTS5 path and a deterministic LIKE fallback."""

        self.init_index()
        terms = [term for term in query.replace('"', " ").split() if term.strip()]
        if not terms:
            return []
        filters = ""
        params: List[Any] = []
        if document_ids:
            filters += " AND document_id IN (%s)" % ",".join("?" for _ in document_ids)
            params.extend(document_ids)
        if mime_types:
            filters += " AND mime_type IN (%s)" % ",".join("?" for _ in mime_types)
            params.extend(mime_types)
        rows: List[sqlite3.Row] = []
        with self._connect() as conn:
            try:
                fts_query = " AND ".join(f'"{term}"' for term in terms[:8])
                rows = conn.execute(
                    "SELECT c.chunk_id, c.document_id, c.revision, c.locator, c.content, c.name, "
                    "c.mime_type, bm25(knowledge_fts) AS rank "
                    "FROM knowledge_fts JOIN knowledge_chunks c ON c.chunk_id = knowledge_fts.chunk_id "
                    f"WHERE knowledge_fts MATCH ?{filters} ORDER BY rank LIMIT ?",
                    [fts_query, *params, top_k * 3],
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if not rows:
                clauses = " AND ".join("content LIKE ?" for _ in terms[:8])
                like_params = [f"%{term}%" for term in terms[:8]]
                rows = conn.execute(
                    "SELECT chunk_id, document_id, revision, locator, content, name, mime_type, 0.0 AS rank "
                    f"FROM knowledge_chunks WHERE {clauses}{filters} LIMIT ?",
                    [*like_params, *params, top_k * 3],
                ).fetchall()
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            key = f"{row['document_id']}:{row['chunk_id']}"
            if key in seen:
                continue
            seen.add(key)
            rank = float(row["rank"] or 0.0)
            score = 1.0 / (1.0 + abs(rank)) if rank else 0.5
            results.append(
                {
                    "chunk_id": row["chunk_id"],
                    "document_id": row["document_id"],
                    "revision": int(row["revision"]),
                    "locator": json.loads(row["locator"] or "{}"),
                    "content": row["content"],
                    "name": row["name"],
                    "mime_type": row["mime_type"],
                    "score": score,
                    "matched_by": ["keyword"],
                }
            )
            if len(results) >= top_k:
                break
        return results

    def stats(self) -> Dict[str, int]:
        documents = len(list(self.root.glob("*/meta.json")))
        chunks = 0
        if (self.root / "index.sqlite3").exists():
            try:
                with self._connect() as conn:
                    row = conn.execute("SELECT COUNT(*) AS c FROM knowledge_chunks").fetchone()
                    chunks = int(row["c"]) if row else 0
            except sqlite3.Error:
                chunks = 0
        return {"documents": documents, "chunks": chunks}
