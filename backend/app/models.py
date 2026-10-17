from dataclasses import dataclass
from typing import Optional

from backend.app.services.text_chunker import TextChunk


@dataclass
class EmbeddingRecord:
    chunk: TextChunk
    embedding: list[float]
    document_id: str


@dataclass
class SearchResult:
    chunk_id: int
    document_id: str
    text: str
    heading: Optional[str]
    page_number: Optional[int]
    score: float