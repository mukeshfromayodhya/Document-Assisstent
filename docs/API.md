# Document Assistant — API Reference

The authoritative route definitions are in `backend/app/routes/` and `backend/main.py`. This page documents the current HTTP contract; development OpenAPI is available at `http://127.0.0.1:8000/docs` only when `APP_ENV=development`.

## Base URL, authentication, and cookies

- Direct backend base URL: `http://127.0.0.1:8000`.
- Through the frontend proxy, prefix backend routes with `/api`, for example `/api/documents/ask`.
- If `API_ACCESS_TOKEN` is configured, every route under `/auth`, `/documents`, and `/chats` requires `Authorization: Bearer <token>`. The root health endpoint does not.
- Session-dependent routes also require the `adi_session` cookie. Start a guest session or sign in first. The cookie is HttpOnly and should be handled by the browser or a cookie jar, not read by client-side JavaScript.
- A bearer token is not a user session. Document ownership checks and account checks still apply.
- The frontend proxy accepts only its allowlisted methods and paths; see [frontend/README.md](../frontend/README.md).

## Endpoint summary

| Method | Backend route | Session requirement | Description |
|---|---|---|---|
| GET | `/` | None | Health response |
| GET | `/auth/session` | Optional | Current authentication state |
| POST | `/auth/guest` | Optional | Create or reuse a guest session |
| POST | `/auth/signup` | None; creates session | Create local account and sign in |
| POST | `/auth/login` | None; creates session | Sign in and rotate session |
| POST | `/auth/logout` | Optional | Invalidate current session and clear cookie |
| GET | `/chats` | Signed-in account | List the account's recent chats |
| PUT | `/chats/{chat_id}` | Signed-in account | Create or update one chat |
| DELETE | `/chats/{chat_id}` | Signed-in account | Delete a chat record |
| POST | `/documents/upload` | Guest or account | Accept PDF and queue indexing |
| GET | `/documents/{document_id}/status` | Guest or account with document access | Read indexing state and metrics |
| GET | `/documents/{document_id}/chunks/{chunk_id}` | Guest or account with document access | Fetch exact cited chunk |
| POST | `/documents/search` | Guest or account with document access | Retrieve ranked passages |
| POST | `/documents/ask` | Guest or account with document access | Generate a cited answer |

## Authentication and sessions

### `GET /auth/session`

Returns whether the current cookie belongs to an account:

~~~json
{"authenticated": false, "email": null}
~~~

This endpoint does not require an existing session cookie.

### `POST /auth/guest`

Creates a guest cookie if there is no valid session; an existing valid session is reused.

Response:

~~~json
{"authenticated": false, "email": null, "guest": true}
~~~

Guest sessions expire after 24 hours.

### `POST /auth/signup` and `POST /auth/login`

JSON request:

~~~json
{"email": "reader@example.test", "password": "Example-Password-42"}
~~~

Signup requires a valid email and a 12–128 character password containing a letter, a number, and a non-space special character. Passwords are stored as scrypt hashes. Sign-in continues to accept 5–128 characters so existing local accounts keep working. Success sets the account session cookie and returns:

~~~json
{"authenticated": true, "email": "reader@example.test"}
~~~

Account sessions expire after 14 days. Login rotates the session while preserving access to documents uploaded in the prior guest session. Signup can return `409` if the email is already registered; invalid credentials return `401`.

The current account flow does not include 2FA, password recovery, or email verification. The chosen factor and recovery policy must be defined before those routes are added.

### `POST /auth/logout`

Invalidates the current session when present and clears the session cookie:

~~~json
{"authenticated": false}
~~~

## Chat history

Chat routes require an authenticated account; guest sessions receive `401`.

### `GET /chats`

Returns up to 60 chats ordered by most recently updated:

~~~json
{"chats": []}
~~~

### `PUT /chats/{chat_id}`

Creates or updates a chat. The `chat.id` must equal the path parameter. The current UI sends a chat object with `id`, `title`, `createdAt`, `updatedAt`, `document`, and `messages`.

~~~json
{
  "chat": {
    "id": "local-chat-id",
    "title": "Research notes",
    "createdAt": 1760000000000,
    "updatedAt": 1760000001000,
    "document": null,
    "messages": []
  }
}
~~~

Success returns `{"id":"local-chat-id","saved":true}`. The backend validates chat/message sizes and verifies access before attaching document metadata.

### `DELETE /chats/{chat_id}`

Returns `{"deleted":true}`. Deleting chat history does not delete the PDF or its vector index.

## Document upload and status

### `POST /documents/upload`

Send `multipart/form-data` with a file field named `file`. The backend accepts PDF files up to 10 MB, validates the PDF signature, and applies a 500-page default limit during extraction. A successful response is HTTP `202 Accepted`:

