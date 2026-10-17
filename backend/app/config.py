"""Application settings for document processing, embeddings, and retrieval."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Resolve project files relative to this source tree, not the process's cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value.strip() if value and value.strip() else default


def _positive_int(key: str, default: int) -> int:
    raw = _env(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}.") from exc
    if value < 1:
        raise ValueError(f"{key} must be at least 1.")
    return value


def _absolute_path(value: str | Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


@dataclass(frozen=True)
class EmbeddingSettings:
    model_name: str = _env("EMBEDDING_MODEL_NAME", "BAAI/bge-small-en-v1.5")
    device: str = _env("EMBEDDING_DEVICE", "auto")
    batch_size: int = _positive_int("EMBEDDING_BATCH_SIZE", 32)
    normalize_embeddings: bool = True
    expected_dimension: int = _positive_int("EMBEDDING_EXPECTED_DIMENSION", 384)
    query_instruction: str | None = os.environ.get("EMBEDDING_QUERY_INSTRUCTION")

    def __post_init__(self) -> None:
        if not self.model_name.strip():
            raise ValueError("EMBEDDING_MODEL_NAME must not be empty.")
        if not self.device.strip():
            raise ValueError("EMBEDDING_DEVICE must not be empty.")
        if isinstance(self.batch_size, bool) or self.batch_size < 1:
            raise ValueError("EMBEDDING_BATCH_SIZE must be at least 1.")
        if isinstance(self.expected_dimension, bool) or self.expected_dimension < 1:
            raise ValueError("EMBEDDING_EXPECTED_DIMENSION must be at least 1.")
        instruction = self.query_instruction
        if instruction is None:
            model_name = self.model_name.lower()
            if "bge" in model_name and "zh" in model_name:
                instruction = "为这个句子生成表示以用于检索相关文章："
            elif "bge" in model_name:
                instruction = "Represent this sentence for searching relevant passages: "
            else:
                instruction = ""
        elif not isinstance(instruction, str):
            raise ValueError("EMBEDDING_QUERY_INSTRUCTION must be a string.")
        elif instruction.strip().lower() in {"none", "off", "false"}:
            instruction = ""
        if not isinstance(instruction, str) or len(instruction) > 512:
            raise ValueError("EMBEDDING_QUERY_INSTRUCTION must be a string of at most 512 characters.")
        object.__setattr__(self, "query_instruction", instruction)


@dataclass(frozen=True)
class VectorStoreSettings:
    persist_directory: str = _absolute_path(
        _env("VECTOR_DB_PATH", "storage/vector_db")
    )
    collection_name: str = _env("VECTOR_DB_COLLECTION", "documents")

    def __post_init__(self) -> None:
        if not self.collection_name.strip():
            raise ValueError("VECTOR_DB_COLLECTION must not be empty.")
        object.__setattr__(self, "persist_directory", _absolute_path(self.persist_directory))


@dataclass(frozen=True)
class RetrievalSettings:
    default_top_k: int = _positive_int("RETRIEVAL_DEFAULT_TOP_K", 5)
    max_top_k: int = _positive_int("RETRIEVAL_MAX_TOP_K", 50)

    def __post_init__(self) -> None:
        for name, value in (
            ("default_top_k", self.default_top_k),
            ("max_top_k", self.max_top_k),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if self.default_top_k > self.max_top_k:
            raise ValueError("RETRIEVAL_DEFAULT_TOP_K must not exceed RETRIEVAL_MAX_TOP_K.")


EMBEDDING_SETTINGS = EmbeddingSettings()
VECTOR_STORE_SETTINGS = VectorStoreSettings()
RETRIEVAL_SETTINGS = RetrievalSettings()

APP_ENV = _env("APP_ENV", "production").lower()
if APP_ENV not in {"development", "test", "production"}:
    raise ValueError("APP_ENV must be development, test, or production.")
API_ACCESS_TOKEN = _env("API_ACCESS_TOKEN", "")
if API_ACCESS_TOKEN and (
    not API_ACCESS_TOKEN.isascii()
    or len(API_ACCESS_TOKEN) < 32
    or any(character.isspace() for character in API_ACCESS_TOKEN)
):
    raise ValueError(
        "API_ACCESS_TOKEN must be ASCII, contain at least 32 characters, and contain no whitespace."
    )
if APP_ENV != "development" and not API_ACCESS_TOKEN:
    raise ValueError(
        "API_ACCESS_TOKEN is required unless APP_ENV=development is explicitly set."
    )
