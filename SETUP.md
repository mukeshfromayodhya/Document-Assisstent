# Document Assistant — Setup Guide

This guide recreates the local development setup from the project files. It assumes the backend and frontend archives have been extracted as sibling folders at the project root. See the [root README](README.md) for the overview and [backend README](backend/README.md) for the complete backend configuration reference.

## Requirements

- Windows, macOS, or Linux on a supported Python/PyTorch platform.
- Python 3.10 or newer.
- Node.js 18 or newer.
- Internet access on first use if the embedding model is not already cached, and access to the configured LLM provider when asking questions.
- Tesseract OCR only if the PDFs to be processed include scanned/image-only pages. It is a system application and is not installed by requirements.txt.

The Python dependency file uses unpinned package names. It is suitable for recreating a development environment but does not promise byte-for-byte reproducible package resolution across operating systems.

## 1. Assemble the project

Extract backend.zip and frontend.zip into the same project root. The result should include:

~~~text
Document Assistant/
├── backend/
├── frontend/
├── docs/
├── .env.example
├── PRD.md
├── README.md
└── requirements.txt
~~~

Do not extract the backend archive inside an extra nested backend directory. Create a local Python environment and runtime storage at this root; neither is included in the archives.

## 2. Create the Python environment

From the project root in PowerShell:

~~~powershell
python --version
python -m venv myenv
.\myenv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
~~~

On macOS/Linux, activate with source myenv/bin/activate. If PowerShell activation is blocked by local policy, use .\myenv\Scripts\python.exe -m pip ... instead of changing system policy.

The backend uses PyTorch for local embeddings. An organization-controlled Windows Application Control policy can block native PyTorch DLLs. Choosing CPU mode or reinstalling PyTorch does not override such a policy; use an approved runtime and follow the administrator's allowlisting process.

## 3. Configure the backend

Copy the safe template to the project-root environment file:

~~~powershell
Copy-Item .env.example .env
~~~

For local development, keep APP_ENV=development. Set LLM_MODEL and LLM_API_KEY for a compatible provider before asking questions. The embedding model and chat model are separate: the embedding model is downloaded/loaded locally; the chat-completions model is selected by the LLM settings.

The development default permits an empty API access token. This is only for loopback development. For a configured token:

1. Set API_ACCESS_TOKEN to a strong ASCII value with at least 32 characters and no whitespace.
2. Copy that exact value to frontend/.env.
3. Keep the API token out of source files and public browser code.

The backend reads .env from the project root. frontend/.env is a separate file for the Node proxy.

## 4. Install Tesseract only when needed

Selectable-text PDFs do not need Tesseract. Install it once on the machine only when processing scanned/image-only pages:

- **Windows:** install a trusted Tesseract build, add its executable to PATH, reopen the terminal, and verify with tesseract --version.
- **macOS:** brew install tesseract.
- **Ubuntu/Debian:** sudo apt install tesseract-ocr.

The default OCR language is English (eng). Install the relevant language data and set OCR_LANGUAGE for other languages, for example eng+hin. Full OCR limits and OS notes are in [backend/README.md](backend/README.md).

## 5. Start the backend

From the project root, activate myenv, then run:

~~~powershell
$env:APP_ENV = "development"
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
~~~

The backend health response is at http://127.0.0.1:8000/. In development, interactive API docs are at http://127.0.0.1:8000/docs. API docs are disabled outside development.

## 6. Configure and start the frontend

Open a second terminal:

~~~powershell
Set-Location frontend
Copy-Item .env.example .env
# If configured, set API_ACCESS_TOKEN to the same value as the backend.
npm start
~~~

Open http://127.0.0.1:4173. The current frontend has no npm dependencies, so an npm install step is not required.

## 7. First-run expectations

1. The interface should report that the backend is connected.
2. Upload a readable PDF smaller than 10 MB and within the configured page limit (500 pages by default).
3. Wait for the status to reach indexed. HTTP 202 means accepted for background processing, not ready to query.
4. Ask a document question and inspect a citation to confirm that its page and passage open.
5. Check provider configuration if answer generation returns a temporary service error.

The first indexing job may be slower because the local embedding model needs to initialize or download. Later uploads with identical file bytes can reuse the content-hash index. Later questions do not re-run document chunking or embedding, though they still perform retrieval and an LLM request.

For exact endpoint examples, see [API.md](docs/API.md). For documented test and manual smoke-check status, see [TESTING.md](TESTING.md).
