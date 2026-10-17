from __future__ import annotations

import logging
import os
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Optional
from uuid import uuid4
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Iterator
from collections.abc import Callable

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from backend.app.config import PROJECT_ROOT
from backend.app.routes.auth import require_session
from backend.app.services.auth_service import Session
from backend.app.services.embedding import get_embedding_service
from backend.app.services.document_registry import get_document_registry
from backend.app.services.llm_service import LLMError
from backend.app.services.rag_service import RAGError, get_rag_service
from backend.app.services.retrieval import get_retrieval_service
from backend.app.services.text_chunker import TextChunker
from backend.app.services.text_cleaner import clean_text
from backend.app.services.text_extractor import (
    MAX_PDF_INPUT_BYTES,
    PDFInputError,
    extract_text,
)
from backend.app.services.text_processor import process_text
from backend.app.services.vector import VectorStoreError, get_vector_store


logger = logging.getLogger(__name__)
router = APIRouter()


class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=8_000)
    top_k: Optional[int] = Field(default=None, ge=1)
    document_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    min_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)


UPLOAD_DIR = PROJECT_ROOT / "storage" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = MAX_PDF_INPUT_BYTES
READ_CHUNK_SIZE = 1024 * 1024
_DOCUMENT_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_DOCUMENT_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class IngestionStats:
    page_count: int
    chunk_count: int
    ocr_page_count: int
    timings_ms: dict[str, int]
    cache_hit: bool
    request_time_ms: int


@contextmanager
def _document_lock(document_id: str) -> Iterator[None]:
    """Prevent two local workers from embedding the same content at once."""
    with _DOCUMENT_LOCKS_GUARD:
        lock, users = _DOCUMENT_LOCKS.get(document_id, (threading.Lock(), 0))
        _DOCUMENT_LOCKS[document_id] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _DOCUMENT_LOCKS_GUARD:
            current_lock, users = _DOCUMENT_LOCKS.get(document_id, (lock, 1))
            if current_lock is lock and users <= 1:
                _DOCUMENT_LOCKS.pop(document_id, None)
            elif current_lock is lock:
                _DOCUMENT_LOCKS[document_id] = (lock, users - 1)


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


def _safe_indexing_error(error: Exception) -> str:
    """Translate known local setup failures into useful, path-free UI guidance."""
    message = str(error).casefold()
    if "tesseract" in message or "ocr extraction failed" in message:
        return (
            "This PDF has scanned pages, but Tesseract OCR is missing or misconfigured. "
            "Install Tesseract and the required language data, add it to PATH, restart "
            "the backend, then retry. Text-based PDFs do not need Tesseract. See the "
            "backend README under Prerequisites for setup instructions."
        )
    if any(marker in message for marker in ("torch_python.dll", "winerror 4551", "application control")):
        return (
            "The local embedding runtime could not start because Windows blocked a "
            "PyTorch library. Ask your administrator to allow the trusted runtime, "
            "then restart the backend and retry. See the backend README for setup help."
        )
    return "Document indexing failed. Check backend setup and logs, then retry the upload."


def calculate_file_hash(file_path: Path) -> str:
    """Return the SHA-256 content hash for a saved upload."""
    file_hash = sha256()
    with file_path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(READ_CHUNK_SIZE), b""):
            file_hash.update(chunk)
    return file_hash.hexdigest()


