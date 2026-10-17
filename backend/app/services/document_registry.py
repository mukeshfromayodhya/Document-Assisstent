"""Durable metadata for indexed PDFs, independent of Chroma's internal DB."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

_DOCUMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    filename: str
    size: int
    page_count: int
    chunk_count: int
    ocr_page_count: int
    timings_ms: dict[str, int]
    indexed_at: str
    last_seen_at: str
    cache_hits: int
    status: str
    stage: str = "ready"
    progress: int = 100
    error: str | None = None


class DocumentRegistry:
    """Persist filenames and ingestion facts so uploads can be reused."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        uploads_dir: str | Path | None = None,
    ) -> None:
        self.database_path = Path(
            database_path or PROJECT_ROOT / "storage" / "document_registry.sqlite3"
        ).expanduser().resolve()
        self.uploads_dir = Path(
            uploads_dir or PROJECT_ROOT / "storage" / "uploads"
        ).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._filename_cache: dict[str, str | None] = {}
        self._filename_cache_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=20.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    page_count INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    ocr_page_count INTEGER NOT NULL DEFAULT 0,
                    timings_ms_json TEXT NOT NULL DEFAULT '{}',
                    indexed_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    cache_hits INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'indexed',
                    stage TEXT NOT NULL DEFAULT 'ready',
                    progress INTEGER NOT NULL DEFAULT 100,
                    error TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(documents)").fetchall()
            }
            if "status" not in columns:
                connection.execute(
                    "ALTER TABLE documents ADD COLUMN status TEXT NOT NULL DEFAULT 'indexed'"
                )
            if "stage" not in columns:
                connection.execute(
                    "ALTER TABLE documents ADD COLUMN stage TEXT NOT NULL DEFAULT 'ready'"
                )
            if "progress" not in columns:
                connection.execute(
                    "ALTER TABLE documents ADD COLUMN progress INTEGER NOT NULL DEFAULT 100"
                )
            if "error" not in columns:
                connection.execute("ALTER TABLE documents ADD COLUMN error TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS document_filenames (
                    document_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY (document_id, filename)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_document_filenames_id "
                "ON document_filenames(document_id, first_seen_at)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS document_access (
                    document_id TEXT NOT NULL,
                    owner_key TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    PRIMARY KEY(document_id, owner_key)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_document_access_owner "
                "ON document_access(owner_key, document_id)"
            )

    def grant_access(self, document_id: str, owner_keys: tuple[str, ...] | list[str]) -> None:
        """Grant a guest/account session access to an indexed document."""
        if not document_id or not owner_keys:
            return
        now = self._now()
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO document_access(document_id, owner_key, granted_at) VALUES (?, ?, ?)",
                [(document_id, owner_key, now) for owner_key in owner_keys if isinstance(owner_key, str) and owner_key],
            )

    def has_access(self, document_id: str, owner_keys: tuple[str, ...] | list[str]) -> bool:
        """Return true when at least one active session owner was granted this document."""
        if not document_id or not owner_keys:
            return False
        placeholders = ",".join("?" for _ in owner_keys)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM document_access WHERE document_id = ? AND owner_key IN ({placeholders}) LIMIT 1",
                (document_id, *owner_keys),
            ).fetchone()
        return row is not None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        if not isinstance(filename, str):
            raise ValueError("filename must be a string")
        normalized = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if (
            not normalized
            or len(normalized) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        ):
            raise ValueError("filename is invalid")
        return normalized

    def _record_from_row(self, row: sqlite3.Row) -> DocumentRecord:
        try:
            timings = json.loads(row["timings_ms_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            timings = {}
        if not isinstance(timings, dict):
            timings = {}
        normalized_timings = {
            str(key): max(0, int(value))
            for key, value in timings.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        return DocumentRecord(
            document_id=row["document_id"],
            filename=row["filename"],
            size=max(0, int(row["file_size"])),
            page_count=max(0, int(row["page_count"])),
            chunk_count=max(0, int(row["chunk_count"])),
            ocr_page_count=max(0, int(row["ocr_page_count"])),
            timings_ms=normalized_timings,
            indexed_at=row["indexed_at"],
            last_seen_at=row["last_seen_at"],
            cache_hits=max(0, int(row["cache_hits"])),
            status=str(row["status"] or "indexed"),
            stage=str(row["stage"] or "ready"),
            progress=max(0, min(100, int(row["progress"]))),
            error=str(row["error"]) if row["error"] else None,
        )

    def get_record(self, document_id: str) -> DocumentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return self._record_from_row(row) if row else None

    def add_filename_alias(self, document_id: str, filename: str) -> None:
        safe_name = self._safe_filename(filename)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO document_filenames "
                "(document_id, filename, first_seen_at) VALUES (?, ?, ?)",
                (document_id, safe_name, self._now()),
            )

    def begin_indexing(self, document_id: str, filename: str, file_size: int) -> None:
        """Record an in-progress ingest before writing vectors, for crash recovery."""
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("document_id must be a non-empty string")
        if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
            raise ValueError("file_size must be a non-negative integer")
        safe_name = self._safe_filename(filename)
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO document_filenames "
                "(document_id, filename, first_seen_at) VALUES (?, ?, ?)",
                (document_id, safe_name, now),
            )
            alias = connection.execute(
                "SELECT filename FROM document_filenames WHERE document_id = ? "
                "ORDER BY rowid LIMIT 1",
                (document_id,),
            ).fetchone()
            canonical_name = alias["filename"] if alias else safe_name
            connection.execute(
                """INSERT INTO documents
                   (document_id, filename, file_size, page_count, chunk_count,
                    ocr_page_count, timings_ms_json, indexed_at, last_seen_at,
                    cache_hits, status, stage, progress, error)
                   VALUES (?, ?, ?, 0, 0, 0, '{}', ?, ?, 0, 'indexing', 'queued', 1, NULL)
                   ON CONFLICT(document_id) DO UPDATE SET
                       file_size = excluded.file_size,
                       last_seen_at = excluded.last_seen_at,
                       stage = CASE WHEN documents.status = 'indexing' THEN documents.stage ELSE 'queued' END,
                       progress = CASE WHEN documents.status = 'indexing' THEN documents.progress ELSE 1 END,
                       status = 'indexing', error = NULL""",
                (document_id, canonical_name, file_size, now, now),
            )
        with self._filename_cache_lock:
            self._filename_cache[document_id] = canonical_name

    def update_indexing_progress(self, document_id: str, stage: str, progress: int) -> None:
        """Persist a coarse, user-safe ingestion stage for the upload tracker."""
        if not isinstance(stage, str) or not stage or len(stage) > 40:
            raise ValueError("stage must be a short non-empty string")
        if isinstance(progress, bool) or not isinstance(progress, int):
            raise ValueError("progress must be an integer")
        progress = max(1, min(99, progress))
        with self._connect() as connection:
            connection.execute(
                "UPDATE documents SET status = 'indexing', stage = ?, progress = ?, "
                "error = NULL, last_seen_at = ? WHERE document_id = ?",
                (stage, progress, self._now(), document_id),
            )

    def mark_indexing_failed(
        self, document_id: str, error: str = "Document processing failed. Retry the upload."
    ) -> None:
        """Persist a safe explanation while retaining the stage that failed."""
        if not isinstance(error, str):
            error = "Document processing failed. Retry the upload."
        error = error.strip()[:500] or "Document processing failed. Retry the upload."
        with self._connect() as connection:
            connection.execute(
                "UPDATE documents SET status = 'failed', error = ?, "
                "last_seen_at = ? WHERE document_id = ?",
                (error, self._now(), document_id),
            )

    def register_document(
        self,
        *,
        document_id: str,
        filename: str,
        file_size: int,
        page_count: int,
        chunk_count: int,
        ocr_page_count: int = 0,
        timings_ms: dict[str, int] | None = None,
        cache_hit: bool = False,
    ) -> DocumentRecord:
        """Store or refresh an indexed document and retain uploaded name aliases."""
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("document_id must be a non-empty string")
        safe_name = self._safe_filename(filename)
        values = (file_size, page_count, chunk_count, ocr_page_count)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("document metadata counts must be non-negative integers")
        safe_timings = {
            str(key): max(0, int(value))
            for key, value in (timings_ms or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO document_filenames "
                "(document_id, filename, first_seen_at) VALUES (?, ?, ?)",
                (document_id, safe_name, now),
            )
            existing = connection.execute(
                "SELECT * FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            first_name = connection.execute(
                "SELECT filename FROM document_filenames WHERE document_id = ? "
                "ORDER BY rowid LIMIT 1",
                (document_id,),
            ).fetchone()
            canonical_name = first_name["filename"] if first_name else safe_name
            fallback_name = f"{document_id}.pdf"
            if canonical_name == fallback_name and safe_name != fallback_name:
                canonical_name = safe_name

            if existing:
                if existing["filename"] == fallback_name and safe_name != fallback_name:
                    connection.execute(
                        "UPDATE documents SET filename = ? WHERE document_id = ?",
                        (safe_name, document_id),
                    )
                if cache_hit:
                    connection.execute(
                        """UPDATE documents
                           SET file_size = ?,
                               page_count = CASE WHEN ? > 0 THEN ? ELSE page_count END,
                               chunk_count = CASE WHEN ? > 0 THEN ? ELSE chunk_count END,
                               last_seen_at = ?, status = 'indexed', stage = 'ready',
                               progress = 100, error = NULL,
                               cache_hits = cache_hits + 1
                           WHERE document_id = ?""",
                        (
                            file_size,
                            page_count,
                            page_count,
                            chunk_count,
                            chunk_count,
                            now,
                            document_id,
                        ),
                    )
                else:
                    connection.execute(
                        """UPDATE documents
                           SET file_size = ?, page_count = ?, chunk_count = ?,
                               ocr_page_count = ?, timings_ms_json = ?, last_seen_at = ?,
                               status = 'indexed', stage = 'ready', progress = 100, error = NULL
                           WHERE document_id = ?""",
                        (
                            file_size,
                            page_count,
                            chunk_count,
                            ocr_page_count,
                            json.dumps(safe_timings, separators=(",", ":")),
                            now,
                            document_id,
                        ),
                    )
            else:
                connection.execute(
                    """INSERT INTO documents
                       (document_id, filename, file_size, page_count, chunk_count,
                        ocr_page_count, timings_ms_json, indexed_at, last_seen_at, cache_hits,
                        status, stage, progress, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'indexed', 'ready', 100, NULL)""",
                    (
                        document_id,
                        canonical_name,
                        file_size,
                        page_count,
                        chunk_count,
                        ocr_page_count,
                        json.dumps(safe_timings, separators=(",", ":")),
                        now,
                        now,
                        1 if cache_hit else 0,
                    ),
                )

        with self._filename_cache_lock:
            self._filename_cache[document_id] = canonical_name
        record = self.get_record(document_id)
        if record is None:  # pragma: no cover - transaction guarantees the row
            raise RuntimeError("Document metadata was not persisted.")
        return record

    def filename_for(self, document_id: str) -> str | None:
        """Return the original filename, recovering old indexes when possible."""
        if not isinstance(document_id, str) or not document_id:
            return None
        with self._filename_cache_lock:
            if document_id in self._filename_cache:
                return self._filename_cache[document_id]

        record = self.get_record(document_id)
        if record and record.filename:
            filename = record.filename
        else:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT filename FROM document_filenames WHERE document_id = ? "
                    "ORDER BY rowid LIMIT 1",
                    (document_id,),
                ).fetchone()
            filename = row["filename"] if row else self._recover_legacy_filename(document_id)
            if filename and not row:
                self._remember_filename(document_id, filename)

        with self._filename_cache_lock:
            self._filename_cache[document_id] = filename
        return filename

    def _remember_filename(self, document_id: str, filename: str) -> None:
        self.add_filename_alias(document_id, filename)

    def _recover_legacy_filename(self, document_id: str) -> str | None:
        if not _DOCUMENT_ID_PATTERN.fullmatch(document_id):
            return None
        hashed_path = self.uploads_dir / f"{document_id}.pdf"
        try:
            hashed_size = hashed_path.stat().st_size
        except OSError:
            return None

        for candidate in sorted(self.uploads_dir.glob("*.pdf"), key=lambda path: path.name.casefold()):
            if candidate == hashed_path or candidate.stem == document_id:
                continue
            try:
                if candidate.stat().st_size != hashed_size:
                    continue
                digest = hashlib.sha256()
                with candidate.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() == document_id:
                    logger.info("Recovered legacy filename metadata for document %s", document_id)
                    return candidate.name
            except OSError:
                continue
        return hashed_path.name


_instance: DocumentRegistry | None = None
_instance_lock = threading.Lock()


def get_document_registry() -> DocumentRegistry:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = DocumentRegistry()
    return _instance
