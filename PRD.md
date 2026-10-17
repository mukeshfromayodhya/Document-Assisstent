# Product Requirements Document

## Document Assistant

**Status:** Product baseline and implementation requirements  
**Version:** 1.0  
**Prepared:** 16 October 2026  
**Product name:** Document Assistant  
**Product type:** Local-first PDF question-answering workspace

> This PRD is based on inspection of the supplied backend and frontend source, their READMEs, the observed UI screenshots, and the runtime failure log shared by the project owner. Existing behavior is labeled **Current**; proposed requirements and targets are labeled **Target**. The runtime failure demonstrates that the current local embedding dependency can be blocked by Windows Application Control; it is treated as a first-class product reliability issue.

## 1. Executive summary

Document Assistant lets a user upload one PDF, wait for it to be fully processed, ask questions grounded in that document, and inspect the exact cited passages. It combines a browser workspace with a local FastAPI service, PDF text extraction and OCR, chunking, embeddings, ChromaDB retrieval, and an OpenAI-compatible language model. Guests can use the app; registered users can save chat history.

The product's key promise is not merely “chat with a PDF.” It is: **the user can tell when a document is ready, receive useful answers tied to relevant pages, and verify those answers against the source.** To keep that promise, the system must treat upload, extraction, indexing, retrieval, generation, and source display as one observable workflow. A file accepted by HTTP is not yet ready for questions.

The codebase already implements much of this baseline: PDF validation, asynchronous indexing with progress, content-hash reuse, page-aware and hybrid retrieval, source citations, guest and account sessions, and a polished responsive chat UI. The supplied runtime log reveals an important deployment gap: PyTorch fails to load (`WinError 4551`) under the user's Windows Application Control policy, so indexing fails after upload acceptance. Target requirements below make dependency readiness and actionable failure recovery part of the upload experience.

## 2. Product context and evidence

### 2.1 Current product behavior (observed in source)

- The frontend is served locally by a dependency-free Node.js server and proxies selected API calls to FastAPI.
- A guest may upload PDFs and ask questions. Guest chats live in page memory. Signed-in chats are persisted in the backend SQLite database.
- Uploads accept PDF only, validate the filename, extension and PDF signature, and enforce a 10 MB limit. The extractor enforces a 500-page limit and bounds OCR workload and extracted text.
- PyMuPDF extracts text and invokes Tesseract OCR for pages where native text is insufficient. The current default OCR language is English.
- Text is cleaned, structured, chunked with overlap, embedded using `BAAI/bge-small-en-v1.5` through Sentence Transformers/PyTorch, and stored in persistent ChromaDB.
- The backend returns HTTP 202 after accepting and saving a PDF. The browser polls document status and is intended to enable questions after indexing completes.
- Re-uploading identical bytes uses a SHA-256 content identifier and reuses existing indexed content when available.
- Answers use an OpenAI-compatible chat-completions endpoint, citations such as `[S1]`, and a source endpoint that returns the exact cited chunk.
- Retrieval includes page-specific handling and a broad document-overview path, with semantic ranking and cached keyword ranking.
- The API has guest/account sessions, per-owner document access checks, stored chat history for accounts, and a frontend proxy that keeps the backend bearer token out of browser code.
- The backend README recommends a single Uvicorn worker because locks and upload capacity controls are process-local. The service is local-first, with SQLite and on-disk ChromaDB.
- No document deletion API is currently described. Other file types, multi-document questions, account recovery, email verification, and persisted guest histories are not currently supported.

### 2.2 Reported runtime failure

The owner shared logs showing `/documents/upload` returning `202 Accepted`, followed by a background indexing exception at embedding initialization. Windows blocked `torch_python.dll` with `WinError 4551`. The PDF was therefore accepted but never made ready. This proves that:

1. HTTP upload acceptance and successful document readiness must be clearly distinguished in UI copy and status.
2. The backend must persist and return the failed stage and a safe, actionable error summary.
3. Dependency and model readiness must be checked or surfaced before the user spends time uploading a document.
4. The deployment guide must cover Windows policy blocking native ML libraries, and the architecture should permit an approved alternative embedding provider/runtime.

