from __future__ import annotations

import re
import unicodedata


def clean_text(text: str) -> str:
    """Normalize extracted PDF text and remove non-printing control characters."""
    if text is None or text == "":
        return ""
    if not isinstance(text, str):
        raise TypeError("text must be a string.")

    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char for char in text if char in "\n\t" or char.isprintable())
    text = re.sub(r"\t+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