def _process_and_index(
    file_path: Path,
    document_id: str,
    filename: str,
    progress_callback: Callable[[str, int], None] | None = None,
) -> IngestionStats:
    """Run the CPU and database work outside the async request event loop."""
    report = progress_callback or (lambda stage, progress: None)
    total_started = time.perf_counter()
    extraction_started = time.perf_counter()
    report("extracting", 4)
    extraction_progress = lambda progress: report(
        "extracting", 4 + round(progress * 0.27)
    )
    try:
        extracted_pages = extract_text(file_path, progress_callback=extraction_progress)
    except TypeError as exc:
        if "progress_callback" not in str(exc):
            raise
        extracted_pages = extract_text(file_path)
    extraction_ms = _elapsed_ms(extraction_started)
    if not extracted_pages:
        raise HTTPException(status_code=422, detail="No pages could be extracted from the PDF.")

    report("structuring", 32)
    transform_started = time.perf_counter()
    structured_text: list[dict] = []
    for page in extracted_pages:
        page_number = page.get("page_number")
        cleaned_blocks = []
        for block in page.get("blocks") or []:
            raw_text = block.get("text", "")
            cleaned_block_text = clean_text(raw_text)
            if cleaned_block_text:
                cleaned_blocks.append({**block, "text": cleaned_block_text})
        structured_text.extend(process_text(cleaned_blocks, page_number))
    preprocessing_ms = _elapsed_ms(transform_started)

    if not structured_text:
        raise HTTPException(status_code=422, detail="No usable text was extracted from the PDF.")

    report("chunking", 42)
    chunk_started = time.perf_counter()
    chunks = TextChunker(max_chars=1_000, overlap_chars=100).chunk(structured_text)
    chunking_ms = _elapsed_ms(chunk_started)
    if not chunks:
        raise HTTPException(status_code=422, detail="No text chunks could be created from the PDF.")

    report("embedding", 50)
    embedding_started = time.perf_counter()
    embedding_service = get_embedding_service()
    embedding_texts = [chunk.text for chunk in chunks]
    embedding_progress = lambda done, total: report(
        "embedding", 50 + round(done / max(1, total) * 39)
    )
    try:
        embeddings = embedding_service.embed_batch(
            embedding_texts, progress_callback=embedding_progress
        )
    except TypeError as exc:
        if "progress_callback" not in str(exc):
            raise
        embeddings = embedding_service.embed_batch(embedding_texts)
    embedding_ms = _elapsed_ms(embedding_started)
    if len(embeddings) != len(chunks):
        raise RuntimeError("The embedding count did not match the chunk count.")

    vector_store = get_vector_store()
    # The vector store performs transactional replacement and restores the
    # prior document records if any write in the replacement fails.
    report("storing", 93)
    indexing_started = time.perf_counter()
    stored_count = vector_store.add(
        document_id=document_id,
        chunks=chunks,
        embeddings=embeddings,
        filename=filename,
    )
    indexing_ms = _elapsed_ms(indexing_started)

    if stored_count != len(chunks):
        raise RuntimeError("Stored chunk count did not match the generated chunk count.")
    report("optimizing", 95)
    retrieval_warm_started = time.perf_counter()
    retrieval_service = get_retrieval_service()
    retrieval_service.invalidate_document(document_id)
    retrieval_service.warm_document(document_id)
    retrieval_warm_ms = _elapsed_ms(retrieval_warm_started)
    report("finalizing", 98)

    return IngestionStats(
        page_count=len(extracted_pages),
        chunk_count=stored_count,
        ocr_page_count=sum(bool(page.get("ocr_used")) for page in extracted_pages),
        timings_ms={
            "extraction_ms": extraction_ms,
            "preprocessing_ms": preprocessing_ms,
            "chunking_ms": chunking_ms,
            "embedding_ms": embedding_ms,
            "indexing_ms": indexing_ms,
            "retrieval_warm_ms": retrieval_warm_ms,
            "total_ms": _elapsed_ms(total_started),
        },
        cache_hit=False,
        request_time_ms=_elapsed_ms(total_started),
    )


def _publish_upload(temporary_path: Path, document_id: str, total_size: int) -> None:
    final_path = UPLOAD_DIR / f"{document_id}.pdf"
    try:
        os.link(temporary_path, final_path)
    except FileExistsError:
        if (
            not final_path.is_file()
            or final_path.stat().st_size != total_size
            or calculate_file_hash(final_path) != document_id
        ):
            raise RuntimeError("An invalid file already occupies the document path.")
    except OSError as exc:
        logger.exception("Could not persist validated upload %s", document_id)
        raise RuntimeError("Could not persist the validated PDF upload.") from exc