### 2.3 Product assumptions to validate

- Initial target is a single-user or small local workspace, not a public multi-tenant SaaS.
- PDFs are the first and only supported document type.
- The user values grounded answers and page-level source inspection over conversational creativity.
- English is the initial supported language for semantic retrieval and OCR. Other languages need explicit model/OCR configuration and validation.
- The owner wants a finished upload/indexing step before chat starts, then faster repeated questions by reusing the warmed index and caches.
- No exact user count, document corpus profile, deployment model, or paid-provider budget has been supplied. Performance targets below are proposed measurable baselines and need validation on the target device.

## 3. Problem statement

Users need to extract information from long PDFs without manually searching every page. Generic chat systems can produce plausible but poorly supported summaries, miss requested pages, or cite irrelevant passages. In this project, asynchronous indexing can also fail after the UI reports the upload was accepted, due to local dependency and policy failures. Users need a workflow that makes readiness explicit, answers precisely, supports source verification, and avoids repeating expensive indexing work.

## 4. Product vision and value

Provide a trustworthy local workspace where a user can upload a PDF once, see every processing stage, ask a question only when the index is genuinely ready, receive a concise answer grounded in relevant passages, and open each citation to verify it. The product should degrade transparently when a required local runtime or configured model provider is unavailable.

### Value propositions

- **Time saved:** find facts and synthesize a long document without manually scanning each page.
- **Trust:** every factual response can be checked against cited source text and page numbers.
- **Clarity:** indexing progress and failures are visible, rather than leaving users with a stuck spinner.
- **Repeat speed:** upload-time work is cached/warmed so subsequent questions avoid reprocessing the PDF.
- **Privacy control:** local storage and a server-side provider proxy keep documents and service credentials under the operator's configuration.

## 5. Goals and non-goals

### 5.1 Goals

1. Accept valid PDFs and complete extraction, OCR where needed, chunking, embedding, vector storage, and retrieval warm-up before enabling questions.
2. Show durable, accurate progress and explain recoverable and unrecoverable failures in plain language.
3. Return relevant, document-grounded answers with verifiable page citations.
4. Correctly answer exact-page and whole-document overview/summary requests.
5. Make repeat questions fast by reusing the document index, retrieval data, and model connections where supported.
6. Preserve access isolation between guest sessions and accounts, and protect server credentials.
7. Make the local installation dependable on Windows, macOS, and Linux configurations declared as supported, including an explicit response to blocked local ML runtimes.

### 5.2 Non-goals for the current release

- Editing, annotating, or transforming source PDFs.
- Producing legal, medical, financial, or compliance determinations as an authority.
- Training/fine-tuning a language or embedding model.
- Collaborative editing or real-time multi-user workspaces.
- Cross-document search, collections, or answers combining multiple PDFs.
- Cloud hosting, enterprise identity, roles, SSO, or organization-level administration.
- Guaranteeing OCR fidelity for handwriting, complex formulas, or every language.
- Building a general-purpose web search agent; answers are scoped to the selected PDF.

## 6. Users and jobs to be done

### Primary user: individual document reader

Examples include a student, researcher, employee, or small-business operator reading reports, manuals, books, policies, or contracts.

**Job:** “When I have a long PDF, help me locate and understand its relevant information, show me where it came from, and let me verify the answer quickly.”

### Secondary user: local workspace owner/operator

**Job:** “When I install and run the project on my computer, tell me whether the backend, OCR, embedding runtime, vector store, and language model are usable; make failures diagnosable without reading a raw traceback.”

### Guest user

**Job:** “Let me try the product without creating an account, while making it clear that chat history is temporary.”

## 7. Experience principles

