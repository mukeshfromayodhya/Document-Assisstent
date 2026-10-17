from __future__ import annotations

import logging
import math
import threading
from typing import Callable, Optional

from backend.app.config import EMBEDDING_SETTINGS, EmbeddingSettings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Raised for any embedding failure: bad input, model load failure,
    or a device/runtime problem. Callers should catch this one type rather
    than reaching into sentence-transformers' or torch's exception classes."""


def _resolve_device(requested: str) -> str:
    """"auto" -> cuda if a GPU is available, else cpu. An explicit request
    ("cpu"/"cuda") is honored as-is so behavior stays predictable in tests
    and on machines without a GPU."""
    if requested != "auto":
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError as exc:  # pragma: no cover - torch is a hard dependency
        raise EmbeddingError(
            "PyTorch is required to select an embedding device."
         ) from exc


class EmbeddingService:
    def __init__(self, settings: Optional[EmbeddingSettings] = None) -> None:
        self.settings = settings or EMBEDDING_SETTINGS
        if self.settings.batch_size < 1:
            raise EmbeddingError("Embedding batch_size must be at least 1.")
        if self.settings.expected_dimension < 1:
            raise EmbeddingError("Expected embedding dimension must be positive.")
        self.device = _resolve_device(self.settings.device)

        try:
            from sentence_transformers import SentenceTransformer
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("sentence_transformers"):
                raise EmbeddingError(
                    "sentence-transformers is not installed. "
                    "Install it in the backend environment."
                ) from exc
            logger.exception("A dependency of sentence-transformers could not be imported")
            raise EmbeddingError(
                "A dependency required by sentence-transformers could not be imported. "
                "Check the chained exception for the failing module."
            ) from exc
        except ImportError as exc:
            logger.exception("sentence-transformers could not be imported")
            raise EmbeddingError(
                "sentence-transformers could not be imported because a dependency "
                "is unavailable or incompatible."
            ) from exc

        logger.info(
            "Loading embedding model '%s' on device '%s'",
            self.settings.model_name,
            self.device,
        )
        try:
            self._model = SentenceTransformer(
                self.settings.model_name, device=self.device
            )
        except Exception as exc:
            # Covers: no internet on first download, corrupted cache,
            # unsupported model name, out-of-memory on model load.
            raise EmbeddingError(
                f"Failed to load embedding model '{self.settings.model_name}': {exc}"
            ) from exc

        get_dimension = getattr(self._model, "get_embedding_dimension", None)
        if callable(get_dimension):
            actual_dim = get_dimension()
        else:  # Backward compatibility with older sentence-transformers.
            actual_dim = self._model.get_sentence_embedding_dimension()
        if isinstance(actual_dim, bool) or not isinstance(actual_dim, int) or actual_dim < 1:
            raise EmbeddingError("The embedding model did not report a valid dimension.")
        if actual_dim != self.settings.expected_dimension:
            logger.error(
                "Embedding dimension is %d, config expected %d. "
                "Update EMBEDDING_EXPECTED_DIMENSION and deliberately rebuild "
                "the vector collection before serving requests.",
                actual_dim,
                self.settings.expected_dimension,
            )
            raise EmbeddingError(
                f"Embedding model dimension {actual_dim} does not match configured "
                f"dimension {self.settings.expected_dimension}."
            )
        self.dimension = actual_dim

    def embed_text(self, text: str) -> list[float]:
        """Embed a single string. Returns a list[float] of length self.dimension."""
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("Text must be a non-empty string.")
        instruction = self.settings.query_instruction or ""
        return self.embed_batch([f"{instruction}{text.strip()}"])[0]

    def embed_batch(
        self,
        texts: list[str],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        """Embed multiple strings in batches. Returns one vector per input,
        in the same order. Raises EmbeddingError if any text is empty, so
        a bad chunk fails loudly instead of silently shifting the alignment
        between chunks and vectors."""
        if not isinstance(texts, list):
            raise EmbeddingError("texts must be a list of strings.")
        if not texts:
            return []
        for i, text in enumerate(texts):
            if not isinstance(text, str) or not text.strip():
                raise EmbeddingError(f"Text at index {i} must be a non-empty string.")

        result: list[list[float]] = []
        if progress_callback is None:
            batches = [(0, texts)]
        else:
            batches = [
                (start, texts[start : start + self.settings.batch_size])
                for start in range(0, len(texts), self.settings.batch_size)
            ]
        batch_count = len(batches)
        for batch_number, (start, batch) in enumerate(batches, start=1):
            try:
                vectors = self._model.encode(
                    batch,
                    batch_size=self.settings.batch_size,
                    normalize_embeddings=self.settings.normalize_embeddings,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
            except RuntimeError as exc:
                # Most commonly a CUDA out-of-memory error on the 3050's 6GB.
                raise EmbeddingError(
                    f"Embedding inference failed (possible out-of-memory on "
                    f"device '{self.device}'): {exc}"
                ) from exc
            except Exception as exc:
                logger.exception("Embedding inference failed")
                raise EmbeddingError("Embedding inference failed.") from exc

            try:
                batch_result = vectors.tolist()
            except AttributeError as exc:
                raise EmbeddingError("The embedding model returned an invalid result.") from exc
            if len(batch_result) != len(batch):
                raise EmbeddingError(
                    f"Embedding count mismatch: received {len(batch_result)} vectors "
                    f"for {len(batch)} texts."
                )
            for offset, vector in enumerate(batch_result):
                index = start + offset
                if not isinstance(vector, list) or len(vector) != self.dimension:
                    raise EmbeddingError(f"Invalid embedding vector at index {index}.")
                if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in vector):
                    raise EmbeddingError(f"Non-finite embedding value at index {index}.")
            result.extend(batch_result)
            if progress_callback is not None:
                progress_callback(batch_number, batch_count)
        return result


# --- Singleton accessor -----------------------------------------------------
# Avoids reloading the model on every FastAPI request while avoiding bare
# module-level global state: the instance is created lazily, once, behind a
# lock (safe under FastAPI's threaded/async workers).

_instance: Optional[EmbeddingService] = None
_lock = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    """FastAPI dependency / app-wide accessor. Creates the model on first
    call, reuses it after that."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = EmbeddingService()
    return _instance
