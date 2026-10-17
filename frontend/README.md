# Document Assistant — Frontend

A dark, "deep research" interface for the Document Assistant backend. Plain HTML, CSS and browser JavaScript served by a small Node.js server. **No npm packages to install; Node.js 18 or newer is required.**

## Run

```powershell
cd <project-root>\frontend
Copy-Item .env.example .env     # once; set API_ACCESS_TOKEN only if the backend uses one
npm start
```

Open <http://127.0.0.1:4173>. Start the backend first (see the root [README](../README.md)). `npm run check` syntax-checks all JavaScript.

## What the UI does

- **Upload and indexing:** drag and drop a PDF (10 MB limit, checked in the browser and by the backend). A live card follows the backend stages (`queued`, `extracting`, `structuring`, `chunking`, `embedding`, `storing`, `optimizing`, `finalizing`) with animated steps, a page scan, chunk tags and an embedding wave. Questions unlock only when the status is `indexed`.
- **Chat with citations:** answers show `S1 · p.4` chips; selecting one opens the source tab on the right with the exact passage, and words from your question are highlighted. The tab stays closed until a citation is selected; close it with the ✕, the Source button or Esc. Drag its left edge (or use the arrow keys when the handle is focused; double-click resets) to make it narrower or wider; the width is remembered. "Open in a new tab" uses `source.html`.
- **Accounts and guests:** guest sessions start automatically. Sign in or create an account (12+ characters with a letter, number and special character, with a strength meter) to save chat history through `/api/chats`. Deleting a chat does not delete the PDF.
- **Responsive and accessible:** the sidebar and source panel become drawers on narrow screens; native `<dialog>`, labelled controls, visible focus, live regions, and `prefers-reduced-motion` support.

## Files

| File | Purpose |
|---|---|
| `server.mjs` | Static server plus allow-listed `/api/*` proxy; adds the bearer token server-side |
| `public/index.html`, `styles.css`, `app.js` | Main app |
| `public/source.html`, `source.js` | Standalone cited-passage viewer |
| `public/favicon.svg` | Icon |
| `.env.example` | `BACKEND_URL`, `API_ACCESS_TOKEN`, `FRONTEND_HOST`, `FRONTEND_PORT` |

## Security notes

- The proxy forwards only the routes in `docs/API.md`, rejects cross-origin mutating requests, enforces body-size limits (10 MB uploads, 1 MB JSON), and never sends the bearer token to the browser.
- A Content-Security-Policy blocks inline scripts and styles. The only external origins are Google Fonts (with system-font fallbacks, so the UI also works offline).
- Keep `FRONTEND_HOST=127.0.0.1` for local use. For any other deployment, add HTTPS, set `SESSION_COOKIE_SECURE=true` on the backend, and review the root README's security section.
- Guest chats live in browser memory only and are lost on reload.