1. **Ready means ready.** Do not enable question entry until all required indexing stages finish and the backend can serve retrieval.
2. **Show the current work.** Progress includes a named stage, percentage where meaningful, and failure state; it must not jump to complete prematurely.
3. **Cite, then verify.** Citations map to exact passages with original filename and PDF page number.
4. **Answer the requested scope.** Exact-page questions are constrained to the requested page; document-wide summaries sample across the document rather than returning only a few locally similar passages.
5. **Be candid about gaps.** If the source lacks evidence, say so; if extraction/OCR is incomplete, do not imply the PDF was fully read.
6. **Keep local operations understandable.** Explain provider setup and blocked native dependencies in terms users can act on.

## 8. User journeys

### 8.1 First-time upload and question

1. User opens the local web app; the UI reports backend connectivity and offers guest use or account sign-in.
2. User chooses or drops a PDF. The app validates file type/size early and explains accepted limits.
3. Upload transfers to the backend. UI distinguishes “Uploading” from “Indexing.”
4. Backend stores the file and returns a document ID and initial state. UI polls durable status.
5. UI displays extraction/OCR, text preparation/chunking, embedding, vector storage, and retrieval warm-up stages. The UI remains usable and explains that asking is unavailable until ready.
6. If processing fails, the status becomes failed, identifies the stage, offers a retry where safe, and provides a clear setup action (for example, check approved embedding runtime). No “ready” badge is shown.
7. On success, the UI shows document name, page count, chunk/index status, and asks become available.
8. User asks a question. The UI shows progress/loading and renders answer, citations, and inspectable sources.

### 8.2 Exact page question

1. User asks “What is on page 5?”
2. System recognizes the explicit page request and retrieves only page 5 content (or clearly reports that page 5 has no extractable text).
3. Answer cites page 5. It does not substitute unrelated pages as evidence.

### 8.3 Whole-document summary

1. User requests a summary, key points, or “what is in this PDF?”
2. System gathers representative content across the page range, including beginning, middle, and end as needed, not just the highest-scoring few chunks.
3. Answer presents a useful, balanced summary with source coverage across the document and no unsupported facts.

### 8.4 Repeat question

1. Once indexed, user asks follow-up questions in the same document.
2. The system reuses stored embeddings and in-process retrieval caches; it does not extract, chunk, or re-embed the PDF per question.
3. On process restart, persisted vector data is reused. Any necessary cache rebuild is bounded and reported as service warm-up, not document re-upload.

### 8.5 Sign-in and saved chat

1. Guest may create an account or sign in.
2. On successful sign-in, the account can access its own authorized documents and persisted chat history.
3. The UI communicates that old guest chats are temporary and that reopening a source may require uploading the PDF again if access ownership is not available.

## 9. Functional requirements

Priority uses **P0** (required for trustworthy release), **P1** (important next), and **P2** (later enhancement).

### 9.1 Availability and setup

- **FR-001 (P0):** Frontend reports backend health as connecting, connected, or unavailable, with a retry path.
- **FR-002 (P0):** Backend startup or health diagnostics identify whether configuration, vector storage, embedding runtime/model, OCR, and LLM configuration are ready, missing, or not yet checked. Secrets must never appear in output.
- **FR-003 (P0):** Setup documentation lists supported Python/Node versions, installation commands, required model downloads, Tesseract/OCR setup, provider configuration, and Windows native-library policy troubleshooting.
- **FR-004 (P1):** Where policy and privacy allow, an operator can select a configured local embedding runtime or a supported remote embedding provider without changing retrieval/API contracts.

### 9.2 PDF upload and indexing