~~~json
{
  "status": "processing",
  "document_id": "64-lowercase-hex-characters",
  "filename": "report.pdf",
  "content_type": "application/pdf",
  "size": 123456,
  "stage": "queued",
  "progress": 1
}
~~~

`202` means the backend accepted the background indexing job. It is not the ready state. Poll the status route. The current job runner uses FastAPI BackgroundTasks in the backend process; this API does not expose a job cancellation operation.

Possible upload errors include `400` for empty/invalid filename, extension, or signature; `401` for missing/invalid API token or session; `413` for the 10 MB limit; `422` for request/PDF validation; `429` when upload capacity is full; and `500` for an unexpected upload failure. Some page/extraction errors occur in the background and appear in status as `failed`, after the initial `202`.

### `GET /documents/{document_id}/status`

The document ID is the SHA-256 content hash. This endpoint checks session ownership and returns, among other fields:

~~~json
{
  "document_id": "64-lowercase-hex-characters",
  "filename": "report.pdf",
  "size": 123456,
  "page_count": 12,
  "chunk_count": 30,
  "ocr_page_count": 0,
  "timings_ms": {"total_ms": 8400},
  "status": "indexed",
  "stage": "ready",
  "progress": 100,
  "error": null
}
~~~

Status values are `indexing`, `indexed`, or `failed`. Progress is between 1 and 100. The response also includes indexing timestamps and cache-hit information. A question/search request before `indexed` returns `409`.

### `GET /documents/{document_id}/chunks/{chunk_id}`

Returns one exact source passage after checking document ownership:

~~~json
{
  "document_id": "64-lowercase-hex-characters",
  "chunk_id": 1,
  "filename": "report.pdf",
  "page_number": 4,
  "heading": "Findings",
  "text": "The exact indexed passage..."
}
~~~

## Search and answer requests

Both endpoints accept the same JSON shape:

~~~json
{
  "query": "What are the main findings?",
  "document_id": "64-lowercase-hex-characters",
  "top_k": 5,
  "min_score": 0.2
}
~~~

`query` is required and limited to 8,000 characters. `document_id` is required by these routes. `top_k` must be at least 1 and is capped at `RETRIEVAL_MAX_TOP_K` (50 by default). `min_score` is optional and must be between 0 and 1.

### `POST /documents/search`

Returns ranked passage objects with `chunk_id`, `document_id`, `filename`, `text`, `page_number`, `heading`, and `score`:

~~~json
{
  "query": "What are the main findings?",
  "top_k": 5,
  "document_id": "64-lowercase-hex-characters",
  "results": []
}
~~~

### `POST /documents/ask`

Returns the answer and source IDs:

~~~json
{
  "query": "What are the main findings?",
  "answer": "The report identifies ... [S1].",
  "small_talk": false,
  "sources": [
    {"citation": "[S1]", "chunk_id": "1", "page_number": 4}
  ]
}
~~~

Greetings may be handled without retrieval and return `small_talk: true` with an empty source list. If no relevant passages are found, the backend returns a no-results answer with no sources. A configured LLM provider failure returns `503`.

## Common status codes

| Code | Meaning |
|---|---|
| 200 | Request completed |
| 202 | Upload accepted for background indexing |
| 400 | Invalid request value or upload |
| 401 | Missing/invalid API token, missing/expired session, or account sign-in required |
| 403 | Session/account is not permitted to save the referenced chat/document |
| 404 | Document/chat/source is unavailable to this owner, or route is missing |
| 409 | Duplicate email or document indexing is incomplete |
| 413 | Request or file exceeds a configured limit |
| 422 | Request schema, query, credential, or PDF validation failed |
| 429 | Upload concurrency capacity or authentication limit was reached |
| 500 | Unexpected server-side failure |
| 503 | Index, retrieval, vector, or answer provider is unavailable |

Error responses use FastAPI's JSON `detail` field. Specific routes may also return `Retry-After`.

## Example request sequence

The following commands use cURL syntax. If a backend token is configured, add `-H "Authorization: Bearer $TOKEN"` to protected routes. Omit that header only for loopback development with the backend token unset.

~~~sh
API=http://127.0.0.1:8000

# Start a guest session and keep its cookie.
curl -c cookies.txt -X POST "$API/auth/guest"

# Upload and save the returned 64-character document_id.
curl -b cookies.txt -c cookies.txt -F "file=@report.pdf" "$API/documents/upload"

# Poll until status is indexed or failed.
curl -b cookies.txt "$API/documents/<document_id>/status"

# Ask only after the status is indexed.
curl -b cookies.txt -H "Content-Type: application/json" \
  -d '{"query":"Summarize the report","document_id":"<document_id>","top_k":5}' \
  "$API/documents/ask"
~~~

For public/non-development deployment, configure bearer authentication, HTTPS, secure cookies, and private backend networking before use. The current service is designed for a local single-machine workflow.
