from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class TextChunk:
    chunk_id: int
    text: str
    heading: Optional[str] = None
    page_number: Optional[int] = None


class TextChunker:
    def __init__(self, max_chars: int, overlap_chars: int) -> None:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
            raise ValueError("max_chars must be a positive integer.")
        if isinstance(overlap_chars, bool) or not isinstance(overlap_chars, int):
            raise ValueError("overlap_chars must be an integer.")
        if overlap_chars < 0:
            raise ValueError("overlap_chars cannot be negative.")
        if overlap_chars >= max_chars:
            raise ValueError("overlap_chars must be smaller than max_chars.")

        self.max_chars = max_chars
        self.overlap_chars = overlap_chars

    def _append_split(
        self,
        chunks: list[TextChunk],
        text: str,
        heading: Optional[str],
        page_number: Optional[int],
    ) -> None:
        """Append bounded pieces, repeating section context after each split."""
        heading_prefix = f"{heading}\n\n" if heading else ""
        body = text
        if heading_prefix and body.startswith(heading_prefix):
            body = body[len(heading_prefix) :]
        elif heading and body.startswith(heading):
            body = body[len(heading) :].lstrip()

        content_limit = self.max_chars - len(heading_prefix)
        if content_limit < 1:
            heading_prefix = ""
            body = text
            content_limit = self.max_chars
        overlap = min(self.overlap_chars, max(0, content_limit - 1))
        stride = content_limit - overlap
        start = 0
        while start < len(body):
            end = min(start + content_limit, len(body))
            piece = f"{heading_prefix}{body[start:end].strip()}".strip()
            if piece:
                chunks.append(
                    TextChunk(
                        chunk_id=len(chunks) + 1,
                        text=piece,
                        heading=heading,
                        page_number=page_number,
                    )
                )
            if end == len(body):
                break
            start += stride
        if not body and heading:
            chunks.append(
                TextChunk(
                    chunk_id=len(chunks) + 1,
                    text=heading[: self.max_chars],
                    heading=heading,
                    page_number=page_number,
                )
            )

    def chunk(self, blocks: list[dict]) -> list[TextChunk]:
        if not isinstance(blocks, list):
            raise TypeError("blocks must be a list of dictionaries.")

        chunks: list[TextChunk] = []
        current_text = ""
        current_heading: Optional[str] = None
        current_page: Optional[int] = None
        current_has_body = False

        def flush() -> None:
            nonlocal current_text, current_has_body
            if current_text and current_has_body:
                chunks.append(
                    TextChunk(
                        chunk_id=len(chunks) + 1,
                        text=current_text,
                        heading=current_heading,
                        page_number=current_page,
                    )
                )
            current_text = ""
            current_has_body = False

        for block in blocks:
            if not isinstance(block, dict):
                raise TypeError("Each text block must be a dictionary.")
            block_type = block.get("type")
            raw_text = block.get("text", "")
            if not isinstance(raw_text, str):
                raise TypeError("Text block 'text' must be a string.")
            text = raw_text.strip()
            if not text:
                continue

            page_number = block.get("page_number")
            if page_number != current_page:
                if current_text:
                    flush()
                current_heading = None
                current_page = page_number

            if block_type == "heading":
                flush()
                current_heading = text
                current_page = page_number
                # Keep the heading in the embedded text as well as metadata,
                # so a heading-only chunk is never stored and body chunks are
                # searchable with their section context.
                current_text = text
                current_has_body = False
                continue
            if block_type not in {"paragraph", "list"}:
                continue

            if not current_text:
                contextual_text = (
                    text
                    if current_heading is None or text.startswith(current_heading)
                    else f"{current_heading}\n\n{text}"
                )
                if len(contextual_text) <= self.max_chars:
                    current_text = contextual_text
                    current_page = page_number
                    current_has_body = True
                else:
                    self._append_split(chunks, contextual_text, current_heading, page_number)
                continue

            candidate = f"{current_text}\n\n{text}"
            if len(candidate) <= self.max_chars:
                current_text = candidate
                current_has_body = True
                continue

            # Split the full candidate so the previous chunk's tail overlaps
            # the next piece without allowing overlap + a long paragraph to
            # exceed max_chars.
            self._append_split(chunks, candidate, current_heading, current_page)
            current_text = ""
            current_has_body = False

        flush()
        return chunks
