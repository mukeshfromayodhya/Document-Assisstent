from __future__ import annotations

import re
from statistics import median
from typing import Any


_LIST_PREFIX = re.compile(r"^\s*(?:(?:\d{1,3}|[A-Za-z])[.)]|[•▪◦‣⁃*+\-])(?:\s+|$)")
_STANDALONE_NUMBER = re.compile(r"^\s*\d{1,3}[.)]?\s*$")


def process_paragraphs(text: str) -> list[str]:
    if not text:
        return []
    if not isinstance(text, str):
        raise TypeError("text must be a string.")
    return [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]


def detect_heading(paragraph: str) -> bool:
    """Apply conservative text-only heading heuristics."""
    if not isinstance(paragraph, str):
        return False
    paragraph = paragraph.strip()
    if not paragraph or len(paragraph) > 120:
        return False
    if paragraph.endswith((".", "?", "!", ";")):
        return False
    if _LIST_PREFIX.match(paragraph) or _STANDALONE_NUMBER.fullmatch(paragraph):
        return False
    return any(char.isalnum() for char in paragraph)


def _bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    value = block.get("bbox")
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return None
    try:
        return tuple(float(part) for part in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _nearby(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_box = _bbox(previous)
    current_box = _bbox(current)
    if previous_box is None or current_box is None:
        return False
    previous_size = max(1.0, float(previous.get("size", 12)))
    current_size = max(1.0, float(current.get("size", previous_size)))
    vertical_gap = current_box[1] - previous_box[3]
    return vertical_gap <= max(4.0, max(previous_size, current_size) * 0.9)


def _same_paragraph(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_box = _bbox(previous)
    current_box = _bbox(current)
    if previous_box is None or current_box is None or not _nearby(previous, current):
        return False
    size = max(1.0, float(previous.get("size", 12)))
    return abs(current_box[0] - previous_box[0]) <= max(12.0, size * 1.5)


def process_text(page_blocks: list[dict[str, Any]], page_number: int) -> list[dict[str, Any]]:
    """Convert positioned PDF lines into page-scoped headings and paragraphs.

    Coordinates preserve blank-space paragraph breaks; font styling and
    conservative text cues identify headings. Numbered/bulleted lines remain
    list blocks and are never reclassified as headings based on font size.
    """
    if not page_blocks:
        return []

    valid_blocks: list[dict[str, Any]] = []
    sizes: list[float] = []
    for block in page_blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text", "")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            size = float(block.get("size", 0))
        except (TypeError, ValueError):
            size = 0.0
        if not 0 < size < 40:
            size = 12.0
        normalized = {**block, "text": text.strip(), "size": size}
        valid_blocks.append(normalized)
        sizes.append(size)

    if not valid_blocks:
        return []

    body_size = median(sizes) if sizes else 12.0
    structured: list[dict[str, Any]] = []
    paragraph_lines: list[str] = []
    previous_paragraph_line: dict[str, Any] | None = None

    def flush_paragraph() -> None:
        nonlocal previous_paragraph_line
        if paragraph_lines:
            structured.append(
                {
                    "type": "paragraph",
                    "text": " ".join(paragraph_lines),
                    "page_number": page_number,
                }
            )
            paragraph_lines.clear()
        previous_paragraph_line = None

    index = 0
    while index < len(valid_blocks):
        block = valid_blocks[index]
        text = block["text"]
        attached_number_marker = False

        # A separate, small text box containing only "1" or "2." often
        # represents the marker for a list item whose wording follows on the
        # same or next visual line. Keep the marker attached to that wording.
        if (
            _STANDALONE_NUMBER.fullmatch(text)
            and index + 1 < len(valid_blocks)
            and _nearby(block, valid_blocks[index + 1])
        ):
            following = valid_blocks[index + 1]
            following_text = following["text"]
            if not _STANDALONE_NUMBER.fullmatch(following_text):
                text = f"{text.strip()} {following_text}"
                block = {**block, "text": text}
                attached_number_marker = True
                index += 1

        is_list = attached_number_marker or bool(_LIST_PREFIX.match(text)) or bool(
            _STANDALONE_NUMBER.fullmatch(text)
        )
        bold = bool(block.get("bold"))
        size = float(block.get("size", body_size))
        styled_heading = (bold and size >= body_size * 0.95) or size >= body_size + 2.5
        is_heading = not is_list and styled_heading and detect_heading(text)

        if is_heading:
            flush_paragraph()
            structured.append(
                {"type": "heading", "text": text, "page_number": page_number}
            )
        elif is_list:
            flush_paragraph()
            structured.append(
                {"type": "list", "text": text, "page_number": page_number}
            )
        else:
            if (
                paragraph_lines
                and previous_paragraph_line is not None
                and not _same_paragraph(previous_paragraph_line, block)
            ):
                flush_paragraph()
            paragraph_lines.append(text)
            previous_paragraph_line = block
        index += 1

    flush_paragraph()
    return structured
