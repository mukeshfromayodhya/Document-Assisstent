import hmac
import logging
import threading

from fastapi import Depends, FastAPI, Header, HTTPException
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.app.config import API_ACCESS_TOKEN, APP_ENV
from backend.app.routes import auth, documents, retrieval

logger = logging.getLogger(__name__)
if not API_ACCESS_TOKEN:
    logger.warning("API_ACCESS_TOKEN is unset; backend bearer-token protection is disabled. Keep this service on loopback.")


def _has_valid_bearer_token(authorization: str | None) -> bool:
    """Compare a bearer token without logging or exposing either value."""
    if not API_ACCESS_TOKEN:
        return True
    scheme, separator, supplied_token = (authorization or "").partition(" ")
    return bool(
        separator
        and scheme.lower() == "bearer"
        and hmac.compare_digest(
            supplied_token.strip().encode("utf-8"), API_ACCESS_TOKEN.encode("ascii")
        )
    )


def _require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Enforce bearer-token protection without logging credentials."""
    if not _has_valid_bearer_token(authorization):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API access token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class _APIGuardMiddleware:
    """Reject unauthenticated API requests before FastAPI parses their bodies.

    Bound request bodies as well: multipart uploads get the configured file
    limit plus a small envelope allowance, while JSON endpoints need only a
    small bounded request body. The limiter counts actual bytes, including
    chunked requests, rather than trusting Content-Length alone.
    """

    JSON_BODY_LIMIT = 1024 * 1024
    MULTIPART_OVERHEAD_LIMIT = 64 * 1024
    MAX_CONCURRENT_UPLOADS = 2

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._json_limited_app = RequestBodyLimitMiddleware(
            app, max_body_size=self.JSON_BODY_LIMIT
        )
        self._upload_limited_app = RequestBodyLimitMiddleware(
            app,
            max_body_size=documents.MAX_FILE_SIZE + self.MULTIPART_OVERHEAD_LIMIT,
        )
        self._upload_slots = threading.BoundedSemaphore(self.MAX_CONCURRENT_UPLOADS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_upload_request = path.rstrip("/") == "/documents/upload"
        if any(path == prefix or path.startswith(prefix + "/") for prefix in ("/documents", "/auth", "/chats")):
            values = [
                value.decode("latin-1")
                for name, value in scope.get("headers", [])
                if name.lower() == b"authorization"
            ]
            authorization = values[0] if len(values) == 1 else None
            if not _has_valid_bearer_token(authorization):
                response = JSONResponse(
                    {"detail": "Missing or invalid API access token."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return

        if is_upload_request:
            if not self._upload_slots.acquire(blocking=False):
                response = JSONResponse(
                    {"detail": "Document upload capacity is temporarily full."},
                    status_code=429,
                    headers={"Retry-After": "1"},
                )
                await response(scope, receive, send)
                return
            try:
                await self._upload_limited_app(scope, receive, send)
            finally:
                self._upload_slots.release()
            return

        await self._json_limited_app(scope, receive, send)

app = FastAPI(
    title="Document Assistant",
    description="API for intelligent document processing and retrieval.",
    version="1.0.0",
    docs_url="/docs" if APP_ENV == "development" else None,
    redoc_url="/redoc" if APP_ENV == "development" else None,
    openapi_url="/openapi.json" if APP_ENV == "development" else None,
)
app.include_router(
    documents.router,
    prefix="/documents",
    tags=["Documents"],
    dependencies=[Depends(_require_api_token)],
)
app.include_router(auth.router, prefix="/auth", tags=["Authentication"], dependencies=[Depends(_require_api_token)])
app.include_router(auth.chats_router, prefix="/chats", tags=["Chat history"], dependencies=[Depends(_require_api_token)])

app.add_middleware(_APIGuardMiddleware)

app.include_router(
    retrieval.router,
    prefix="/documents",
    tags=["Retrieval"],
    dependencies=[Depends(_require_api_token)],
)

@app.get("/")
def root():
    return {"message": "Document Assistant API is running"}