- **FR-010 (P0):** Accept only valid PDFs; enforce extension/signature validation, nonempty file, 10 MB upload limit, 500-page limit, and OCR/extracted-text resource bounds.
- **FR-011 (P0):** Return an upload identifier and `processing/indexing` state only after durable file and ownership metadata are written.
- **FR-012 (P0):** Index asynchronously and persist status across browser refresh and backend restart where job recovery is supported. Status includes `status`, `stage`, `progress`, filename, page count, chunk count, OCR page count, timings, cache hit, and safe error details.
- **FR-013 (P0):** Stages cover queued, extraction/OCR, text cleanup/structure, chunking, embedding, vector storage, retrieval warm-up, indexed/ready, and failed. Percentages are monotonic within one attempt, except an explicitly labeled retry.
- **FR-014 (P0):** Only mark a document indexed when the vector data is committed, expected dimension validated, and the retrieval path can return content for that document.
- **FR-015 (P0):** `/ask` and search reject documents that are not indexed with a stable conflict response. The UI prevents the avoidable request and keeps the state authoritative from backend status.
- **FR-016 (P0):** A failed job exposes a human-readable summary plus a stable machine-readable failure code/stage. Raw filesystem paths, credentials, and full tracebacks are not returned to the browser.
- **FR-017 (P0):** Retry is safe and idempotent: it does not create duplicate index entries or corrupt a valid pre-existing index. UI offers retry only when the error is retryable or after configuration is fixed.
- **FR-018 (P0):** Identical file content may reuse the existing extraction/vector index, but access grants remain bound to the correct guest/account. UI indicates reuse where useful.
- **FR-019 (P1):** Provide cancel/delete for an in-progress upload and document removal that consistently cleans metadata, access grants, source PDF, vector records, and related chat references, subject to confirmation and retention rules.
- **FR-020 (P1):** Support resumable or restart-recoverable jobs so a process restart does not leave a document permanently in `indexing`.

### 9.3 Retrieval and answering

- **FR-030 (P0):** Answer requests are scoped to one authorized indexed document, with bounded query length and top-k settings.
- **FR-031 (P0):** Combine semantic and lexical matching so exact names, dates, identifiers, and terminology are not lost when semantic similarity is weak.
- **FR-032 (P0):** Explicit page requests filter retrieval to the requested page or pages; out-of-range pages receive a direct, clear response.
- **FR-033 (P0):** Summary/overview requests select representative evidence throughout the document and account for long PDFs. Coverage is not limited to a single semantic neighborhood.
- **FR-034 (P0):** Ordinary questions return the most relevant evidence, with a conservative fallback when evidence is weak. The model must be instructed to state when information is absent or unclear.
- **FR-035 (P0):** Each cited ID maps to a retrieved source and exact chunk. Remove invalid or invented citation IDs, and never display a citation whose source cannot be loaded.
- **FR-036 (P0):** Source inspection displays filename, PDF page number, and exact passage with accessible dialog controls.
- **FR-037 (P0):** Greetings/small talk may be handled without retrieval, but must not imply document facts or attach irrelevant sources.
- **FR-038 (P0):** Treat extracted PDF text as untrusted content; it cannot override system instructions, trigger external actions, or disclose secrets.
- **FR-039 (P1):** Provide response-length/style choices (for example, concise, detailed, bullet summary) while retaining evidence and citation rules.
- **FR-040 (P1):** Track answer generation, retrieval, and provider errors separately so UI messages indicate whether retrying is safe.

### 9.4 Conversations and accounts

- **FR-050 (P0):** Support guest sessions and registered local accounts. Enforce document ownership on status, retrieval, source, and chat operations.
- **FR-051 (P0):** Guests are told chats are not saved after closing the page; signed-in chats persist and can be restored.
- **FR-052 (P0):** Account password hashes use a modern password-hashing function; session tokens are random, stored hashed, and issued only in HttpOnly cookies.
- **FR-053 (P0):** Rate-limit authentication attempts and avoid account enumeration through distinguishable login responses.
- **FR-054 (P1):** Add password reset/account recovery and email verification only if a trusted delivery service and user-facing account policy are selected.
- **FR-055 (P1):** Provide chat/document deletion and clear retention behavior.
- **FR-056 (P0):** New local accounts require a 12–128 character password with a letter, number, and non-space special character. Existing accounts may continue signing in with their previously accepted passwords.

**Two-factor authentication is not currently specified or implemented.** Select the supported factor, enrollment and recovery policy, and operational requirements before adding a second-factor challenge to account sign-in.

### 9.5 Frontend experience and accessibility

