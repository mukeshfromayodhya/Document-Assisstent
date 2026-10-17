# Document Assistant — Backend

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-REST%20API-009688?logo=fastapi&logoColor=white)
![ChromaDB](https://img.shields.io/badge/ChromaDB-vector%20store-orange)

A FastAPI backend that lets users upload PDF documents and ask questions about them in natural language. It implements a complete **Retrieval-Augmented Generation (RAG)** pipeline: PDF text extraction (with OCR fallback), chunking, local embeddings, vector search, and grounded answer generation with source citations. It also provides guest sessions, user accounts, and saved per-account chat history.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Usage Example](#usage-example)
- [Security](#security)
- [Data Storage](#data-storage)
- [Design Notes & Limitations](#design-notes--limitations)

---

## Features

**PDF understanding**
- PDF upload with strict validation (extension, `%PDF-` signature, 10 MB size cap, page-count cap).
- Layout-aware text extraction with **PyMuPDF**, plus automatic **Tesseract OCR** for scanned or image-heavy pages.
- Text cleaning, heading detection, and overlapping chunking (1,000 characters with 100-character overlap).
- Local embeddings using `BAAI/bge-small-en-v1.5` (384 dimensions) via `sentence-transformers`, running on CPU or CUDA.
- Persistent vector search with **ChromaDB** using cosine similarity.
- Content-hash caching: re-uploading an identical PDF reuses its existing vectors instead of re-processing it.

**Question answering**
- Grounded answers from any **OpenAI-compatible** chat-completions provider (OpenAI, NVIDIA NIM, Gemini's compatible endpoint, local servers, and others).
- Numbered citations (`[S1]`, `[S2]`, …) tied to exact passages, with an endpoint to fetch the cited text.
- Prompt-injection hardening: retrieved excerpts are treated as untrusted data, and invented citations are stripped from the output.

**Accounts and sessions**
- Guest sessions (24 hours) and registered accounts (14 days).
- Saved conversation history per account.
- Per-document access control: every indexed PDF is bound to the guest session or account that uploaded it.

---

## Architecture

### Ingestion — `POST /documents/upload`

```mermaid
flowchart LR
    A[PDF upload] --> B[Validate<br/>type, size, signature]
    B --> C[SHA-256 hash<br/>becomes document_id]
    C --> D[Persist file and return 202]
    D --> E[Extract text<br/>PyMuPDF + OCR fallback]
    E --> F[Clean, structure<br/>and chunk]
    F --> G[Embed with BGE<br/>and store in ChromaDB]
    G --> H[Warm retrieval cache]
    H --> I[Status becomes indexed]
```

### Question answering — `POST /documents/ask`

```mermaid
flowchart LR
    Q[Question] --> R[Check document access]
    R --> S[Embed query]
    S --> T[Cosine search<br/>in ChromaDB]
    T --> U[Build grounded prompt<br/>excerpts S1..Sn]
    U --> V[LLM chat completion]
    V --> W[Answer with citations]
```

Upload returns `202` after the validated PDF is safely stored. The frontend polls `/documents/{document_id}/status` and enables chat only when the status is `indexed`; stage and percentage fields cover extraction, chunking, embedding, storage, and retrieval warm-up. Repeated uploads reuse the content-hash index. Page-number questions load only the requested page, document-overview questions sample passages across the full page range, and ordinary questions combine semantic ranking with cached BM25-style keyword matching.

The routes validate the HTTP boundary and delegate to the service layer (`app/services/`), so each stage (extraction, chunking, embedding, retrieval, prompting, generation) can be tested or replaced independently.

---

## Tech Stack

| Layer | Technology |
| --- | --- |
| API framework | FastAPI, Starlette, Pydantic |
| PDF processing | PyMuPDF, Tesseract OCR |
| Embeddings | sentence-transformers, PyTorch (`BAAI/bge-small-en-v1.5`) |
| Vector database | ChromaDB (persistent, cosine distance) |
| LLM access | Any OpenAI-compatible `/chat/completions` endpoint (standard-library HTTP client) |
| Accounts and metadata | SQLite (WAL mode), scrypt password hashing |

---

## Project Structure

```
<project-root>/
├── .env                              # Your configuration (not committed)
├── storage/                          # Created automatically at first run
│   ├── uploads/                      # Validated PDFs, stored as <sha256>.pdf
│   ├── vector_db/                    # ChromaDB persistent store
│   ├── accounts.sqlite3              # Accounts, sessions, chats
│   └── document_registry.sqlite3     # Document metadata and access grants
└── backend/
    ├── main.py                       # App factory, auth guard, request-size limits
    └── app/
        ├── config.py                 # Typed, validated settings
        ├── models.py                 # Shared dataclasses
        ├── routes/
        │   ├── auth.py               # /auth/* and /chats/*
        │   ├── documents.py          # /documents/upload, status, chunks, ask
        │   └── retrieval.py          # /documents/search
        └── services/
            ├── auth_service.py       # Accounts, sessions, rate limiting, chat storage
            ├── document_registry.py  # Document metadata and per-owner access
            ├── text_extractor.py     # PDF text extraction and OCR
            ├── text_cleaner.py       # Unicode and whitespace normalisation
            ├── text_processor.py     # Paragraph and heading detection
            ├── text_chunker.py       # Overlapping chunk generation
            ├── embedding.py          # Embedding model wrapper (lazy singleton)
            ├── vector.py             # ChromaDB vector store
            ├── retrieval.py          # Query -> ranked chunks
            ├── retrieval_adapter.py  # Retrieval adapter for the RAG pipeline
            ├── prompt_builder.py     # Citation-ready, size-bounded prompts
            ├── llm_service.py        # OpenAI-compatible chat client
            └── rag_service.py        # End-to-end retrieve -> prompt -> answer
```

> **Important:** `PROJECT_ROOT` is the directory that **contains** `backend/`. The `.env` file and the `storage/` directory live there, not inside `backend/`.

---

## Getting Started

### Prerequisites

- **Python 3.10 or newer** (developed on Python 3.12)
- **Tesseract OCR** — required only for scanned or image-based PDFs
  - Windows: install from the [UB Mannheim builds](https://github.com/UB-Mannheim/tesseract/wiki) and add it to `PATH`
  - macOS: `brew install tesseract`
  - Ubuntu/Debian: `sudo apt install tesseract-ocr`
- An API key for an OpenAI-compatible LLM provider
- *(Optional)* An NVIDIA GPU with CUDA for faster embeddings; the backend falls back to CPU automatically

### 1. Create a virtual environment

Run these commands from the project root (the folder that contains `backend/`).

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

> The first request that needs embeddings downloads the embedding model (about 130 MB) and caches it locally. Internet access is required for that first run.

### OCR for scanned PDFs (one-time setup)

Tesseract is **not a Python package**, so `pip install -r requirements.txt` cannot install it. You only need Tesseract when a PDF contains scanned/image-only pages; PDFs with selectable text work without it.

- **Windows:** install Tesseract from the [UB Mannheim Windows builds](https://github.com/UB-Mannheim/tesseract/wiki). Keep the installer option that adds Tesseract to `PATH`, or add its install folder to `PATH` yourself. Close and reopen PowerShell (and restart the backend), then verify with `tesseract --version`.
- **macOS:** run `brew install tesseract`, then verify with `tesseract --version`.
- **Ubuntu/Debian:** run `sudo apt install tesseract-ocr`, then verify with `tesseract --version`.

If OCR language data is missing, install the language pack and set `OCR_LANGUAGE` to its code (the default is `eng`). If the app detects that OCR is needed but unavailable, it marks indexing as failed and shows these setup instructions; install Tesseract and retry the upload.

### 3. Configure the environment

Create a `.env` file in the project root:

```env
APP_ENV=development
API_ACCESS_TOKEN=replace-with-a-random-string-of-at-least-32-chars

LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=your-model-name
LLM_API_KEY=your-provider-api-key
```

Generate a strong access token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 4. Run the server

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

The module path is `backend.main:app`, so always launch from the project root.

- Health check: `GET http://127.0.0.1:8000/`
- Interactive API docs (only when `APP_ENV=development`): `http://127.0.0.1:8000/docs`

---

## Configuration

All settings are read from environment variables (or `.env`). Invalid values fail fast at startup with a clear error message.

### Core

| Variable | Default | Description |
| --- | --- | --- |
| `APP_ENV` | `production` | `development`, `test`, or `production`. API docs are exposed only in `development`. |
| `API_ACCESS_TOKEN` | *(empty)* | Bearer token required on all `/documents`, `/auth`, and `/chats` requests. Must be ASCII, at least 32 characters, with no whitespace. **Required unless `APP_ENV=development`.** |
| `SESSION_COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS. |
| `AUTH_DB_PATH` | `storage/accounts.sqlite3` | Location of the accounts database. |

### LLM provider

| Variable | Default | Description |
| --- | --- | --- |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Provider base URL. Must be HTTPS unless it points to localhost. |
| `LLM_MODEL` | *(required)* | Model name supported by your provider. |
| `LLM_API_KEY` | *(empty)* | API key. Falls back to `OPENAI_API_KEY` or `NVIDIA_API_KEY` when the base URL matches those providers. |
| `LLM_API_KEY_HEADER` | `Authorization` | Header used to send the key. |
| `LLM_API_KEY_PREFIX` | `Bearer` | Prefix placed before the key. Set empty for providers that need none. |
| `LLM_CHAT_COMPLETIONS_PATH` | `chat/completions` | Relative endpoint path. |
| `LLM_MAX_TOKENS_FIELD` | `max_tokens` | Request field name for the output limit (some providers use `max_completion_tokens`). |
| `LLM_SEND_TEMPERATURE` | `true` | Set to `false` for models that reject a temperature value. |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature (0–2). |
| `LLM_MAX_OUTPUT_TOKENS` | `800` | Maximum answer length in tokens. |
| `LLM_TIMEOUT_SECONDS` | `60` | Request timeout. |

### Embeddings and retrieval

| Variable | Default | Description |
| --- | --- | --- |
| `EMBEDDING_MODEL_NAME` | `BAAI/bge-small-en-v1.5` | Any `sentence-transformers` model. |
| `EMBEDDING_DEVICE` | `auto` | `auto`, `cpu`, or `cuda`. |
| `EMBEDDING_BATCH_SIZE` | `32` | Batch size for embedding chunks. |
| `EMBEDDING_EXPECTED_DIMENSION` | `384` | Must match the model's output dimension. |
| `EMBEDDING_QUERY_INSTRUCTION` | *(auto for BGE)* | Query prefix. Use `none` to disable. |
| `VECTOR_DB_PATH` | `storage/vector_db` | ChromaDB persistence directory. |
| `VECTOR_DB_COLLECTION` | `documents` | Collection name. |
| `RETRIEVAL_DEFAULT_TOP_K` | `5` | Chunks returned when `top_k` is omitted. |
| `RETRIEVAL_MAX_TOP_K` | `50` | Upper bound; larger requests are clamped. |

### PDF processing

| Variable | Default | Description |
| --- | --- | --- |
| `PDF_MAX_PAGES` | `500` | Maximum pages per document. |
| `OCR_DPI` | `240` | OCR render resolution (maximum 600). |
| `OCR_LANGUAGE` | `eng` | Tesseract language code(s), for example `eng` or `eng+hin`. |
| `PDF_MAX_OCR_PIXELS` | `500000000` | Cumulative OCR pixel budget per document. |

> If you change the embedding model, update `EMBEDDING_EXPECTED_DIMENSION` and re-index your documents. The vector store rejects dimension mismatches and non-cosine collections.

---

## API Reference

Every endpoint under `/documents`, `/auth`, and `/chats` requires the header `Authorization: Bearer <API_ACCESS_TOKEN>` when a token is configured. Document and chat endpoints additionally require a session cookie (`adi_session`), obtained from `/auth/guest`, `/auth/signup`, or `/auth/login`.

### Authentication and sessions

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/auth/session` | Return the current session state (`authenticated`, `email`). |
| `POST` | `/auth/guest` | Start a guest session (24 h). Reuses an existing session if present. |
| `POST` | `/auth/signup` | Create an account and sign in. Body: `{ "email", "password" }`. |
| `POST` | `/auth/login` | Sign in. Rotates the session and keeps the guest's uploaded documents. |
| `POST` | `/auth/logout` | Invalidate the session and clear the cookie. |

### Chat history (account required)

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/chats` | List the 60 most recently updated chats. |
| `PUT` | `/chats/{chat_id}` | Create or update a chat. Body: `{ "chat": { "id", "title", "messages", "document", ... } }`. |
| `DELETE` | `/chats/{chat_id}` | Delete a chat. |

### Documents

| Method | Endpoint | Description |
| --- | --- | --- |
| `POST` | `/documents/upload` | Upload a PDF (`multipart/form-data`, field `file`). Returns `202`, `document_id`, and the initial indexing stage. |
| `GET` | `/documents/{document_id}/status` | Indexing stage and percentage; includes pages, chunks, timings, cache hits, and `status` (`indexing`, `indexed`, or `failed`). |
| `GET` | `/documents/{document_id}/chunks/{chunk_id}` | Exact text of a cited passage. |
| `POST` | `/documents/search` | Page-aware hybrid search. Body: `{ "query", "document_id", "top_k?", "min_score?" }`. Returns ranked chunks with scores. |
| `POST` | `/documents/ask` | Full RAG answer. Same body as search. Returns `answer`, `sources`, and `small_talk`. |

**Typical status codes:** `202` upload accepted for indexing · `400` invalid file or input · `401` missing or invalid token/session · `404` document not available to this session · `409` indexing is not complete · `413` file too large · `422` validation failure · `429` rate limit or upload capacity reached · `503` embedding, vector, or LLM service unavailable.

---

## Usage Example

```bash
API=http://127.0.0.1:8000
TOKEN=<your API_ACCESS_TOKEN>

# 1. Start a guest session (stores the session cookie)
curl -c cookies.txt -X POST "$API/auth/guest" \
     -H "Authorization: Bearer $TOKEN"

# 2. Upload a PDF — note the returned document_id
curl -b cookies.txt -X POST "$API/documents/upload" \
     -H "Authorization: Bearer $TOKEN" \
     -F "file=@sample.pdf"

# 3. Ask a question about it
curl -b cookies.txt -X POST "$API/documents/ask" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"query": "What are the key findings?", "document_id": "<document_id>"}'
```

Example `/documents/ask` response:

```json
{
  "query": "What are the key findings?",
  "answer": "The study reports a 12% improvement in ... [S1]. It also notes ... [S2].",
  "small_talk": false,
  "sources": [
    { "citation": "[S1]", "chunk_id": "14", "page_number": 3 },
    { "citation": "[S2]", "chunk_id": "21", "page_number": 5 }
  ]
}
```

Use `GET /documents/{document_id}/chunks/{chunk_id}` to display the exact passage behind each citation.

---

## Security

- **API access token** — constant-time bearer-token comparison; the token is never logged. In any environment other than `development`, the server refuses to start without one.
- **Password storage** — scrypt (`N=2^17, r=8, p=1`) with a per-user random salt. Passwords are never stored in plaintext.
- **Sessions** — random 256-bit tokens, rotated on sign-in; only SHA-256 hashes are stored. Cookies are `HttpOnly` and `SameSite=Strict`, and `Secure` when `SESSION_COOKIE_SECURE=true`.
- **Brute-force protection** — five failed sign-ins lock the email/client bucket for 15 minutes. Unknown-account logins perform a dummy hash to equalise timing.
- **Document isolation** — every document is bound to the guest or account that uploaded it; access is verified on every status, chunk, search, and ask request. Unauthorised access returns `404`.
- **Upload hardening** — extension and `%PDF-` signature checks, sanitised filenames, streamed size enforcement, content-addressed storage, and a limit of two concurrent uploads.
- **Request limits** — 1 MB for JSON bodies, byte-counted (including chunked requests); unauthenticated requests are rejected before their body is parsed.
- **LLM client** — HTTPS enforced for non-local providers, redirects disabled so credentials can't be forwarded, and response size capped at 4 MB.
- **Prompt-injection defence** — retrieved text is passed as untrusted data, and any citation the model invents is removed.

---

## Data Storage

| Path | Contents |
| --- | --- |
| `storage/uploads/` | Validated PDFs, named by their SHA-256 hash |
| `storage/vector_db/` | ChromaDB collection (chunk text, embeddings, metadata) |
| `storage/accounts.sqlite3` | Accounts, hashed sessions, login-attempt limits, saved chats |
| `storage/document_registry.sqlite3` | Document metadata, filename aliases, per-owner access grants |

Back up the whole `storage/` directory to preserve accounts, documents, and chat history.

---

## Design Notes & Limitations

- **PDF only.** Other file types are rejected at upload.
- **Language.** The default embedding model and OCR language are English. For other languages, change `EMBEDDING_MODEL_NAME` (and the matching dimension) and `OCR_LANGUAGE`.
- **One document per question.** Search and ask require a `document_id`; cross-document search is not exposed.
- **Single-process design.** Per-document locks and the upload limiter are in-process. Run a single Uvicorn worker; for multi-worker or multi-host deployments, replace these with a shared mechanism.
- **Local-first storage.** SQLite and an on-disk ChromaDB suit a single-node deployment. Scaling out would require a client/server database and vector service.
- **Password policy.** New accounts require passwords of 12–128 characters with a letter, a number, and a special character. Existing accounts may continue signing in with their previously accepted passwords. This local service does not currently implement 2FA, password reset, or email verification.
- **Keep it on loopback.** The service is designed to sit behind the project's local frontend, which proxies requests and injects the API bearer token so it never reaches browser code. If you expose it publicly, put it behind HTTPS and set `SESSION_COOKIE_SECURE=true`.
- **Documents indexed before access control.** A PDF indexed before ownership tracking existed has no access grant. Re-upload the same file while signed in (or in the relevant guest session) to grant access; the content-hash cache reuses the existing extraction and vectors, so nothing is reprocessed.
