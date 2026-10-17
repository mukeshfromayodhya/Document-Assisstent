from __future__ import annotations

import os
import logging
import math
from pathlib import Path
import re
from typing import Any, Callable

import pymupdf

logger = logging.getLogger(__name__)
MAX_EXTRACTED_TEXT_CHARS = 5_000_000
MAX_PDF_INPUT_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_OCR_PIXELS = 500_000_000


class PDFInputError(ValueError):
    """Raised when an uploaded PDF is invalid or exceeds processing limits."""


def _positive_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _text_blocks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block_index, block in enumerate(raw.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            pieces: list[str] = []
            previous_x1: float | None = None
            max_size = 0.0
            is_bold = False
            for span in spans:
                span_text = span.get("text", "")
                if not isinstance(span_text, str) or not span_text:
                    continue
                try:
                    size = float(span.get("size", 0))
                except (TypeError, ValueError):
                    size = 0.0
                max_size = max(max_size, size)
                font_name = str(span.get("font", "")).lower()
                try:
                    font_flags = int(span.get("flags", 0))
                except (TypeError, ValueError):
                    font_flags = 0
                is_bold = is_bold or "bold" in font_name or bool(font_flags & 16)

                span_bbox = span.get("bbox")
                try:
                    x0, _, x1, _ = (float(value) for value in span_bbox)
                except (TypeError, ValueError):
                    x0 = x1 = None
                if (
                    pieces
                    and previous_x1 is not None
                    and x0 is not None
                    and x0 - previous_x1 >= max(0.8, size * 0.12)
                    and not pieces[-1].endswith((" ", "\t"))
                    and not span_text.startswith((" ", "\t"))
                ):
                    pieces.append(" ")
                pieces.append(span_text)
                if x1 is not None:
                    previous_x1 = x1 if previous_x1 is None else max(previous_x1, x1)

            line_text = "".join(pieces).strip()
            if not line_text:
                continue
            line_record: dict[str, Any] = {
                "text": line_text,
                "size": max_size,
                "bold": is_bold,
                "block_index": block_index,
            }
            bbox = line.get("bbox")
            if isinstance(bbox, (tuple, list)) and len(bbox) == 4:
                try:
                    line_record["bbox"] = tuple(float(value) for value in bbox)
                except (TypeError, ValueError):
                    pass
            blocks.append(line_record)
    return blocks


def _contains_large_image(page: pymupdf.Page, minimum_coverage: float = 0.05) -> bool:
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return False
    for image in page.get_images(full=True):
        for rect in page.get_image_rects(image[0]):
            visible = rect & page.rect
            visible_area = max(0, visible.width) * max(0, visible.height)
            if visible_area / page_area >= minimum_coverage:
                return True
    return False


def extract_text(
    file_path: str | Path,
    progress_callback: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """Extract positioned text, falling back to Tesseract for image-only pages."""
    max_pages = _positive_env_int("PDF_MAX_PAGES", 500)
    ocr_dpi = _positive_env_int("OCR_DPI", 240)
    if ocr_dpi > 600:
        raise ValueError("OCR_DPI must not exceed 600.")
    ocr_language = os.getenv("OCR_LANGUAGE", "eng").strip() or "eng"
    if not re.fullmatch(r"[A-Za-z0-9_+.-]{1,64}", ocr_language):
        raise ValueError("OCR_LANGUAGE contains unsupported characters.")
    max_ocr_pixels = _positive_env_int(
        "PDF_MAX_OCR_PIXELS", DEFAULT_MAX_OCR_PIXELS
    )

    try:
        source_path = Path(file_path)
        if source_path.stat().st_size > MAX_PDF_INPUT_BYTES:
            raise PDFInputError("PDF exceeds the 10 MB upload limit.")
        # Parse bounded bytes instead of leaving a native file handle attached
        # to a failed parser open (notably on Windows, where that can prevent
        # the temporary upload from being removed immediately).
        pdf_data = source_path.read_bytes()
        doc = pymupdf.open(stream=pdf_data, filetype="pdf")
    except PDFInputError:
        raise
    except OSError as exc:
        logger.warning("Could not read a temporary PDF file: %s", type(exc).__name__)
        raise RuntimeError("Could not read the uploaded PDF.") from exc
    except (pymupdf.FileDataError, pymupdf.EmptyFileError) as exc:
        logger.info("Rejected an unreadable PDF (parser error: %s)", type(exc).__name__)
        raise PDFInputError("Uploaded file is not a readable PDF.") from exc
    pages: list[dict[str, Any]] = []
    try:
        if doc.needs_pass:
            raise PDFInputError("Password-protected PDFs are not supported.")
        if len(doc) > max_pages:
            raise PDFInputError(f"PDF exceeds the {max_pages}-page limit.")

        total_extracted_characters = 0
        total_ocr_pixels = 0
        page_total = len(doc)
        for page in doc:
            page_number = page.number + 1
            raw = page.get_text("dict", sort=True)
            blocks = _text_blocks(raw)
            ocr_used = False

            # OCR pages with no selectable text, plus mixed pages with a
            # substantial image area. Tiny logos do not trigger a full OCR pass.
            # full=False preserves legible text and fills image regions.
            if not blocks or _contains_large_image(page):
                estimated_pixels = (
                    page.rect.width * ocr_dpi / 72
                ) * (page.rect.height * ocr_dpi / 72)
                if estimated_pixels > 50_000_000:
                    raise PDFInputError(
                        f"PDF page {page_number} is too large for the configured OCR resolution."
                    )
                total_ocr_pixels += math.ceil(estimated_pixels)
                if total_ocr_pixels > max_ocr_pixels:
                    raise PDFInputError(
                        "PDF exceeds the maximum cumulative OCR processing limit. "
                        "Lower OCR_DPI or split the document into smaller files."
                    )
                try:
                    ocr_used = True
                    text_page = page.get_textpage_ocr(
                        language=ocr_language,
                        dpi=ocr_dpi,
                        full=False,
                    )
                    raw = page.get_text("dict", textpage=text_page, sort=True)
                    blocks = _text_blocks(raw)
                except Exception as exc:
                    logger.exception(
                        "OCR extraction failed on PDF page %d", page_number
                    )
                    raise RuntimeError(
                        "OCR extraction failed on PDF page "
                        f"{page_number}; verify Tesseract and the configured "
                        f"'{ocr_language}' language data."
                    ) from exc

            total_extracted_characters += sum(len(block["text"]) for block in blocks)
            if total_extracted_characters > MAX_EXTRACTED_TEXT_CHARS:
                raise PDFInputError(
                    "PDF exceeds the maximum extracted text limit of "
                    f"{MAX_EXTRACTED_TEXT_CHARS} characters."
                )

            page_text = "\n\n".join(block["text"] for block in blocks)

            pages.append(
                {
                    "page_number": page_number,
                    "text": page_text,
                    "blocks": blocks,
                    "ocr_used": ocr_used,
                }
            )
            if progress_callback is not None and page_total:
                progress_callback(round((page.number + 1) / page_total * 100))
    except (pymupdf.FileDataError, pymupdf.EmptyFileError) as exc:
        logger.info("Rejected a PDF with unreadable page data (parser error: %s)", type(exc).__name__)
        raise PDFInputError("PDF contains unreadable page data.") from exc
    finally:
        doc.close()
    return pages