- **FR-060 (P0):** Preserve a responsive workspace with navigation/history, selected document context, backend status, chat thread, composer, upload dropzone, and source viewer.
- **FR-061 (P0):** Announce status changes accessibly; support keyboard upload, navigation, source dialogs, send controls, and focus return after dialogs.
- **FR-062 (P0):** Disable the composer while indexing or while a request is being submitted; make enabled/disabled state and error state visually clear.
- **FR-063 (P0):** Keep progress visible across the complete viewport, with stage labels and actionable failure messaging.
- **FR-064 (P0):** Maintain light/dark theme support and respect system preference on first visit.
- **FR-065 (P1):** Make error messages actionable, preserve the user's unsent question on recoverable request failure, and provide retry without duplicate messages.
- **FR-066 (P1):** Avoid implying upload progress is indexing progress; label transfer and processing separately.

## 10. Quality attributes and measurable targets

These are **proposed target-device baselines**, not measured current performance. Test them on a documented reference machine and revise after baseline profiling.

### 10.1 Performance

- **NFR-PERF-01:** Once indexing is ready and dependencies are warm, retrieval without LLM generation should complete within 2 seconds at p95 for a 500-page PDF on the reference local machine.
- **NFR-PERF-02:** Warm end-to-end answer latency should be measured and reported by stage. Initial target: p95 <= 15 seconds, excluding provider outages and network latency; revise by provider/model choice.
- **NFR-PERF-03:** Repeated questions must not invoke PDF extraction or re-embed document chunks. Instrument extraction, embedding, retrieval, and LLM durations separately.
- **NFR-PERF-04:** The UI must receive a status update at least every 2 seconds during active processing when progress is available; use bounded polling/backoff when no progress changes.
- **NFR-PERF-05:** No fixed indexing-time target until representative PDF sizes, scan ratios, hardware, and provider configuration are benchmarked. Report actual duration and cache-hit behavior.

### 10.2 Reliability and correctness

- **NFR-REL-01:** No document reaches `indexed` unless the complete readiness criteria in FR-014 pass.
- **NFR-REL-02:** All background job terminal states (`indexed`, `failed`, `cancelled`) are persisted; no silent failure or indefinite spinner.
- **NFR-REL-03:** Upload retries and identical-file uploads are idempotent for both files and vector records.
- **NFR-REL-04:** Citations must resolve to the same document, page, and passage used to generate the answer.
- **NFR-REL-05:** On restart, interrupted jobs are either safely resumed/retried or marked failed with recovery instructions; they must not remain falsely ready or indefinitely processing.

### 10.3 Privacy and security

- **NFR-SEC-01:** Keep API bearer credentials on the frontend server/backend boundary; never ship them to browser JavaScript or local storage.
- **NFR-SEC-02:** Bind every document and chat operation to authenticated session ownership; unauthorized access reveals no document existence (404 behavior where appropriate).
- **NFR-SEC-03:** Bind local services to loopback by default. Production/external exposure requires token protection, HTTPS, secure cookies, origin checks, request caps, and deployment hardening.
- **NFR-SEC-04:** Do not log full document content, passwords, session tokens, provider keys, or sensitive user prompts by default.
- **NFR-SEC-05:** Explain that when a remote LLM/embedding provider is configured, retrieved content or document-derived text is transmitted to that provider; list the selected provider and settings clearly.
- **NFR-SEC-06:** Protect against PDF parser/resource abuse, oversized request bodies, path traversal, hostile filenames, prompt injection, and cross-origin requests.

### 10.4 Compatibility and maintainability

- **NFR-COMP-01:** Publish and verify a Windows installation path, including Python DLL/policy troubleshooting; avoid presenting CPU mode as a workaround when the entire PyTorch import is blocked.
- **NFR-COMP-02:** Pin or constrain dependencies and document reproducible install steps. Validate the exact installed OS/Python/PyTorch/Sentence Transformers/ChromaDB combination.
- **NFR-COMP-03:** Keep provider implementations behind stable service interfaces and preserve embedding dimension/model compatibility with persisted vectors.
- **NFR-MAINT-01:** A single-process deployment is the supported baseline until locks, queue, and capacity controls are made shared across workers.
- **NFR-MAINT-02:** API schemas and status/error enums are versioned/documented; frontend and backend changes remain compatible during upgrades.

