# Document Assistant — Troubleshooting

Use the symptom and check below to narrow the issue. Backend logs usually contain the useful processing stage; user-facing error text is intentionally safer and less detailed.

## Backend startup

### Configuration error for `API_ACCESS_TOKEN`

Outside `APP_ENV=development`, the backend requires `API_ACCESS_TOKEN`. It must be ASCII, at least 32 characters, and contain no whitespace. For local development only, set `APP_ENV=development`; the application will warn that bearer-token protection is disabled. Keep the backend bound to loopback when no token is configured.

If the token is configured, copy the same value into `frontend/.env`. Do not add it to browser JavaScript or commit it.

### Windows reports `WinError 4551` or blocks `torch_python.dll`

The local embedding runtime cannot load because Windows Application Control blocked PyTorch or one of its dependencies. Selecting CPU mode or reinstalling packages does not override a device policy. Use an approved Python runtime and ask the administrator to allow the trusted PyTorch runtime. Restart the backend after the allowed environment is ready, then retry the upload.

### Import error or missing Python package

Activate the intended virtual environment and install from the project-root `requirements.txt`. Confirm that the Python used to start Uvicorn is the same interpreter where the packages were installed. Use `python -m pip`, not a potentially unrelated global `pip`.

## Upload and indexing

### HTTP 202 appears, but chat is still disabled

202 means accepted for background processing, not indexed. Keep the upload tracker open and check `GET /documents/{document_id}/status`. Ask/search requests return 409 before status is indexed. If the tracker reports a safe error, fix that cause and use the upload retry.

The worker uses an in-process FastAPI background task, not a durable external job queue. If the backend is stopped during indexing, restart it and re-upload the same PDF to run indexing again; if a complete vector index already exists, the content-hash path can reuse it. Do not expect a separate worker to continue the old task.

### Tesseract or OCR error

Only scanned/image-only pages require OCR. Install Tesseract as a system application, add it to `PATH`, reopen the terminal, and verify `tesseract --version`. Install the correct language data and set `OCR_LANGUAGE` (default `eng`), then restart the backend and retry. Tesseract is not installed by Python's requirements file.

### PDF rejected as invalid, empty, too large, encrypted, or over page limit

Use an unencrypted readable PDF. The current maximum file size is 10 MB and the default page cap is 500. The backend checks the file signature and parser validity; renaming another file type to `.pdf` does not make it a valid PDF. Split files only when it is appropriate and preserve citation/page context for the questions being asked.

### Indexing fails with vector dimension or collection error

The stored collection uses cosine distance and expects the configured embedding dimension (384 by default). Do not switch the model or dimension against an existing collection without a planned re-index. Back up the data first. Rebuild/re-index only after confirming which documents and vectors must be preserved.

### First upload is unusually slow

First use can initialize or download the embedding model. OCR and CPU embedding can also be time-consuming. Watch the current indexing stage and check the backend log for the actual stage. Later questions reuse the index, but still run retrieval and an LLM provider request.

## Answer and retrieval

### Answer request returns 503

The document may remain indexed while the answer service is unavailable. Confirm `LLM_BASE_URL`, `LLM_MODEL`, and the provider key in the project-root `.env`. The configured endpoint must implement the expected OpenAI-compatible chat-completions API. Non-local provider URLs must use HTTPS. Check provider availability, model name, rate limits, and timeout.

The embedding model and the answer-generation LLM are separate settings. A working embedding model does not prove that the chat-completions provider is configured.

### Answer is too general, summary misses important content, or page question cites another page

Open each citation and check the extracted passage and page number. If the text is missing or garbled, inspect the PDF extraction/OCR result; if the text is present but the wrong passage is selected, record the query and relevant expected passages for the RAG evaluation dataset. Page queries and overview summaries use different retrieval paths; an API 200 alone does not establish answer quality.

### Source passage returns 404

The current session must have access to the document, and the cited chunk must exist in the vector collection. Sign in to the account that owns the chat or upload the same PDF again in the current session. Check that the complete storage set was restored if files were moved.

## Frontend and API proxy

### Frontend says backend offline

Start the backend, verify `BACKEND_URL` in `frontend/.env`, and request the backend health route directly. The default is `http://127.0.0.1:8000`. Restart the frontend process after changing its environment file.

### Protected route returns 401

Check that frontend and backend `API_ACCESS_TOKEN` values match exactly. If the backend token is unset, its warning means token protection is disabled; in that local development case leave the frontend token empty. A session may also be missing or expired; reload and start/sign in to a session.

### State-changing proxy request returns 403

The frontend proxy requires an exact same-origin `Origin` for POST/PUT/DELETE requests. With a trusted TLS reverse proxy, set `FRONTEND_ORIGIN` to the browser-visible scheme and host only, with no path or trailing route. Keep the upstream frontend inaccessible from untrusted networks.

### Frontend port is already in use

Set `FRONTEND_PORT` to another available port in `frontend/.env`, restart the server, and open the corresponding URL. If a reverse proxy is used, update its target and `FRONTEND_ORIGIN` too.

### Search/ask returns 409 or 429

- **409:** indexing is not complete. Wait until the status reports indexed.
- **429:** the two-upload concurrency limit or authentication rate limit may have been reached. Wait for the indicated retry window and retry.

### Frontend returns API route not found

The Node proxy deliberately allowlists a limited set of API routes. Use a supported path from [API.md](docs/API.md), or call the backend directly from a trusted development client.

## Data recovery

Keep a backup of uploads, registry SQLite, account SQLite, and the persistent Chroma directory as one set. Stop the backend before copying the set. If only one store is restored, metadata, source passages, and vectors may no longer agree. See [DATABASE.md](docs/DATABASE.md).
