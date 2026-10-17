"""End-to-end retrieval-augmented answer generation."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from backend.app.services.llm_service import LLMError, LLMService
from backend.app.services.prompt_builder import PromptBuilder, PromptBuilderError
from backend.app.services.retrieval import (
    RetrievalError,
    RetrievalService,
    get_retrieval_service,
)
logger = logging.getLogger(__name__)


class RAGError(RuntimeError):
    """Raised when retrieval or answer generation cannot be completed."""


@dataclass(frozen=True)
class SourceCitation:
    citation: str
    document_id: str
    chunk_id: str
    page_number: int | None
    heading: str | None
    score: float | None
    filename: str | None = None


@dataclass(frozen=True)
class RAGResponse:
    query: str
    answer: str
    sources: list[SourceCitation]
    small_talk: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "answer": self.answer,
            "small_talk": self.small_talk,
            # The web client uses the active conversation's document ID plus
            # chunk number to load an excerpt. Keep hashes out of source cards.
            "sources": [
                {
                    "citation": source.citation,
                    "chunk_id": source.chunk_id,
                    "page_number": source.page_number,
                }
                for source in self.sources
            ],
        }


class RAGService:
    """Retrieve evidence, build a grounded prompt, and generate an answer.

    Dependencies are injectable so the pipeline can be called with fake
    retrievers and LLM clients during development without loading a model or
    contacting an external provider.
    """

    NO_RESULTS_ANSWER = "I couldn't find relevant information in the indexed documents."
    MAX_QUERY_CHARS = 8_000
    _SMALL_TALK = re.compile(
        r"^(?:hi+|hello+|hey+|good\s+(?:morning|afternoon|evening)|"
        r"how\s+are\s+you(?:\s+doing)?|what(?:'s|\s+is)\s+up|"
        r"thanks?(?:\s+you)?|thank\s+you|bye+|goodbye|who\s+are\s+you)[.!?\s]*$",
        re.IGNORECASE,
    )
    _DOCUMENT_OVERVIEW = re.compile(
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

    def __init__(
        self,
        retrieval_service: RetrievalService | None = None,
        prompt_builder: PromptBuilder | None = None,
        llm_service: LLMService | None = None,
    ) -> None:
        self.retrieval_service = (
            retrieval_service if retrieval_service is not None else get_retrieval_service()
        )
        self.prompt_builder = prompt_builder if prompt_builder is not None else PromptBuilder()
        self.llm_service = llm_service if llm_service is not None else LLMService()

    def answer(
        self,
        query: str,
        *,
        top_k: int | None = None,
        document_id: str | None = None,
        min_score: float | None = None,
    ) -> RAGResponse:
        """Answer a question using retrieved document chunks as evidence."""

        if not isinstance(query, str) or not query.strip():
            raise RAGError("Query must be a non-empty string.")
        query = query.strip()
        if len(query) > self.MAX_QUERY_CHARS:
            raise RAGError(f"Query must not exceed {self.MAX_QUERY_CHARS} characters.")

        if self._SMALL_TALK.fullmatch(query):
            lowered = query.lower()
            if re.match(r"^(?:hi+|hello+|hey+|good\s+)", lowered):
                answer = "Hello! I can help you find and explain information in your document."
            elif lowered.startswith("how are you") or "what" in lowered:
                answer = "I’m ready to help with your document. What would you like to know?"
            elif lowered.startswith(("thank", "thanks")):
                answer = "You’re welcome. Ask me anything about your document."
            elif lowered.startswith(("bye", "goodbye")):
                answer = "Goodbye! Your document chat is here when you need it."
            else:
                answer = "I’m your document assistant. Ask me a question about the PDF you uploaded."
            return RAGResponse(query=query, answer=answer, sources=[], small_talk=True)

        try:
            chunks = self.retrieval_service.retrieve(
                query=query,
                top_k=top_k,
                document_id=document_id,
                min_score=min_score,
            )
        
        except RetrievalError as exc:
            logger.warning("RAG document retrieval failed: %s", type(exc).__name__)
            raise RAGError("Document retrieval failed.") from exc

        except Exception as exc:
            logger.exception("Unexpected RAG retrieval failure")
            raise RAGError("Document retrieval failed.") from exc

        if not chunks:
            return RAGResponse(
                query=query,
                answer=self.NO_RESULTS_ANSWER,
                sources=[],
            )

        try:
            is_overview = bool(self._DOCUMENT_OVERVIEW.search(query))
            if is_overview:
                try:
                    prompt_context = self.prompt_builder.build(query, chunks, purpose="overview")
                except TypeError as exc:
                    # Keep injected legacy prompt builders compatible; only
                    # retry when they reject the new optional purpose keyword.
                    if "purpose" not in str(exc):
                        raise
                    prompt_context = self.prompt_builder.build(query, chunks)
            else:
                prompt_context = self.prompt_builder.build(query, chunks)
        except PromptBuilderError as exc:
            raise RAGError("Could not build the answer prompt.") from exc
        except Exception as exc:
            logger.exception("Unexpected prompt construction failure")
            raise RAGError("Could not build the answer prompt.") from exc

        try:
            answer = self.llm_service.generate(prompt_context.messages)
        except LLMError as exc:
            logger.error("LLM answer generation failed: %s", exc)
            raise RAGError("Answer generation failed.") from exc
        except Exception as exc:
            logger.exception("Unexpected answer generation failure")
            raise RAGError("Answer generation failed.") from exc

        answer = self._normalize_answer_citations(answer, prompt_context.sources)

        sources = [
            self._source_from_chunk(source, index + 1)
            for index, source in enumerate(prompt_context.sources)
        ]
        cited = set(re.findall(r"\[S\d+\]", answer))
        if cited:
            sources = [source for source in sources if source.citation in cited]
        return RAGResponse(query=query, answer=answer, sources=sources)

    @staticmethod
    def _normalize_answer_citations(answer: str, sources: list[dict[str, Any]]) -> str:
        """Map legacy hash citations to short source labels and reject invented IDs."""
        allowed: set[str] = set()
        for index, source in enumerate(sources, start=1):
            citation = source.get("citation")
            if not isinstance(citation, str) or not re.fullmatch(r"\[S\d+\]", citation):
                citation = f"[S{index}]"
            allowed.add(citation)
            document_id = source.get("document_id")
            chunk_id = source.get("chunk_id")
            if document_id is not None and chunk_id is not None:
                answer = answer.replace(f"[{document_id}:{chunk_id}]", citation)
                answer = answer.replace(f"[[{document_id}:{chunk_id}]]", citation)
            answer = answer.replace(f"[[{citation[1:-1]}]]", citation)

        answer = re.sub(r"\[\[?[0-9a-fA-F]{32,64}:\d+\]\]?", "", answer)
        answer = re.sub(
            r"\[(?:S|Source\s*)\d+\]",
            lambda match: match.group(0) if match.group(0) in allowed else "",
            answer,
        )
        answer = re.sub(r"[ \t]{2,}", " ", answer)
        return answer.strip()

    @classmethod
    def _source_from_chunk(cls, chunk: Any, source_index: int = 1) -> SourceCitation:
        metadata = cls._value(chunk, "metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        document_id = cls._value(
            chunk,
            "document_id",
            metadata.get("document_id", "unknown-document"),
        ) or metadata.get("document_id") or "unknown-document"
        chunk_id = cls._value(
            chunk,
            "chunk_id",
            metadata.get("chunk_id", "unknown-chunk"),
        )
        if chunk_id is None:
            chunk_id = "unknown-chunk"
        page_number = cls._value(chunk, "page_number", metadata.get("page_number"))
        if page_number == -1:
            page_number = None
        if page_number is not None:
            try:
                page_number = int(page_number)
            except (TypeError, ValueError):
                page_number = None
        filename = cls._value(chunk, "filename", metadata.get("filename"))
        if isinstance(filename, str):
            filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()[:255] or None
        else:
            filename = None
        citation = cls._value(chunk, "citation", None)
        if not isinstance(citation, str) or not re.fullmatch(r"\[S\d+\]", citation):
            citation = f"[S{max(1, source_index)}]"

        document_id = str(document_id)
        chunk_id = str(chunk_id)
        return SourceCitation(
            citation=citation,
            document_id=document_id,
            chunk_id=chunk_id,
            page_number=page_number,
            heading=None,
            score=None,
            filename=filename,
        )

    @staticmethod
    def _value(chunk: Any, name: str, default: Any) -> Any:
        if isinstance(chunk, dict):
            return chunk.get(name, default)
        return getattr(chunk, name, default)


_instance: RAGService | None = None
_lock = threading.Lock()


def get_rag_service() -> RAGService:
    """Return the process-wide RAG service without eager model/network work."""

    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = RAGService()
    return _instance