def _ensure_indexed(
    file_path: Path,
    document_id: str,
    filename: str,
    total_size: int,
) -> IngestionStats:
    """Reuse durable vectors, including indexes made before the registry existed."""
    request_started = time.perf_counter()
    registry = get_document_registry()
    vector_store = get_vector_store()

    with _document_lock(document_id):
        record = registry.get_record(document_id)
        has_vectors = vector_store.has_document(document_id)
        vector_info = None
        is_legacy_index = False
        if has_vectors and (
            record is None
            or (record.status == "indexing" and record.chunk_count == 0)
        ):
            vector_info = vector_store.document_info(document_id)
            # Older Chroma records have no filename. Adopt those once; a new
            # orphaned index carries filename metadata and must be repaired.
            is_legacy_index = not vector_info.get("filename")

        if has_vectors and (
            (record is not None and record.status in {"indexed", "indexing"} and record.chunk_count > 0)
            or is_legacy_index
        ):
            if record and record.status in {"indexed", "indexing"} and record.chunk_count > 0:
                page_count = record.page_count
                chunk_count = record.chunk_count
                ocr_page_count = record.ocr_page_count
                timings = dict(record.timings_ms)
                canonical_filename = (
                    filename
                    if record.filename == f"{document_id}.pdf"
                    else record.filename
                )
            else:
                vector_info = vector_info or vector_store.document_info(document_id)
                chunk_count = vector_info["chunk_count"]
                page_count = vector_info["page_count"]
                ocr_page_count = 0
                timings = {}
                canonical_filename = (
                    (record.filename if record else None)
                    or vector_info.get("filename")
                    or registry.filename_for(document_id)
                    or filename
                )
                if canonical_filename == f"{document_id}.pdf":
                    canonical_filename = filename

            _publish_upload(file_path, document_id, total_size)
            registry.update_indexing_progress(document_id, "optimizing", 95)
            get_retrieval_service().warm_document(document_id)
            registry.register_document(
                document_id=document_id,
                filename=canonical_filename,
                file_size=total_size,
                page_count=page_count,
                chunk_count=chunk_count,
                ocr_page_count=ocr_page_count,
                timings_ms=timings,
                cache_hit=True,
            )
            registry.add_filename_alias(document_id, filename)
            return IngestionStats(
                page_count=page_count,
                chunk_count=chunk_count,
                ocr_page_count=ocr_page_count,
                timings_ms={"cache_lookup_ms": _elapsed_ms(request_started)},
                cache_hit=True,
                request_time_ms=_elapsed_ms(request_started),
            )

        registry.begin_indexing(document_id, filename, total_size)
        try:
            last_progress = {"stage": "", "progress": 0}

            def publish_progress(stage: str, progress: int) -> None:
                progress = max(1, min(99, int(progress)))
                if (
                    stage != last_progress["stage"]
                    or progress >= last_progress["progress"] + 2
                ):
                    registry.update_indexing_progress(document_id, stage, progress)
                    last_progress.update(stage=stage, progress=progress)

            stats = _process_and_index(
                file_path, document_id, filename, progress_callback=publish_progress
            )
            _publish_upload(file_path, document_id, total_size)
            registry.register_document(
                document_id=document_id,
                filename=filename,
                file_size=total_size,
                page_count=stats.page_count,
                chunk_count=stats.chunk_count,
                ocr_page_count=stats.ocr_page_count,
                timings_ms=stats.timings_ms,
            )
            return stats
        except Exception as exc:
            try:
                registry.mark_indexing_failed(document_id, _safe_indexing_error(exc))
            except Exception:
                logger.exception("Could not record failed document indexing status")
            raise


def _run_indexing_job(file_path: Path, document_id: str, filename: str, total_size: int) -> None:
    """Process the already-persisted PDF after the upload response is sent."""
    try:
        _ensure_indexed(file_path, document_id, filename, total_size)
    except Exception as exc:
        logger.exception("Background document indexing failed for %s", document_id)
        try:
            get_document_registry().mark_indexing_failed(
                document_id, _safe_indexing_error(exc)
            )
        except Exception:
            logger.exception("Could not record failed document indexing status")


@router.post("/upload", status_code=202)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session: Session = Depends(require_session),
):
    temporary_path: Path | None = None

    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="Filename is required.")
        original_filename = Path(file.filename.replace("\\", "/")).name
        if not original_filename or len(original_filename) > 255:
            raise HTTPException(status_code=400, detail="Invalid filename.")
        if any(ord(character) < 32 or ord(character) == 127 for character in original_filename):
            raise HTTPException(status_code=400, detail="Invalid filename.")
        if Path(original_filename).suffix.lower() != ".pdf":
            raise HTTPException(status_code=400, detail="Only PDF files are allowed.")

        temporary_path = UPLOAD_DIR / f".{uuid4().hex}.upload"
        total_size = 0
        pdf_signature = b""
        with temporary_path.open("xb") as output_file:
            while chunk := await file.read(READ_CHUNK_SIZE):
                if not pdf_signature:
                    pdf_signature = chunk[:8]
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE:
                    raise HTTPException(status_code=413, detail="File size must not exceed 10 MB.")
                output_file.write(chunk)

        if total_size == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        if not pdf_signature.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF.")

        document_id = calculate_file_hash(temporary_path)
        _publish_upload(temporary_path, document_id, total_size)
        registry = get_document_registry()
        registry.begin_indexing(document_id, original_filename, total_size)
        registry.grant_access(document_id, session.owner_keys)
        background_tasks.add_task(
            _run_indexing_job,
            UPLOAD_DIR / f"{document_id}.pdf",
            document_id,
            original_filename,
            total_size,
        )

        return {
            "status": "processing",
            "document_id": document_id,
            "filename": original_filename,
            "content_type": "application/pdf",
            "size": total_size,
            "stage": "queued",
            "progress": 1,
        }
    except HTTPException:
        raise
    except PDFInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Document upload processing failed")
        raise HTTPException(status_code=500, detail="Document processing failed.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary upload %s", temporary_path)
        try:
            await file.close()
        except Exception:
            logger.warning("Could not close the uploaded file handle")


