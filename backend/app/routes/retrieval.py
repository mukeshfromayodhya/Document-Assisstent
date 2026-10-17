from typing import Optional

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.config import RETRIEVAL_SETTINGS
from backend.app.routes.auth import require_session
from backend.app.services.auth_service import Session
from backend.app.services.document_registry import get_document_registry
from backend.app.services.retrieval import (
    RetrievalError,
    RetrievalInputError,
    get_retrieval_service,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=8_000, description="The user's question or search text.")
    top_k: Optional[int] = Field(
        None, ge=1, description="How many chunks to return. Defaults to the server's configured top_k."
    )
    document_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=256,
        description="Restrict the search to one document. Omit to search all documents.",
    )
    min_score: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Optional similarity floor in [0, 1]. Results below it are dropped."
    )


@router.post("/search")
def search(request: RetrieveRequest, session: Session = Depends(require_session)):
    """
    Query -> query embedding -> vector similarity search -> ranked chunks.

    Route holds no search logic itself — it only validates the HTTP
    boundary and delegates to RetrievalService, per the project's
    route -> service separation used in app/routes/documents.py.
    """
    if not request.query.strip():
        raise HTTPException(status_code=422, detail="Query must not be empty.")
    if not request.document_id:
        raise HTTPException(status_code=422, detail="Select a document before searching.")
    registry = get_document_registry()
    if not registry.has_access(request.document_id, session.owner_keys):
        raise HTTPException(status_code=404, detail="Document is not available in this session.")
    record = registry.get_record(request.document_id)
    if record is None or record.status != "indexed":
        raise HTTPException(status_code=409, detail="Document indexing is not complete yet.")

    effective_top_k = min(
        request.top_k if request.top_k is not None else RETRIEVAL_SETTINGS.default_top_k,
        RETRIEVAL_SETTINGS.max_top_k,
    )
    service = get_retrieval_service()
    try:
        chunks = service.retrieve(
            query=request.query,
            top_k=request.top_k,
            document_id=request.document_id,
            min_score=request.min_score,
        )
    except RetrievalInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RetrievalError as exc:
        logger.warning("Retrieval request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="The retrieval service is temporarily unavailable.",
        ) from exc

    return {
        "query": request.query,
        "top_k": effective_top_k,
        "document_id": request.document_id,
        "results": [
            {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "filename": chunk.filename,
                "text": chunk.text,
                "page_number": chunk.page_number,
                "heading": chunk.heading,
                "score": round(chunk.score, 4),
            }
            for chunk in chunks
        ],
    }
