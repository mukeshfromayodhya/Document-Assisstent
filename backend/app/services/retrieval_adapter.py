"""Retrieval adapter shared by the search API and the RAG pipeline."""

from __future__ import annotations

import math
from typing import Any, Optional

from backend.app.config import RETRIEVAL_SETTINGS, RetrievalSettings
from backend.app.services.retrieval import (
    RetrievalService,
    get_retrieval_service,
)
from backend.app.services.embedding import get_embedding_service


class Retriever:
    """Retrieve ranked chunks using the application's shared retrieval layer.

    ``collection`` keeps the original collection-based API available for
    existing callers. New code should omit it so the configured retrieval
    service, embedding singleton, and vector-store validation are reused.
    """

    MAX_QUERY_CHARS = 8_000

    def __init__(
        self,
        collection: Any | None = None,
        embedding_service: Any | None = None,
        *,
        retrieval_service: RetrievalService | None = None,
        settings: Optional[RetrievalSettings] = None,
    ) -> None:
        if collection is not None and retrieval_service is not None:
            raise ValueError("Pass either collection or retrieval_service, not both.")

        self.collection = collection
        self.embedding_service = embedding_service
        self.retrieval_service = retrieval_service
        self.settings = settings or RETRIEVAL_SETTINGS

        if self.collection is None and self.retrieval_service is None:
            self.retrieval_service = get_retrieval_service()

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        document_id: str | None = None,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return ranked chunks with citation metadata and similarity scores."""

        query = self._validate_query(query)
        top_k = self._validate_top_k(top_k)
        document_id = self._validate_document_id(document_id)
        min_score = self._validate_min_score(min_score)

        if self.collection is None:
            assert self.retrieval_service is not None
            chunks = self.retrieval_service.retrieve(
                query=query,
                top_k=top_k,
                document_id=document_id,
                min_score=min_score,
            )
            return [self._from_service_chunk(chunk) for chunk in chunks]

        return self._retrieve_collection(
            query=query,
            top_k=top_k,
            document_id=document_id,
            min_score=min_score,
        )

    @staticmethod
    def _validate_query(query: str) -> str:
        if not isinstance(query, str):
            raise TypeError("Query must be a string.")
        query = query.strip()
        if not query:
            raise ValueError("Query cannot be empty.")
        if len(query) > Retriever.MAX_QUERY_CHARS:
            raise ValueError(
                f"Query must not exceed {Retriever.MAX_QUERY_CHARS} characters."
            )
        return query

    def _validate_top_k(self, top_k: int | None) -> int:
        if top_k is None:
            top_k = self.settings.default_top_k
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise TypeError("top_k must be an integer.")
        if top_k < 1:
            raise ValueError("top_k must be at least 1.")
        return min(top_k, self.settings.max_top_k)

    @staticmethod
    def _validate_document_id(document_id: str | None) -> str | None:
        if document_id is None:
            return None
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("document_id must be a non-empty string when supplied.")
        return document_id.strip()

    @staticmethod
    def _validate_min_score(min_score: float | None) -> float | None:
        if min_score is None:
            return None
        if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
            raise TypeError("min_score must be a number.")
        min_score = float(min_score)
        if not math.isfinite(min_score) or not 0.0 <= min_score <= 1.0:
            raise ValueError("min_score must be between 0.0 and 1.0.")
        return min_score

    def _retrieve_collection(
        self,
        *,
        query: str,
        top_k: int,
        document_id: str | None,
        min_score: float | None,
    ) -> list[dict[str, Any]]:
        embedding_service = self.embedding_service or get_embedding_service()
        embed = getattr(embedding_service, "embed_text", None)
        if not callable(embed):
            # Retain support for older injected embedders while the bundled
            # EmbeddingService uses the explicit ``embed_text`` method.
            embed = getattr(embedding_service, "embed", None)
        if not callable(embed):
            raise TypeError("Embedding service must provide embed_text().")

        query_embedding = embed(query)
        available_count = getattr(self.collection, "count", None)
        if callable(available_count):
            count = available_count()
            if isinstance(count, int) and count <= 0:
                return []
            if isinstance(count, int):
                top_k = min(top_k, count)
        query_args: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": top_k,
        }
        if document_id is not None:
            query_args["where"] = {"document_id": document_id}

        raw = self.collection.query(**query_args)
        ids = self._first_result_row(raw.get("ids"))
        documents = self._first_result_row(raw.get("documents"))
        metadatas = self._first_result_row(raw.get("metadatas"))
        distances = self._first_result_row(raw.get("distances"))

        results: list[dict[str, Any]] = []
        for index, vector_id in enumerate(ids):
            text = documents[index] if index < len(documents) else ""
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = distances[index] if index < len(distances) else None
            result = self._from_collection_record(
                vector_id=vector_id,
                text=text,
                metadata=metadata,
                distance=distance,
            )
            if result is None:
                continue
            if min_score is not None and result["score"] < min_score:
                continue
            results.append(result)

        results.sort(key=lambda item: (-item["score"], self._chunk_sort_key(item["chunk_id"])))
        return results

    @staticmethod
    def _first_result_row(value: Any) -> list[Any]:
        if not value:
            return []
        if isinstance(value, list) and value and isinstance(value[0], list):
            return value[0]
        return value if isinstance(value, list) else []

    @classmethod
    def _from_service_chunk(cls, chunk: Any) -> dict[str, Any]:
        metadata = {
            "document_id": cls._value(chunk, "document_id", ""),
            "chunk_id": cls._value(chunk, "chunk_id", None),
            "heading": cls._value(chunk, "heading", None),
            "page_number": cls._value(chunk, "page_number", None),
            "filename": cls._value(chunk, "filename", None),
        }
        score = cls._score(cls._value(chunk, "score", 0.0))
        return {
            "chunk_id": metadata["chunk_id"],
            "document_id": metadata["document_id"],
            "text": cls._value(chunk, "text", ""),
            "heading": metadata["heading"],
            "page_number": metadata["page_number"],
            "filename": metadata["filename"],
            "score": score,
            "distance": 1.0 - score,
            "metadata": metadata,
        }

    @classmethod
    def _from_collection_record(
        cls,
        *,
        vector_id: Any,
        text: Any,
        metadata: Any,
        distance: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(metadata, dict):
            metadata = {}
        if not isinstance(text, str):
            text = ""
        try:
            numeric_distance = float(distance)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric_distance):
            return None

        score = cls._score(1.0 - numeric_distance)
        chunk_id = metadata.get("chunk_id", vector_id)
        page_number = metadata.get("page_number")
        if page_number == -1:
            page_number = None
        normalized_metadata = {
            "document_id": metadata.get("document_id", ""),
            "chunk_id": chunk_id,
            "heading": metadata.get("heading") or None,
            "page_number": page_number,
            "filename": metadata.get("filename") or None,
        }
        return {
            "chunk_id": chunk_id,
            "document_id": normalized_metadata["document_id"],
            "text": text,
            "heading": normalized_metadata["heading"],
            "page_number": page_number,
            "filename": normalized_metadata["filename"],
            "score": score,
            "distance": numeric_distance,
            "metadata": normalized_metadata,
        }

    @staticmethod
    def _value(chunk: Any, name: str, default: Any) -> Any:
        if isinstance(chunk, dict):
            return chunk.get(name, default)
        return getattr(chunk, name, default)

    @staticmethod
    def _score(value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(score):
            return 0.0
        return max(0.0, min(1.0, score))

    @staticmethod
    def _chunk_sort_key(chunk_id: Any) -> tuple[int, Any]:
        try:
            return (0, int(chunk_id))
        except (TypeError, ValueError):
            return (1, str(chunk_id))


def get_retriever(collection: Any | None = None) -> Retriever:
    """Create a retriever, optionally adapting a supplied Chroma collection."""

    return Retriever(collection=collection)