@router.get("/{document_id}/status")
def document_status(document_id: str, session: Session = Depends(require_session)):
    """Return durable ingestion progress and metrics for an uploaded PDF."""
    registry = get_document_registry()
    if not registry.has_access(document_id, session.owner_keys):
        raise HTTPException(status_code=404, detail="Document is not available in this session.")
    record = registry.get_record(document_id)
    if record is None:
        try:
            vector_info = get_vector_store().document_info(document_id)
        except VectorStoreError as exc:
            raise HTTPException(status_code=503, detail="Document index is unavailable.") from exc
        if vector_info["chunk_count"]:
            source_path = UPLOAD_DIR / f"{document_id}.pdf"
            try:
                file_size = source_path.stat().st_size
            except OSError:
                file_size = 0
            filename = (
                vector_info.get("filename")
                or registry.filename_for(document_id)
                or f"{document_id}.pdf"
            )
            record = registry.register_document(
                document_id=document_id,
                filename=filename,
                file_size=file_size,
                page_count=vector_info["page_count"],
                chunk_count=vector_info["chunk_count"],
                cache_hit=True,
            )
    if record is None:
        raise HTTPException(status_code=404, detail="Document is not registered as indexed.")
    return {
        "document_id": record.document_id,
        "filename": record.filename,
        "size": record.size,
        "page_count": record.page_count,
        "chunk_count": record.chunk_count,
        "ocr_page_count": record.ocr_page_count,
        "timings_ms": record.timings_ms,
        "indexed_at": record.indexed_at,
        "last_seen_at": record.last_seen_at,
        "cache_hits": record.cache_hits,
        "status": record.status,
        "stage": record.stage,
        "progress": record.progress,
        "error": record.error,
    }


@router.get("/{document_id}/chunks/{chunk_id}")
def get_source_chunk(document_id: str, chunk_id: int, session: Session = Depends(require_session)):
    """Return the exact cited passage for a compact source link."""
    registry = get_document_registry()
    if not registry.has_access(document_id, session.owner_keys):
        raise HTTPException(status_code=404, detail="Document is not available in this session.")
    try:
        chunk = get_vector_store().get_chunk(document_id, chunk_id)
    except VectorStoreError as exc:
        raise HTTPException(status_code=503, detail="Document source is unavailable.") from exc
    if chunk is None:
        raise HTTPException(status_code=404, detail="Source passage was not found.")
    return {
        "document_id": chunk.document_id,
        "chunk_id": chunk.chunk_id,
        "filename": chunk.filename or registry.filename_for(document_id),
        "page_number": chunk.page_number,
        "heading": chunk.heading,
        "text": chunk.text,
    }


@router.post(
    "/ask",
    responses={
        503: {
            "description": "The document answer service is temporarily unavailable.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "The document answer service is temporarily unavailable."
                    }
                }
            },
        }
    },
)
def ask_documents(request: AskRequest, session: Session = Depends(require_session)):
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="Query must not be empty.")
    if request.document_id is not None and not request.document_id.strip():
        raise HTTPException(status_code=400, detail="document_id must not be empty.")
    if request.document_id is None:
        raise HTTPException(status_code=422, detail="Select a document before asking a question.")
    registry = get_document_registry()
    if not registry.has_access(request.document_id, session.owner_keys):
        raise HTTPException(status_code=404, detail="Document is not available in this session.")
    record = registry.get_record(request.document_id)
    if record is None or record.status != "indexed":
        raise HTTPException(status_code=409, detail="Document indexing is not complete yet.")

    try:
        response = get_rag_service().answer(
            query=request.query,
            top_k=request.top_k,
            document_id=request.document_id,
            min_score=request.min_score,
        )
    except (RAGError, LLMError) as exc:
        logger.warning("Document answer request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="The document answer service is temporarily unavailable.",
        ) from exc
    return response.to_dict()