### 10.5 Accessibility and usability

- **NFR-UX-01:** Meet WCAG 2.1 AA for the primary upload, chat, progress, and source-review flows as a target; validate contrast, focus, semantics, and screen-reader announcements.
- **NFR-UX-02:** Layout remains usable at mobile widths and 200% zoom without hiding status, citations, or send controls.
- **NFR-UX-03:** Clear user-facing labels distinguish accepted, uploading, indexing, ready, failed, and asking states.

## 11. Data and API contract expectations

### Main entities

- **User/session:** user identity or guest session, expiry, owner scope.
- **Document:** SHA-256 document ID, original safe filename, bytes, page count, extraction/OCR metrics, owner grants, lifecycle state, timestamps.
- **Index/job:** stage, progress, retry count, timings, failure code, embedding model/version/dimension, vector collection reference.
- **Chunk/source:** stable chunk ID, page number, heading/context, exact extracted text, embedding reference.
- **Chat/message:** chat owner, selected document, user query, answer, citations, timestamps.

### Current API surface (observed/documented)

| Area | Endpoint | Purpose |
|---|---|---|
| Health | `GET /` | Basic service health |
| Session | `/auth/*` | Guest session, signup, login, logout, current session |
| Chats | `/chats` and `/chats/{chat_id}` | List, save/update, delete saved chats |
| Upload | `POST /documents/upload` | Validate, store and enqueue PDF indexing; `202` means accepted |
| Status | `GET /documents/{document_id}/status` | Read processing stage, progress, metrics, error and final status |
| Ask | `POST /documents/ask` | Retrieve evidence and produce grounded answer with citations |
| Search | `POST /documents/search` | Inspect ranked evidence without full answer generation |
| Source | `GET /documents/{document_id}/chunks/{chunk_id}` | Retrieve exact cited chunk |

### Target API behavior

- Standard response shapes with stable `code`, `message`, `retryable`, and relevant `stage` fields for user-actionable errors.
- Status state machine: `queued -> processing -> indexed | failed | cancelled`; `stage` identifies substep. Terminal states are immutable except through explicit retry creating a new attempt.
- Upload idempotency keyed by content hash plus owner grant; retries cannot duplicate chunks.
- Provider/model metadata retained with index so embedding model changes cannot silently mix incompatible vectors.
- Error bodies never contain raw stack traces or local absolute paths; detailed diagnostics remain in server logs with request/job correlation ID.

## 12. Index readiness and failure handling (critical contract)

### Readiness gate

The system may report **Ready** only when all are true:

1. PDF validated, stored, and accessible to the current owner.
2. Every eligible page extracted or OCR attempted according to configuration; OCR limitations recorded.
3. Usable text and chunks exist, with valid page metadata.
4. Embeddings were produced for every chunk using one compatible model/dimension.
5. Vector records were committed and validated.
6. The retrieval cache/warm-up step succeeded, and a smoke retrieval can query the document.
7. Status is durably persisted as indexed.

### Failure UX mapping

| Failure | UI should say | Required action |
|---|---|---|
| Embedding runtime/model cannot load (including blocked `torch_python.dll`) | “Document could not be indexed because the embedding runtime could not start.” | Offer setup guide/retry after fix; name stage and safe error code. |
| Tesseract or language pack missing | “Scanned pages could not be read because OCR is unavailable.” | Explain install/configure OCR; identify affected pages if possible. |
| PDF has no extractable content | “No readable text was found in this PDF.” | Suggest checking scan quality/OCR language. |
| Vector store unavailable | “The document index could not be saved.” | Retry storage/index operation safely. |
| LLM provider unavailable (after index is ready) | “The document is ready, but answer generation is unavailable.” | Preserve document state and query; allow retry. |
| Indexing still underway | “Preparing your document: [stage].” | Continue polling; keep ask disabled. |

## 13. Success metrics

Establish baseline instrumentation before claiming improvement. Do not log raw PDF text for analytics.

### Product metrics

