

from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Optional

from backend.app.config import RETRIEVAL_SETTINGS, RetrievalSettings
from backend.app.services.embedding import (
    EmbeddingError,
    EmbeddingService,
    get_embedding_service,
)
from backend.app.services.vector import (
    SearchResult,
    VectorStore,
    VectorStoreError,
    get_vector_store,
)
from backend.app.services.document_registry import get_document_registry

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u0900-\u097f]+", re.IGNORECASE)
_STOP_WORDS = frozenset(
    "a an and are as at be but by can could did do does for from had has have how "
    "i in is it its me my of on or our please tell than that the their them then "
    "there these they this to was we were what when where which who why will with "
    "would you your pdf document book file page about content show explain describe "
    "give find summarize summary overview key points".split()
)
_PAGE_QUERY = re.compile(
    r"\b(?:pages?|p\.?|pagina|página|pag\.?|पेज|पन्ना)"
    r"(?:\s+(?:number|no\.?))?\s*#?\s*(\d{1,4})\b",
    re.IGNORECASE,
)
_DOCUMENT_OVERVIEW_QUERY = re.compile(
    r"\b(?:summar(?:y|ize|ise|izing|ising)|overview|key\s+points|"
    r"what\s+(?:is|does)\s+(?:this|the)\s+(?:pdf|document|book|file)|"
    r"what\s+is\s+(?:this|the)\s+(?:pdf|document|book|file)\s+(?:about|on)|"
    r"what\s+is\s+(?:the\s+)?(?:content|inside)\s+of\s+(?:this|the)\s+(?:pdf|document|book|file)|"
    r"what(?:'s|\s+is)\s+in\s+(?:this|the)\s+(?:pdf|document|book|file)|"
    r"describe\s+(?:this|the)\s+(?:pdf|document|book|file)|"
    r"(?:pdf|document|book)\s+(?:mein|me)\s+kya\s+hai|"
    r"(?:pdf|document|book)\s+ka\s+(?:summary|saaransh|overview)|"
    r"इस\s+(?:पीडीएफ|दस्तावेज़|डॉक्यूमेंट|किताब).{0,24}(?:क्या|सारांश|बताइए))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _DocumentIndex:
    chunks: list[SearchResult]
    token_counts: list[Counter[str]]
    document_frequencies: dict[str, int]
    average_length: float


class RetrievalError(Exception):
    """Raised for any retrieval failure: bad input (empty query, invalid
    top_k) or a failure in an underlying service (embedding/vector store).
    Routes catch this one type instead of reaching into EmbeddingError /
    VectorStoreError individually."""


class RetrievalInputError(RetrievalError, ValueError):
    """Raised when a caller supplies invalid retrieval parameters."""


class RetrievalUnavailableError(RetrievalError):
    """Raised when embeddings or the vector database cannot serve a query."""


@dataclass
class RetrievedChunk:
    """One retrieved chunk, ready for a future LLM/prompt-building layer to
    consume. Mirrors vector.py's SearchResult field-for-field (chunk_id,
    document_id, text, heading, page_number, score) — kept as its own type
    only so the retrieval layer's public contract doesn't change if
    vector.py's internal SearchResult shape ever does."""

    chunk_id: int
    document_id: str
    text: str
    heading: Optional[str]
    page_number: Optional[int]
    score: float
    filename: Optional[str] = None


def _to_retrieved_chunk(result: SearchResult) -> RetrievedChunk:
    filename = result.filename
    if not filename:
        try:
            filename = get_document_registry().filename_for(result.document_id)
        except Exception as exc:
            logger.warning("Document filename lookup failed: %s", type(exc).__name__)
    return RetrievedChunk(
        chunk_id=result.chunk_id,
        document_id=result.document_id,
        text=result.text,
        heading=result.heading,
        page_number=result.page_number,
        score=result.score,
        filename=filename,
    )


class RetrievalService:
    """Usage:
        service = RetrievalService()
        chunks = service.retrieve("what is a primary key?", top_k=5)
        chunks = service.retrieve("...", top_k=5, document_id="doc-123")

    Dependencies are injectable (defaults to the existing process-wide
    singletons from embedding.py / vector.py) so this can be unit-tested
    with fakes, without loading the real model or opening the real
    on-disk database.
    """

    def __init__(
        self,
        embedding_service: Optional[EmbeddingService] = None,
        vector_store: Optional[VectorStore] = None,
        settings: Optional[RetrievalSettings] = None,
    ) -> None:
        self._embedding_service = embedding_service
        self._vector_store = vector_store
        self.settings = settings or RETRIEVAL_SETTINGS
        self._document_indexes: OrderedDict[str, _DocumentIndex] = OrderedDict()
        self._document_index_lock = threading.RLock()

    # Lazy-resolve the singletons on first use rather than at __init__ time,
    # so constructing a RetrievalService doesn't force-load the embedding
    # model or open the vector DB until retrieve() is actually called.
    def _get_embedding_service(self) -> EmbeddingService:
        if self._embedding_service is None:
            self._embedding_service = get_embedding_service()
        return self._embedding_service

    def _get_vector_store(self) -> VectorStore:
        if self._vector_store is None:
            self._vector_store = get_vector_store()
        return self._vector_store

    def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        document_id: Optional[str] = None,
        min_score: Optional[float] = None,
    ) -> list[RetrievedChunk]:
        """Embed `query` and return the top_k most similar chunks.

        Args:
            query: the user's question / search text. Must be non-empty.
            top_k: how many chunks to return. Defaults to
                RetrievalSettings.default_top_k; values above
                RetrievalSettings.max_top_k are clamped down (not rejected)
                since an overly large top_k is a client mistake, not a
                reason to error.
            document_id: restrict the search to one document, if given.
            min_score: optional similarity floor in [0, 1]. Chroma has no
                native server-side threshold, so this is applied client-side
                after the search — results below it are dropped. Left as
                None by default (no filtering), since a good default cutoff
                is corpus/model-dependent and shouldn't be hard-coded here.

        Returns:
            Ranked list of RetrievedChunk, best match first. An empty list
            (not an error) if nothing matches or the store is empty — "no
            results" is an expected, normal outcome for a RAG system.

        Raises:
            RetrievalError: empty query, invalid top_k, or a failure in the
                embedding/vector-store layer underneath.
        """
        if not isinstance(query, str) or not query.strip():
            raise RetrievalInputError("Query must be a non-empty string.")
        if len(query.strip()) > 8_000:
            raise RetrievalInputError("Query must not exceed 8000 characters.")

        top_k = self.settings.default_top_k if top_k is None else top_k
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise RetrievalInputError("top_k must be an integer.")
        if top_k < 1:
            raise RetrievalInputError("top_k must be >= 1.")
        top_k = min(top_k, self.settings.max_top_k)

        if document_id is not None:
            if not isinstance(document_id, str) or not document_id.strip():
                raise RetrievalInputError("document_id must be a non-empty string.")
            document_id = document_id.strip()

        if min_score is not None:
            if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
                raise RetrievalInputError("min_score must be a number.")
            min_score = float(min_score)
            if not math.isfinite(min_score) or not 0.0 <= min_score <= 1.0:
                raise RetrievalInputError("min_score must be between 0.0 and 1.0.")

        try:
            document_index = (
                self._get_document_index(document_id)
                if document_id is not None
                else None
            )

            requested_pages = self._requested_pages(query)
            if requested_pages and document_index is not None:
                # Page-number questions must be answered from those exact pages;
                # semantic nearest-neighbour search routinely misses numeric intent.
                page_chunks = [
                    chunk
                    for chunk in document_index.chunks
                    if chunk.page_number in requested_pages
                ]
                return [_to_retrieved_chunk(chunk) for chunk in page_chunks[:24]]

            if document_index is not None and self._is_document_overview(query):
                summary_chunks = self._representative_chunks(document_index.chunks, 18)
                return [_to_retrieved_chunk(chunk) for chunk in summary_chunks]

            query_vector = self._get_embedding_service().embed_text(query)
        except EmbeddingError as exc:
            logger.warning("Query embedding failed: %s", type(exc).__name__)
            raise RetrievalUnavailableError("Failed to embed the search query.") from exc
        except VectorStoreError as exc:
            logger.warning("Document passages could not be loaded: %s", type(exc).__name__)
            raise RetrievalUnavailableError("The document passages are unavailable.") from exc

        try:
            candidate_k = (
                min(max(top_k * 8, 40), self.settings.max_top_k)
                if document_index is not None
                else top_k
            )
            results = self._get_vector_store().search(
                query_vector, top_k=candidate_k, document_id=document_id
            )
        except VectorStoreError as exc:
            # Covers "vector store not initialized"/unreachable — e.g.
            # chromadb missing or the persist directory can't be opened.
            logger.warning("Vector search failed: %s", type(exc).__name__)
            raise RetrievalUnavailableError("The vector search service is unavailable.") from exc

        if min_score is not None:
            results = [r for r in results if r.score >= min_score]

        # Deterministic ordering: the vector store already returns results
        # sorted by distance, but ties (equal score) could otherwise depend
        # on the index's internal order. Explicit stable sort removes that
        # ambiguity — best score first, ties broken by chunk_id.
        results.sort(key=lambda r: (-r.score, r.chunk_id))

        if document_index is not None:
            results = self._hybrid_rank(query, results, document_index, top_k, min_score)
        else:
            results = results[:top_k]

        if not results:
            logger.info("No matching chunks for query (top_k=%d).", top_k)

        return [_to_retrieved_chunk(r) for r in results]

    @staticmethod
    def _requested_page(query: str) -> int | None:
        pages = RetrievalService._requested_pages(query)
        return pages[0] if pages else None

    @staticmethod
    def _requested_pages(query: str) -> tuple[int, ...]:
        return tuple(dict.fromkeys(int(value) for value in _PAGE_QUERY.findall(query)))

    @staticmethod
    def _is_document_overview(query: str) -> bool:
        return bool(_DOCUMENT_OVERVIEW_QUERY.search(query))

    def _get_document_index(self, document_id: str) -> _DocumentIndex | None:
        with self._document_index_lock:
            cached = self._document_indexes.get(document_id)
            if cached is not None:
                self._document_indexes.move_to_end(document_id)
                return cached

        vector_store = self._get_vector_store()
        load_chunks = getattr(vector_store, "get_document_chunks", None)
        if not callable(load_chunks):
            return None
        chunks = load_chunks(document_id)
        token_counts: list[Counter[str]] = []
        frequencies: Counter[str] = Counter()
        total_length = 0
        for chunk in chunks:
            counts = Counter(self._tokens(chunk.text))
            token_counts.append(counts)
            frequencies.update(counts.keys())
            total_length += sum(counts.values())
        index = _DocumentIndex(
            chunks=chunks,
            token_counts=token_counts,
            document_frequencies=dict(frequencies),
            average_length=total_length / max(1, len(chunks)),
        )
        with self._document_index_lock:
            self._document_indexes[document_id] = index
            self._document_indexes.move_to_end(document_id)
            while len(self._document_indexes) > 4:
                self._document_indexes.popitem(last=False)
        return index

    def warm_document(self, document_id: str) -> bool:
        """Build the in-memory lexical index before the UI marks upload ready."""
        try:
            return self._get_document_index(document_id) is not None
        except VectorStoreError as exc:
            logger.warning("Could not prepare fast retrieval for %s: %s", document_id, type(exc).__name__)
            return False

    def invalidate_document(self, document_id: str) -> None:
        """Drop cached passages before a document's vectors are replaced."""
        with self._document_index_lock:
            self._document_indexes.pop(document_id, None)

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [
            token.lower()
            for token in _TOKEN_RE.findall(text)
            if len(token) > 1 and token.lower() not in _STOP_WORDS
        ]

    @classmethod
    def _representative_chunks(
        cls, chunks: list[SearchResult], limit: int
    ) -> list[SearchResult]:
        """Choose content-rich passages spread across the complete page range."""
        if not chunks:
            return []
        by_page: dict[int, list[SearchResult]] = defaultdict(list)
        for chunk in chunks:
            by_page[chunk.page_number if chunk.page_number is not None else chunk.chunk_id].append(chunk)
        pages = sorted(by_page)
        if len(pages) <= limit:
            selected_pages = pages
        else:
            selected_pages = [
                pages[round(slot * (len(pages) - 1) / (limit - 1))]
                for slot in range(limit)
            ]

        selected: list[SearchResult] = []
        for page in selected_pages:
            candidates = by_page[page]
            eligible = [
                chunk for chunk in candidates
                if not re.search(
                    r"\b(?:table\s+of\s+contents|contents|index|bibliography|references)\b",
                    f"{chunk.heading or ''} {chunk.text[:240]}",
                    re.IGNORECASE,
                )
            ]
            if not eligible:
                eligible = candidates

            def content_score(chunk: SearchResult) -> tuple[float, int]:
                words = cls._tokens(chunk.text)
                diversity = len(set(words)) / max(1, len(words))
                text_length = len(chunk.text.strip())
                has_heading = bool((chunk.heading or "").strip())
                return (min(text_length, 900) * (0.85 + diversity * 0.3) + has_heading * 80, text_length)

            selected.append(max(eligible, key=content_score))
        return sorted(selected, key=lambda chunk: (chunk.page_number or 0, chunk.chunk_id))

    def _hybrid_rank(
        self,
        query: str,
        semantic_results: list[SearchResult],
        index: _DocumentIndex,
        top_k: int,
        min_score: float | None,
    ) -> list[SearchResult]:
        """Combine BGE similarity with a cached BM25-style exact-term score."""
        query_tokens = list(dict.fromkeys(self._tokens(query)))
        if not query_tokens or not index.chunks:
            return semantic_results[:top_k]

        total = len(index.chunks)
        average_length = max(1.0, index.average_length)
        lexical_scores: list[float] = []
        for chunk, counts in zip(index.chunks, index.token_counts):
            length = sum(counts.values())
            score = 0.0
            for token in query_tokens:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                document_frequency = index.document_frequencies.get(token, 0)
                inverse_frequency = math.log(
                    1.0 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + 1.2 * (0.25 + 0.75 * length / average_length)
                score += inverse_frequency * frequency * 2.2 / denominator
            lexical_scores.append(score)

        lexical_max = max(lexical_scores, default=0.0)
        semantic_by_id = {result.chunk_id: result for result in semantic_results}
        fused: list[tuple[float, SearchResult]] = []
        for chunk, lexical_score in zip(index.chunks, lexical_scores):
            semantic = semantic_by_id.get(chunk.chunk_id)
            if min_score is not None and semantic is None:
                continue
            semantic_score = semantic.score if semantic is not None else 0.0
            lexical_score = lexical_score / lexical_max if lexical_max else 0.0
            # Exact terms can rescue a passage outside the vector top-k, while
            # semantic similarity still carries more weight for paraphrases.
            score = 0.58 * semantic_score + 0.42 * lexical_score
            if score <= 0:
                continue
            fused.append((score, SearchResult(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                text=chunk.text,
                heading=chunk.heading,
                page_number=chunk.page_number,
                score=score,
                filename=chunk.filename,
            )))

        fused.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return [chunk for _, chunk in fused[:top_k]]


# --- Singleton accessor -------------------------------------------------
# Same pattern as embedding.py / vector.py: one instance per process,
# reused across requests instead of rebuilt per call.

_instance: Optional[RetrievalService] = None
_lock = threading.Lock()


def get_retrieval_service() -> RetrievalService:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = RetrievalService()
    return _instance
