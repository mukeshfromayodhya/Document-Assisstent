# Document Assistant — System Architecture

This document describes the implementation currently present in the backend and frontend source archives. It is an implementation guide, not a promise of hosted or multi-node capabilities. Product goals and acceptance criteria are in [PRD.md](../PRD.md); setup steps are in [SETUP.md](../SETUP.md); request contracts are in [API.md](API.md).

## System context

~~~mermaid
flowchart LR
    U["Browser user"] -->|Same-origin UI| N["Node frontend<br/>proxy"]
    N -->|Allowlisted API| F["FastAPI<br/>backend"]
    F -->|PDFs and state| S["Local storage"]
    F -->|Chunks and vectors| C["Persistent ChromaDB<br/>vectors"]
    F -->|Local embeddings| E["Sentence Transformers<br/>and PyTorch"]
    F -->|Retrieved evidence| L["Configured LLM<br/>provider"]
    C -->|Ranked chunks| F
    F -->|Answer and source IDs| N
    N --> U
~~~

The frontend is plain HTML, CSS, and browser JavaScript. Its Node.js 18+ server serves a fixed set of static files and proxies a restricted API allowlist. The browser does not call the provider or receive the backend bearer token. The FastAPI backend owns identity checks, PDF processing, retrieval, and answer generation.

## Document ingestion

~~~mermaid
flowchart TD
    A["Choose PDF"] --> B["Browser upload<br/>progress"]
    B --> C["Node proxy"]
    C --> D["Validate session, PDF<br/>signature, and limits"]
    D --> E["SHA-256 ID and<br/>stored PDF"]
    E --> F["HTTP 202<br/>queued"]
    F --> G["PyMuPDF<br/>text extraction"]
    G --> H{"Scanned page<br/>needs OCR?"}
    H -->|Yes| I["Tesseract OCR"]
    H -->|No| J["Use extracted text"]
    I --> K["Clean and structure<br/>page text"]
    J --> K
    K --> M["Create page-aware<br/>chunks"]
    M --> N["Encode with local<br/>embedding model"]
    N --> O["Write chunks and metadata<br/>to ChromaDB"]
    O --> P["Warm retrieval<br/>index"]
    P --> Q["Persist ready<br/>status"]
    Q --> R["Frontend enables<br/>questions"]
~~~

The upload response reports acceptance for background processing; the frontend polls the status route until the registry reports **indexed** or **failed**. The current worker is scheduled through FastAPI BackgroundTasks inside the backend process. There is no external queue or distributed worker. Status and progress are persisted in SQLite, but a process restart is not a durable job-queue recovery mechanism.

The processing stages are extraction, text structuring, chunking, embedding, vector storage, retrieval optimization, and finalization. The current chunker uses approximately 1,000 characters with 100 characters of overlap and keeps each chunk's page and heading metadata.

## Question answering and retrieval

1. The route requires a valid API token when configured, a current guest/account session, access to the requested document, and a completed index.
2. Exact-page questions are handled by selecting chunks on the requested page. Document overview requests use representative passages spread across the page range.
3. Other questions use the persisted vector search together with a cached BM25-style lexical score. The current fusion weights semantic similarity at 0.58 and lexical score at 0.42.
4. The RAG service builds bounded context from the retrieved passages and sends it to the configured OpenAI-compatible chat-completions endpoint.
5. The answer normalizes citations to source labels such as [S1]. The browser uses the returned chunk ID to fetch the exact source text.

The selected embedding model is BAAI/bge-small-en-v1.5 by default and produces 384-dimensional vectors. The vector collection uses cosine distance. Changing the embedding model or its dimension requires a planned vector rebuild and re-indexing; the backend rejects a dimension mismatch.

## Components and source map

| Component | Implementation | Responsibility |
|---|---|---|
| API application and security middleware | backend/main.py | FastAPI setup, token guard, request-body bounds, upload concurrency cap, and development-only API docs |
| HTTP routes | backend/app/routes/auth.py, documents.py, retrieval.py | Authentication, chat persistence, upload/status/source, search, and answer request boundaries |
| PDF extraction | backend/app/services/text_extractor.py | Page text extraction, image-only OCR fallback, and document/page/OCR limits |
| Text preparation | text_cleaner.py, text_processor.py, text_chunker.py | Clean positioned text, preserve paragraph/list/heading structure, and create page-aware chunks |
| Embedding | embedding.py | Lazy-load Sentence Transformers and PyTorch; encode chunks and queries |
| Vector store | vector.py | Persistent Chroma collection with chunk text, embeddings, and source metadata |
| Retrieval | retrieval.py, retrieval_adapter.py | Exact-page selection, document-wide sampling, semantic search, lexical fusion, and a small in-process cache |
| Answer generation | rag_service.py, prompt_builder.py, llm_service.py | Grounded context, provider request, and source/citation normalization |
| SQLite metadata | document_registry.py, auth_service.py | Document status, filename aliases, document ownership, accounts, sessions, and account chats |
| Browser UI | frontend/public/ | Upload state, conversation and account UI, answer display, and citation dialog |
| Frontend proxy | frontend/server.mjs | Static asset allowlist, same-origin checks, backend forwarding, and server-side API token |

## Persistence and ownership

- Uploaded PDFs are saved under storage/uploads/ using their SHA-256 content ID as the filename. Re-uploading identical bytes can reuse the existing vectors.
- storage/document_registry.sqlite3 records document status, upload metadata, filename aliases, timings, and owner grants.
- storage/vector_db/ holds the persistent Chroma collection, including chunk text and page/heading metadata.
- storage/accounts.sqlite3 holds local account records, session-token hashes, login limits, and signed-in chats.
- Guest conversations are held in browser memory. Signed-in chats are saved in the backend.
- A guest session lasts 24 hours; an account session lasts 14 days. Browser cookies are HttpOnly and SameSite=Strict; Secure is enabled when SESSION_COOKIE_SECURE=true.

The document ID is a content hash, not a secret. Authorization still depends on the current session's owner grant. Document and chat APIs are designed for a local single-machine deployment.

## Deployment constraints

- Run one Uvicorn worker. Upload concurrency guards, document locks, and retrieval caches are process-local.
- SQLite and local Chroma persistence are not a shared multi-host coordination layer.
- The in-process background task is appropriate for this local app but is not a durable job broker.
- Keep both servers on loopback for local use. A reverse proxy for HTTPS needs a matching FRONTEND_ORIGIN and SESSION_COOKIE_SECURE=true; this alone is not a complete public deployment design.
- If a remote LLM provider is configured, retrieved document excerpts are sent to it for generation. Keep sensitive files local unless that provider's data handling is acceptable.

See [DATABASE.md](DATABASE.md) for persistence details and backup guidance, and [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) for operational failure cases.
