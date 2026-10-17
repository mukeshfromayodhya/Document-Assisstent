from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Optional

from backend.app.config import VECTOR_STORE_SETTINGS, VectorStoreSettings
from backend.app.services.text_chunker import TextChunk

logger = logging.getLogger(__name__)


class VectorStoreError(Exception):
    """
    Raised for vector-store validation, database, or consistency failures.
    """


@dataclass
class SearchResult:
    """
    Application-level representation of a vector search result.
    """

    chunk_id: int
    document_id: str
    text: str
    heading: Optional[str]
    page_number: Optional[int]
    score: float
    extra: dict = field(default_factory=dict)
    filename: Optional[str] = None


def _vector_id(document_id: str, chunk_id: int) -> str:
    """
    Create a collision-safe Chroma ID.

    The document ID is part of the key because chunk IDs are only unique
    inside a document.
    """

    return f"{document_id}::{chunk_id}"


class VectorStore:
    """
    Chroma-backed vector store for the document RAG pipeline.

    Responsibilities:

    - validate vector-store input
    - insert/update document chunks
    - replace documents without leaving stale chunks
    - perform similarity search
    - filter search by document
    - delete documents
    - count stored chunks
    """

    def __init__(
        self,
        settings: Optional[VectorStoreSettings] = None,
    ) -> None:

        self.settings = settings or VECTOR_STORE_SETTINGS

        try:
            import chromadb
        except ImportError as exc:
            raise VectorStoreError(
                "chromadb is not installed. "
                "Install it inside the project virtual environment."
            ) from exc

        try:

            self._client = chromadb.PersistentClient(
                path=self.settings.persist_directory
            )

            self._collection = (
                self._client.get_or_create_collection(
                    name=self.settings.collection_name,
                    metadata={
                        "hnsw:space": "cosine"
                    },
                )
            )

            metadata = self._collection.metadata or {}
            distance_space = metadata.get("hnsw:space")
            if distance_space is None:
                configuration = getattr(self._collection, "configuration", None)
                if isinstance(configuration, dict):
                    distance_space = configuration.get("hnsw", {}).get("space")
            # Chroma's default space is L2. Existing collections are returned
            # as-is by get_or_create_collection, so the requested metadata above
            # does not guarantee that a pre-existing collection uses cosine.
            distance_space = distance_space or "l2"
            if distance_space != "cosine":
                raise VectorStoreError(
                    f"Collection '{self.settings.collection_name}' uses "
                    f"{distance_space!r}; this store requires cosine distance. "
                    "Create a cosine collection and re-index its documents."
                )

        except Exception as exc:

            if isinstance(exc, VectorStoreError):
                raise

            raise VectorStoreError(
                "Failed to initialize vector database at "
                f"'{self.settings.persist_directory}': {exc}"
            ) from exc

        # Protect write operations within this process.
        #
        # Chroma itself is persistent, but our replace operation consists
        # of multiple operations:
        #
        #     upsert -> get -> delete
        #
        # so we protect that sequence from concurrent local writers.
        self._write_lock = threading.RLock()

        logger.info(
            "Vector store initialized: collection=%s path=%s",
            self.settings.collection_name,
            self.settings.persist_directory,
        )

    # ======================================================================
    # Internal validation helpers
    # ======================================================================

    @staticmethod
    def _validate_document_id(document_id: str) -> str:
        if not isinstance(document_id, str):
            raise VectorStoreError(
                "document_id must be a string."
            )

        document_id = document_id.strip()

        if not document_id:
            raise VectorStoreError(
                "document_id must not be empty."
            )

        return document_id

    @staticmethod
    def _validate_embedding(
        vector: list[float],
        *,
        chunk_id: int,
        expected_dimension: Optional[int] = None,
    ) -> None:

        if not isinstance(vector, list):
            raise VectorStoreError(
                f"Embedding for chunk_id={chunk_id} "
                "must be a list."
            )

        if not vector:
            raise VectorStoreError(
                f"Empty embedding vector for chunk_id={chunk_id}."
            )

        if expected_dimension is not None:
            if len(vector) != expected_dimension:
                raise VectorStoreError(
                    f"Embedding dimension mismatch for "
                    f"chunk_id={chunk_id}: "
                    f"got {len(vector)}, expected "
                    f"{expected_dimension}."
                )

        for value in vector:

            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise VectorStoreError(
                    f"Embedding for chunk_id={chunk_id} "
                    "contains a non-numeric value."
                )

            if not math.isfinite(float(value)):
                raise VectorStoreError(
                    f"Embedding for chunk_id={chunk_id} "
                    "contains NaN or infinity."
                )

    @staticmethod
    def _validate_chunk(
        chunk: TextChunk,
    ) -> None:

        if not isinstance(chunk, TextChunk):
            raise VectorStoreError(
                "Chunk must be a TextChunk instance."
            )

        if not isinstance(chunk.text, str):
            raise VectorStoreError(
                f"chunk_id={chunk.chunk_id} text must be a string."
            )

        if not chunk.text.strip():
            raise VectorStoreError(
                f"chunk_id={chunk.chunk_id} "
                "contains empty text."
            )

        if isinstance(chunk.chunk_id, bool) or not isinstance(chunk.chunk_id, int):
            raise VectorStoreError(
                f"chunk_id={chunk.chunk_id!r} "
                "must be an integer."
            )

        if chunk.chunk_id < 0:
            raise VectorStoreError(
                f"chunk_id={chunk.chunk_id} "
                "must not be negative."
            )

    def _prepare_records(
        self,
        document_id: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
        filename: Optional[str] = None,
    ) -> tuple[
        list[str],
        list[list[float]],
        list[str],
        list[dict],
    ]:

        document_id = self._validate_document_id(
            document_id
        )
        if filename is not None:
            if not isinstance(filename, str) or not filename.strip() or len(filename) > 255:
                raise VectorStoreError("filename must be a non-empty string of at most 255 characters.")
            filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
            if not filename or any(ord(character) < 32 or ord(character) == 127 for character in filename):
                raise VectorStoreError("filename is invalid.")

        if not chunks:
            raise VectorStoreError(
                "Cannot store zero chunks."
            )

        if not embeddings:
            raise VectorStoreError(
                "Cannot store zero embeddings."
            )

        if len(chunks) != len(embeddings):
            raise VectorStoreError(
                f"chunks ({len(chunks)}) and embeddings "
                f"({len(embeddings)}) counts must match."
            )

        ids: list[str] = []
        vectors: list[list[float]] = []
        documents: list[str] = []
        metadatas: list[dict] = []

        seen_ids: set[str] = set()

        expected_dimension: Optional[int] = None

        for chunk, vector in zip(
            chunks,
            embeddings,
        ):

            self._validate_chunk(chunk)

            if expected_dimension is None:
                self._validate_embedding(vector, chunk_id=chunk.chunk_id)
                expected_dimension = len(vector)
            else:
                self._validate_embedding(
                    vector,
                    chunk_id=chunk.chunk_id,
                    expected_dimension=expected_dimension,
                )

            vector_id = _vector_id(
                document_id,
                chunk.chunk_id,
            )

            # Prevent duplicate chunk IDs inside one ingestion.
            if vector_id in seen_ids:
                raise VectorStoreError(
                    f"Duplicate chunk ID detected: "
                    f"{chunk.chunk_id} for document "
                    f"'{document_id}'."
                )

            seen_ids.add(vector_id)

            ids.append(vector_id)
            vectors.append(vector)
            documents.append(chunk.text)

            metadatas.append(
                {
                    "document_id": document_id,
                    "chunk_id": chunk.chunk_id,
                    "heading": chunk.heading or "",
                    "filename": filename or "",
                    "page_number": (
                        chunk.page_number
                        if chunk.page_number is not None
                        else -1
                    ),
                }
            )

        return (
            ids,
            vectors,
            documents,
            metadatas,
        )

    def _collection_dimension(self) -> Optional[int]:
        """Return the dimension already stored in the collection, if any."""
        sample = self._collection.get(limit=1, include=["embeddings"])
        embeddings = sample.get("embeddings")
        if embeddings is None or len(embeddings) == 0:
            return None
        vector = embeddings[0]
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        try:
            dimension = len(vector)
        except TypeError as exc:
            raise VectorStoreError("The collection contains an invalid embedding.") from exc
        if dimension < 1:
            raise VectorStoreError("The collection contains an empty embedding.")
        return dimension

    @staticmethod
    def _plain_vector(vector: object) -> list[float]:
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        if not isinstance(vector, (list, tuple)):
            raise VectorStoreError("Could not snapshot an existing embedding for rollback.")
        try:
            return [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise VectorStoreError("Could not snapshot an existing embedding for rollback.") from exc

    # ======================================================================
    # Write path
    # ======================================================================

    def add(
        self,
        document_id: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
        filename: Optional[str] = None,
    ) -> int:
        """
        Store a complete document.

        This intentionally delegates to replace_document() so callers
        using the older add() API cannot accidentally leave stale chunks.
        """

        return self.replace_document(
            document_id=document_id,
            chunks=chunks,
            embeddings=embeddings,
            filename=filename,
        )

    def replace_document(
        self,
        document_id: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
        filename: Optional[str] = None,
    ) -> int:
        """
        Replace all vectors belonging to one document.

        Existing records are snapshotted before the first write. If any
        write, verification, or stale-record deletion fails, the new partial
        state is removed and the prior records are restored.
        """

        (
            ids,
            vectors,
            documents,
            metadatas,
        ) = self._prepare_records(
            document_id,
            chunks,
            embeddings,
            filename,
        )

        document_id = self._validate_document_id(
            document_id
        )

        with self._write_lock:
            old_records: list[tuple[str, list[float], str, dict]] = []
            mutation_started = False
            try:
                expected_dimension = len(vectors[0])
                stored_dimension = self._collection_dimension()
                if stored_dimension is not None and stored_dimension != expected_dimension:
                    raise VectorStoreError(
                        f"Embedding dimension mismatch: collection has {stored_dimension}, "
                        f"new model produced {expected_dimension}. Rebuild the collection "
                        "with the intended embedding model before indexing."
                    )

                previous = self._collection.get(
                    where={"document_id": document_id},
                    include=["embeddings", "documents", "metadatas"],
                )
                previous_ids = previous.get("ids", [])
                previous_embeddings = previous.get("embeddings")
                previous_documents = previous.get("documents")
                previous_metadatas = previous.get("metadatas")
                if previous_ids:
                    if (
                        previous_embeddings is None
                        or previous_documents is None
                        or previous_metadatas is None
                        or not (
                            len(previous_ids)
                            == len(previous_embeddings)
                            == len(previous_documents)
                            == len(previous_metadatas)
                        )
                    ):
                        raise VectorStoreError(
                            "Could not snapshot all existing document records for rollback."
                        )
                    for index, old_id in enumerate(previous_ids):
                        old_metadata = previous_metadatas[index]
                        old_document = previous_documents[index]
                        if not isinstance(old_metadata, dict) or not isinstance(old_document, str):
                            raise VectorStoreError(
                                "Could not snapshot all existing document records for rollback."
                            )
                        old_records.append(
                            (
                                old_id,
                                self._plain_vector(previous_embeddings[index]),
                                old_document,
                                old_metadata,
                            )
                        )

                batch_size_getter = getattr(self._client, "get_max_batch_size", None)
                max_batch_size = (
                    int(batch_size_getter())
                    if callable(batch_size_getter)
                    else min(len(ids), 5_000)
                )
                if max_batch_size < 1:
                    raise VectorStoreError("Chroma reported an invalid maximum batch size.")
                mutation_started = True
                for start in range(0, len(ids), max_batch_size):
                    end = start + max_batch_size
                    self._collection.upsert(
                        ids=ids[start:end],
                        embeddings=vectors[start:end],
                        documents=documents[start:end],
                        metadatas=metadatas[start:end],
                    )

                existing = self._collection.get(
                    where={"document_id": document_id},
                    include=[],
                )
                existing_ids = set(existing.get("ids", []))
                new_ids = set(ids)
                stale_ids = existing_ids - new_ids
                if stale_ids:
                    self._collection.delete(ids=list(stale_ids))
                    logger.info(
                        "Removed %d stale chunks from document_id=%s",
                        len(stale_ids),
                        document_id,
                    )

                verified = self._collection.get(
                    where={"document_id": document_id},
                    include=[],
                )
                if set(verified.get("ids", [])) != new_ids:
                    raise VectorStoreError(
                        "Vector store did not retain the complete replacement record set."
                    )

            except Exception as exc:
                if mutation_started:
                    try:
                        current = self._collection.get(
                            where={"document_id": document_id},
                            include=[],
                        )
                        current_ids = current.get("ids", [])
                        if current_ids:
                            self._collection.delete(ids=current_ids)

                        if old_records:
                            batch_size_getter = getattr(
                                self._client, "get_max_batch_size", None
                            )
                            restore_batch_size = (
                                int(batch_size_getter())
                                if callable(batch_size_getter)
                                else min(len(old_records), 5_000)
                            )
                            if restore_batch_size < 1:
                                raise VectorStoreError(
                                    "Chroma reported an invalid rollback batch size."
                                )
                            for start in range(0, len(old_records), restore_batch_size):
                                batch = old_records[start : start + restore_batch_size]
                                self._collection.upsert(
                                    ids=[record[0] for record in batch],
                                    embeddings=[record[1] for record in batch],
                                    documents=[record[2] for record in batch],
                                    metadatas=[record[3] for record in batch],
                                )
                    except Exception as rollback_exc:
                        logger.exception(
                            "Vector replacement rollback failed for document_id=%s",
                            document_id,
                        )
                        raise VectorStoreError(
                            f"Failed to replace document '{document_id}', and rollback "
                            "could not restore the previous records."
                        ) from rollback_exc
                raise VectorStoreError(
                    f"Failed to replace document '{document_id}': {exc}"
                ) from exc

        logger.info(
            "Stored %d chunks for document_id=%s",
            len(ids),
            document_id,
        )

        return len(ids)

    # ======================================================================
    # Search
    # ======================================================================

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        document_id: Optional[str] = None,
    ) -> list[SearchResult]:
        """
        Perform cosine similarity search.

        If document_id is supplied, only chunks belonging to that document
        are searched.
        """

        if not isinstance(query_embedding, list):
            raise VectorStoreError(
                "query_embedding must be a list."
            )

        if not query_embedding:
            raise VectorStoreError(
                "query_embedding must not be empty."
            )

        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise VectorStoreError(
                "top_k must be an integer."
            )

        if top_k < 1:
            raise VectorStoreError(
                "top_k must be >= 1."
            )

        if document_id is not None:
            document_id = self._validate_document_id(
                document_id
            )

        # Validate query vector values.
        self._validate_embedding(
            query_embedding,
            chunk_id=-1,
        )

        where = (
            {"document_id": document_id}
            if document_id
            else None
        )

        try:

            stored_dimension = self._collection_dimension()
            if stored_dimension is not None and stored_dimension != len(query_embedding):
                raise VectorStoreError(
                    f"Query embedding dimension {len(query_embedding)} does not match "
                    f"the collection dimension {stored_dimension}. Rebuild the "
                    "collection with the intended embedding model before querying."
                )

            # Avoid asking Chroma for more records than exist.
            collection_count = self._collection.count()

            if collection_count == 0:
                return []

            requested_k = min(
                top_k,
                collection_count,
            )

            raw = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=requested_k,
                where=where,
            )

        except Exception as exc:

            raise VectorStoreError(
                f"Vector search failed: {exc}"
            ) from exc

        ids = raw.get("ids") or [[]]
        docs = raw.get("documents") or [[]]
        metas = raw.get("metadatas") or [[]]
        dists = raw.get("distances") or [[]]

        ids = ids[0] if ids else []
        docs = docs[0] if docs else []
        metas = metas[0] if metas else []
        dists = dists[0] if dists else []

        results: list[SearchResult] = []

        for vector_id, text, meta, distance in zip(
            ids,
            docs,
            metas,
            dists,
        ):

            if meta is None:
                meta = {}

            if distance is None:
                continue

            try:
                distance = float(distance)
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring result %s with invalid distance=%r",
                    vector_id,
                    distance,
                )
                continue

            if not math.isfinite(distance):
                continue

            # Chroma cosine distance:
            #
            # identical vectors -> 0
            # increasingly different -> larger distance
            #
            # Convert to similarity and clamp numerical noise.
            score = max(
                0.0,
                min(
                    1.0,
                    1.0 - distance,
                ),
            )

            chunk_id = meta.get(
                "chunk_id"
            )

            if chunk_id is None:
                logger.warning(
                    "Skipping vector %s because chunk_id "
                    "metadata is missing.",
                    vector_id,
                )
                continue

            try:
                chunk_id = int(chunk_id)
            except (TypeError, ValueError):
                logger.warning(
                    "Skipping vector %s because chunk_id "
                    "is invalid: %r",
                    vector_id,
                    chunk_id,
                )
                continue

            page_number = meta.get(
                "page_number",
                -1,
            )

            if page_number == -1:
                page_number = None

            results.append(
                SearchResult(
                    chunk_id=chunk_id,
                    document_id=meta.get(
                        "document_id",
                        "",
                    ),
                    text=text or "",
                    heading=(
                        meta.get("heading")
                        or None
                    ),
                    page_number=page_number,
                    score=score,
                    filename=(meta.get("filename") or None),
                )
            )

        return results

    # ======================================================================
    # Management
    # ======================================================================

    def delete(
        self,
        document_id: str,
    ) -> int:
        """
        Delete every vector belonging to one document.

        Returns the number of deleted chunks.
        """

        document_id = self._validate_document_id(
            document_id
        )

        with self._write_lock:

            try:

                existing = self._collection.get(
                    where={
                        "document_id": document_id
                    },
                    include=[],
                )

                ids = existing.get(
                    "ids",
                    [],
                )

                if not ids:
                    return 0

                self._collection.delete(
                    ids=ids
                )

            except Exception as exc:

                raise VectorStoreError(
                    f"Failed to delete document "
                    f"'{document_id}': {exc}"
                ) from exc

        logger.info(
            "Deleted %d chunks for document_id=%s",
            len(ids),
            document_id,
        )

        return len(ids)

    def count(
        self,
        document_id: Optional[str] = None,
    ) -> int:
        """
        Return total number of chunks.

        If document_id is provided, return only that document's count.
        """

        if document_id is None:

            try:
                return self._collection.count()

            except Exception as exc:

                raise VectorStoreError(
                    f"Failed to count vector records: {exc}"
                ) from exc

        document_id = self._validate_document_id(
            document_id
        )

        try:

            existing = self._collection.get(
                where={
                    "document_id": document_id
                },
                include=[],
            )

            return len(
                existing.get(
                    "ids",
                    [],
                )
            )

        except Exception as exc:

            raise VectorStoreError(
                f"Failed to count document "
                f"'{document_id}': {exc}"
            ) from exc

    def has_document(self, document_id: str) -> bool:
        """Check indexed-document presence without materializing all chunk IDs."""
        document_id = self._validate_document_id(document_id)
        try:
            existing = self._collection.get(
                where={"document_id": document_id},
                limit=1,
                include=[],
            )
            return bool(existing.get("ids"))
        except Exception as exc:
            raise VectorStoreError(
                f"Failed to inspect document '{document_id}': {exc}"
            ) from exc

    def document_info(self, document_id: str) -> dict:
        """Return lightweight metadata for an already indexed document."""
        document_id = self._validate_document_id(document_id)
        try:
            existing = self._collection.get(
                where={"document_id": document_id},
                include=["metadatas"],
            )
        except Exception as exc:
            raise VectorStoreError(
                f"Failed to inspect document '{document_id}': {exc}"
            ) from exc

        metadatas = existing.get("metadatas") or []
        page_numbers: set[int] = set()
        filename: Optional[str] = None
        for metadata in metadatas:
            if not isinstance(metadata, dict):
                continue
            candidate_name = metadata.get("filename")
            if isinstance(candidate_name, str) and candidate_name.strip() and filename is None:
                filename = candidate_name.strip()
            page_number = metadata.get("page_number")
            if isinstance(page_number, int) and not isinstance(page_number, bool) and page_number > 0:
                page_numbers.add(page_number)

        ids = existing.get("ids") or []
        return {
            "chunk_count": len(ids),
            "page_count": len(page_numbers),
            "filename": filename,
        }

    def get_document_chunks(self, document_id: str) -> list[SearchResult]:
        """Load all text and metadata for one document without its embeddings."""
        document_id = self._validate_document_id(document_id)
        try:
            stored = self._collection.get(
                where={"document_id": document_id},
                include=["documents", "metadatas"],
            )
        except Exception as exc:
            raise VectorStoreError(
                f"Could not load document passages for '{document_id}': {exc}"
            ) from exc

        ids = stored.get("ids") or []
        documents = stored.get("documents") or []
        metadatas = stored.get("metadatas") or []
        chunks: list[SearchResult] = []
        for vector_id, text, metadata in zip(ids, documents, metadatas):
            if not isinstance(text, str) or not text.strip():
                continue
            metadata = metadata if isinstance(metadata, dict) else {}
            try:
                chunk_id = int(metadata.get("chunk_id", str(vector_id).rsplit("::", 1)[-1]))
            except (TypeError, ValueError):
                continue
            page = metadata.get("page_number")
            try:
                page = int(page) if page is not None and int(page) > 0 else None
            except (TypeError, ValueError):
                page = None
            chunks.append(
                SearchResult(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    text=text.strip(),
                    heading=metadata.get("heading") or None,
                    page_number=page,
                    score=0.0,
                    filename=metadata.get("filename") or None,
                )
            )
        chunks.sort(key=lambda item: (item.page_number or 0, item.chunk_id))
        return chunks

    def get_chunk(self, document_id: str, chunk_id: int) -> Optional[SearchResult]:
        """Load one exact cited passage from a document without rerunning search."""
        document_id = self._validate_document_id(document_id)
        if isinstance(chunk_id, bool) or not isinstance(chunk_id, int) or chunk_id < 0:
            raise VectorStoreError("chunk_id must be a non-negative integer.")
        try:
            stored = self._collection.get(
                ids=[_vector_id(document_id, chunk_id)],
                include=["documents", "metadatas"],
            )
        except Exception as exc:
            raise VectorStoreError(f"Could not load source chunk: {exc}") from exc
        ids = stored.get("ids") or []
        if not ids:
            return None
        texts = stored.get("documents") or []
        metadatas = stored.get("metadatas") or []
        metadata = metadatas[0] if metadatas and isinstance(metadatas[0], dict) else {}
        page = metadata.get("page_number")
        if page == -1:
            page = None
        return SearchResult(
            chunk_id=chunk_id,
            document_id=document_id,
            text=texts[0] if texts and isinstance(texts[0], str) else "",
            heading=metadata.get("heading") or None,
            page_number=page,
            score=1.0,
            filename=metadata.get("filename") or None,
        )


# ======================================================================
# Singleton
# ======================================================================

_instance: Optional[VectorStore] = None
_lock = threading.Lock()


def get_vector_store() -> VectorStore:
    """
    Return the process-level VectorStore singleton.
    """

    global _instance

    if _instance is None:

        with _lock:

            if _instance is None:
                _instance = VectorStore()

    return _instance

