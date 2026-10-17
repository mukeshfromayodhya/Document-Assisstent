# Document Assistant — Testing and Verification

This document distinguishes supported project tests from manual checks and temporary workspace artifacts. It does not claim that the checklist has been run on every OS, provider, or PDF type.

## Current test-suite status

The backend and frontend archives do not currently include a maintained automated test suite, a CI workflow, or a documented pytest command. The project requirements file contains runtime dependencies and does not install pytest as a development dependency.

Several `work/verify_*.py` files exist in this Codex workspace as one-off verification scripts. They are not part of the backend or frontend deliverables, some include machine-specific Windows paths, and they should not be treated as a supported test command or portable regression suite.

Before calling a release tested, run the manual workflow below on the intended machine and record the OS, Python and Node versions, embedding device, PDF type, and LLM provider. Add portable automated tests and a test dependency group before relying on CI claims.

## Manual end-to-end smoke check

Run against a fresh local project environment. Use non-sensitive sample PDFs.

| Step | Action | Expected behavior |
|---|---|---|
| Startup | Start the backend and frontend using `SETUP.md` | Backend health returns a message; UI reports backend connected |
| Guest session | Open the frontend in a fresh browser profile | UI starts a guest session without account signup |
| Valid PDF | Upload a readable, selectable-text PDF under 10 MB | Upload returns 202; tracker shows processing; questions stay disabled until status is indexed |
| Index ready | Wait for indexing completion | Status is indexed with page/chunk counts; upload UI enables questions |
| Question and source | Ask a question with a clearly stated answer in the PDF, then open a citation | Answer has source metadata; citation shows the matching page and exact indexed passage |
| Exact page | Ask for content of a known page | Returned evidence is restricted to that page when the request is recognized as a page query |
| Overview | Ask for a summary or whole-document overview | Backend selects representative passages across the document; inspect citations from multiple parts |
| Repeat upload | Upload byte-identical content again in a new chat/session that needs access | Index cache may be reused; indexing still reports ready and ownership remains session-scoped |
| Invalid inputs | Try a non-PDF, empty file, a PDF above 10 MB, and a file with a renamed extension | Frontend/backend validation shows a useful error; invalid data is not reported as indexed |
| Account history | Create an account, ask a question, refresh, then sign in again | Account chat appears in the recent list and remains available through the same backend |
| Guest history | Ask as a guest and close/reopen the page | Guest conversation content is not presented as saved history after the page session ends |
| Provider unavailable | Use a deliberately unavailable test provider in a disposable environment | Answer call returns a safe service error; indexed document remains indexed |

Testing the upload-size and invalid-PDF cases can consume time and temporary disk space. Do not use production documents for fault injection.

## Important regression areas

When modifying the corresponding backend behavior, verify:

- PDF file signature, empty-file, byte limit, page-count limit, and password-protected PDF handling.
- Text extraction for text PDFs and OCR fallback for scanned PDFs with and without Tesseract installed.
- Page metadata, heading/list structure, chunk boundaries, overlap, and chunk-to-page citation mapping.
- Upload state transitions: queued/indexing, monotonic progress, ready, failed, and re-upload/cache hit.
- Readiness gate: search/ask return 409 until indexed; no UI state presents a merely accepted upload as ready.
- Exact page retrieval and document-wide representative passage selection.
- Hybrid semantic/lexical retrieval, no-result behavior, and bounded top-k.
- RAG answer grounding, small-talk behavior, source-label normalization, and citation chunk lookups.
- Session ownership across guest, account, sign-in, and chat history restoration.
- API bearer-token behavior, upload concurrency, request-size limits, and same-origin proxy checks.
- Frontend keyboard controls, loading/errors, narrow-screen navigation, theme preference, and accessible citation dialogs.

## Automated test structure to add

The test suite should be introduced as maintained source code rather than reusing machine-bound scratch scripts. A reasonable starting split is:

1. **Unit tests:** text cleaner, text processor, chunker, retrieval ranking, prompt construction, citation normalization, configuration validation.
2. **Persistence tests:** registry progress transitions and access grants; auth session expiry and chat size/ownership validation; vector collection metadata and exact-chunk lookup.
3. **API tests:** FastAPI TestClient with temporary SQLite/Chroma paths and injected fake embedding/vector/LLM services. Avoid loading PyTorch or contacting a real provider in ordinary tests.
4. **Integration tests:** one generated text PDF through upload → index → ask → citation. Run scanned-PDF OCR tests only in an environment where the Tesseract binary and language data are explicitly available.
5. **Frontend checks:** manual browser flow or a later browser automation suite against a controlled backend. The current frontend has no test runner.

The repository does not yet define these test dependencies or commands. Add an explicit development dependency set and document a single command (for example, pytest) only when the maintained tests are committed.

## RAG quality is separate from API availability

An HTTP 200 response does not prove that retrieval selected the right passages or that an answer is supported. Use the evaluation protocol in [docs/RAG_EVALUATION.md](docs/RAG_EVALUATION.md) for retrieval relevance, answer support, citation accuracy, and latency. The project currently has no published RAG benchmark results.