- Upload-to-ready completion rate, segmented by OS and failure stage.
- Median and p95 time-to-ready by PDF size/page count and OCR percentage.
- Percentage of users asking at least one question after successful indexing.
- User-reported answer usefulness and source trust (lightweight opt-in feedback).
- Citation click-through and source passage load success.
- Repeat-question latency and cache hit rate.
- Retry success rate after a failed indexing attempt.

### Quality metrics

- Page-question citation precision: citations are on requested page(s).
- Summary coverage: cited sources span meaningful parts of the document for long PDFs.
- Citation entailment: answer claims are supported by cited passages, reviewed through a labeled evaluation set.
- Abstention quality: system correctly states absence/uncertainty rather than fabricating.
- Index completeness: indexed chunk count matches produced valid chunks; no unexplained page loss.
- Security checks: cross-session document access is denied, prompts cannot override system rules, secrets absent from client bundle/logs.

### Initial release gates (proposed)

- 100% of accepted indexing jobs end in a persisted terminal state under controlled failure injection.
- 100% of page-specific evaluation questions cite only the requested page, unless that page has no extractable content and the response says so.
- 0 false `indexed` states in integration/restart tests.
- >= 95% citation passage retrieval success for generated citations in the evaluation corpus.
- Measured, published warm retrieval and answer p50/p95 on the reference machine before performance claims.

## 14. Risks, dependencies, and mitigations

| Risk/dependency | Impact | Mitigation/product requirement |
|---|---|---|
| Windows Application Control blocks PyTorch DLL | Every first-time index fails after upload | Preflight embedding runtime, expose stage and safe error; document allowlisting and provide pluggable approved embedding backend. |
| Native dependencies vary by Python/OS | Setup failures and inconsistent behavior | Lock supported versions; smoke-test imports; publish per-OS setup instructions. |
| Tesseract not installed or language data absent | Scanned pages appear empty or incomplete | Diagnose OCR readiness, report affected pages, guide setup, distinguish scanned vs text PDFs. |
| Remote LLM/embedding sends document-derived content outside device | Privacy/compliance concern | Explicit provider disclosure, local option where practical, no silent provider fallback. |
| Poor PDF structure/columns/tables | Wrong reading order and weak answers | Preserve block/page metadata, create document evaluation corpus with complex layouts, report extraction limitations. |
| Embedding model or dimension changed while old Chroma data persists | Query/index incompatibility | Store model/version/dimension per index; reject mismatch and require controlled reindex. |
| Long OCR/indexing blocks UI or uses excessive resources | Apparent hang/resource exhaustion | Durable background jobs, limits, progress, cancellation, bounded concurrency. |
| In-process lock/queue with multiple workers | Duplicate/racing indexing and incorrect state | Officially support one worker; later move jobs/locks to shared queue/database before horizontal scaling. |
| Guest history is ephemeral | User loses conversation | Clear guest warning; allow sign-in migration and export/retention policy as follow-up. |
| No document deletion API | Storage grows and privacy control limited | P1 deletion lifecycle with cleanup and references. |
| LLM provider latency/outage | Slow/failed answers despite ready index | Separate readiness from answer provider; stage timing, retry and provider errors surfaced. |

## 15. Release plan

### Phase 0 — Reliability baseline (P0)

- Build reproducible setup and startup diagnostics for PDF extraction, OCR, embedding runtime/model, vector DB, and LLM configuration.
- Fix end-to-end failure visibility for background indexing and ensure the UI receives the actual terminal state.
- Validate `torch` import in the exact Windows virtual environment and document policy remediation; confirm supported alternative runtime/provider path before release.
- Add end-to-end acceptance flows for success, upload rejection, embedding failure, OCR failure, vector failure, restart, retry, page question, overview summary, and citation inspection.

### Phase 1 — Trustworthy local document chat (P0)

- Deliver validated upload, complete indexing gate, progress, status recovery, hybrid/page-aware retrieval, relevant summaries, cited answers, ownership security, guest/account behavior, accessible responsive UI.
- Instrument stage timings and establish quality/performance baselines on representative PDFs.

