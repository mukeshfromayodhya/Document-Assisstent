# Document Assistant — Database and Storage

This page describes the current local persistence layout and schema as implemented by the backend. Data is stored on the machine running the backend. Treat PDFs, chats, account records, and vector files as private user data.

## Storage layout

| Path | Store | Contents |
|---|---|---|
| `storage/uploads/{sha256}.pdf` | Filesystem | Validated PDF bytes; the SHA-256 digest is the document ID |
| `storage/document_registry.sqlite3` | SQLite | Document state, metrics, filename aliases, and owner access grants |
| `storage/accounts.sqlite3` | SQLite | Accounts, session-token hashes, login-limit counters, and saved account chats |
| `storage/vector_db/` | ChromaDB persistent client | Chunk text, embeddings, and source metadata |
| Browser page memory | Browser | Guest conversation messages for the current page session |
| Browser local storage | Browser | Manual light/dark theme and legacy browser-chat migration input; not current account session tokens |

`VECTOR_DB_PATH` changes the Chroma directory. `AUTH_DB_PATH` changes the account SQLite path. Uploads and the document registry use the project-root `storage/` directory in the current implementation.

## Account database

The default account database is `storage/accounts.sqlite3`. Current tables:

| Table | Primary key | Purpose and notable fields |
|---|---|---|
| `accounts` | `id` | Normalized unique email, scrypt password hash, creation timestamp |
| `sessions` | `token_hash` | Account ID or guest ID, expiration timestamp; raw session tokens are not stored |
| `login_limits` | `bucket` | Login-failure count, time window, and lock-until value |
| `chats` | `id` | Account owner, title, JSON document metadata, JSON messages, created/updated timestamps |

`sessions.account_id` and `chats.account_id` reference `accounts.id` and cascade on account deletion at the database level. There is no public account-deletion endpoint. Chat payloads have validation and size limits in the service layer.

Guest chats are not written to this database. Signed-in chats are limited to the latest 60 records when listed. Session expiry is 24 hours for guests and 14 days for accounts.

## Document registry

The default registry is `storage/document_registry.sqlite3`. It is separate from Chroma so indexing state, upload identity, and access checks remain available without materializing vectors.

| Table | Primary key | Purpose |
|---|---|---|
| `documents` | `document_id` | Filename, size, page/chunk/OCR counts, timing JSON, indexing timestamps, cache count, status/stage/progress, safe error |
| `document_filenames` | `(document_id, filename)` | Original filename aliases observed for the content hash |
| `document_access` | `(document_id, owner_key)` | Access grant for `guest:<id>` or `account:<id>` |

`document_id` is a lowercase SHA-256 hash of the uploaded file bytes. Registry tables are linked by the document ID in application code; document rows do not declare foreign-key constraints to Chroma. Ownership is checked on status, source, search, and ask operations.

The registry initializes tables at service startup and contains additive compatibility logic for a few status columns. The project does not currently provide a separate versioned migration framework.

## Vector collection

Chroma uses a persistent collection named `documents` by default, configured with cosine distance. Each stored chunk has a collision-safe vector ID:

~~~text
<document_id>::<chunk_id>
~~~

The chunk ID is local to one document. The stored document text is the cleaned passage. Metadata includes `document_id`, `chunk_id`, `filename`, `page_number`, and `heading`; page/heading values are retained so a citation can resolve to the exact original passage.

The default embedding model creates 384-dimensional vectors. The existing collection's distance space and vector dimension are checked. Changing model or dimension without rebuilding the collection can make indexing fail; plan and run a deliberate re-index instead.

## Data lifecycle and deletion

Uploading identical bytes produces the same content hash, so the backend can reuse an existing vector index and refresh ownership metadata. A signed-in chat can store the document metadata and source citations for later display, but access is still governed by the registry's owner grants.

The current public API has no document-delete or account-delete endpoint. Deleting a chat removes its chat row but leaves the PDF, registry metadata, owner grants, and vectors in local storage. Do not assume removing one file is sufficient to erase all document data.

## Backup and restore

To preserve the application's local state, back up all of the following together:

- `storage/uploads/`
- `storage/document_registry.sqlite3` and any SQLite WAL/SHM files
- `storage/accounts.sqlite3` and any SQLite WAL/SHM files
- The configured Chroma directory, normally `storage/vector_db/`

Stop the backend before copying the complete storage set so SQLite and Chroma files are not changing during the copy. Protect backups as sensitive data. Restore the complete set to the same project storage locations (or update supported paths) before starting the backend.

Model caches are outside the project's storage tree and can be downloaded again if internet access is available; they are not a substitute for backing up uploaded documents and indices.