### Phase 2 — Manageability and answer controls (P1)

- Document deletion and index cleanup, cancellation/resume, password recovery if account usage requires it, richer answer controls, and quality feedback.
- Improve long-document summary coverage and complex layout/table handling based on evaluation results.

### Phase 3 — Broader workflows (P2; discovery required)

- Evaluate multi-document collections, additional file formats, team sharing, hosted deployment, multilingual support, and enterprise controls only after user demand and privacy requirements are understood.

## 16. Acceptance criteria for the primary release

The release is acceptable when all of the following are demonstrated on supported environments:

1. A valid text PDF completes every backend stage and the UI enables asking only after the status endpoint reports indexed.
2. A scanned PDF uses OCR when configured and reports OCR use or a clear setup failure.
3. A PDF rejected by type, signature, size, page limit, or resource limit gets an understandable error without creating a ready document.
4. An embedding runtime failure such as blocked PyTorch DLL moves the job to failed, includes a stable error code/stage and setup guidance, and never leaves a spinner or false ready state.
5. `/ask` and search return a clear not-ready response while indexing is underway.
6. An exact-page query is grounded only in the requested page, with matching page citation.
7. A long-document summary reflects the beginning, middle, and end where relevant and cites evidence across the document.
8. An unsupported question is answered with an explicit uncertainty/absence statement rather than an invented document fact.
9. Every displayed citation opens the exact source passage with correct filename and PDF page number.
10. Repeated questions do not re-extract or re-embed the document; warm retrieval and end-to-end timings are recorded.
11. Re-uploading identical content does not duplicate vectors and does not grant access to a different owner without an explicit owner grant.
12. Guest/account separation is verified for status, ask, search, source, and chat history.
13. Keyboard and screen-reader users can upload, monitor progress, send questions, and inspect/close citations.
14. Browser assets contain no backend bearer token, and error responses contain no traceback, secret, or local absolute path.
15. The supported single-worker deployment model and all required external/local services are documented.

## 17. Open product decisions

These do not block describing the current product, but should be decided before a broader public release:

1. Is the product strictly local/offline, or may document excerpts be sent to a remote LLM/embedding provider?
2. Confirm the final public-facing brand usage and any trademark clearance before commercial release.
3. What exact operating systems and machine profile are first-class support targets?
4. Is PyTorch local inference required, or is a remote/configurable embedding provider acceptable on restricted Windows machines?
5. Which LLM provider/model is the supported default, and what are its privacy, cost, rate, and latency constraints?
6. What are the expected PDF volume, max file size/page count, languages, scan rate, and concurrent users?
7. Are guest documents/chats deleted on session expiry, retained locally, or explicitly kept until user deletion?
8. Is document deletion, export, or account recovery required for initial release?
9. Which reference PDF set and human-reviewed answer/citation rubric will define “relevant summary” and answer quality?
10. What latency targets are required on the owner's real machine after measuring the current model/provider?

## 18. Source references inspected

- `work/backend/backend/README.md` — architecture, endpoints, defaults, security, storage, and known limits.
- `work/backend/backend/app/config.py` — embedding, vector-store, and retrieval configuration.
- `work/backend/backend/app/routes/documents.py` and `retrieval.py` — upload/status/ask/search request behavior and readiness gates.
- `work/backend/backend/app/routes/auth.py`, `services/auth_service.py`, and `services/document_registry.py` — sessions, accounts, persistence, and owner access.
- `work/backend/backend/app/services/text_extractor.py`, `text_processor.py`, and `text_chunker.py` — PDF/OCR and indexing input preparation.
- `work/backend/backend/app/services/embedding.py`, `vector.py`, `retrieval.py`, `rag_service.py`, and `prompt_builder.py` — embedding, vector storage, retrieval, citations, and response generation.
- `work/frontend/frontend/README.md`, `public/app.js`, `public/index.html`, and `server.mjs` — browser journeys, progress, accessibility, and local API proxy.
- Owner-provided server log — Windows `WinError 4551` blocking `torch_python.dll` during the background embedding stage.

