"""Local account, guest session, and conversation storage."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, Request, Response

from backend.app.config import PROJECT_ROOT

SESSION_COOKIE = "adi_session"
SESSION_TTL = 60 * 60 * 24 * 14
GUEST_TTL = 60 * 60 * 24
PASSWORD_MIN = 12
PASSWORD_MAX = 128
_EMAIL = re.compile(r"^[^\s@]{1,64}@[^\s@.]+(?:\.[^\s@.]+)+$")
_AUTH_DB_SETTING = os.environ.get("AUTH_DB_PATH", "").strip()
_DB_PATH = Path(_AUTH_DB_SETTING or PROJECT_ROOT / "storage" / "accounts.sqlite3").expanduser().resolve()
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_INIT_LOCK = threading.Lock()


@dataclass(frozen=True)
class Session:
    guest_id: str
    account_id: int | None
    email: str | None

    @property
    def authenticated(self) -> bool:
        return self.account_id is not None

    @property
    def owner_keys(self) -> tuple[str, ...]:
        keys = [f"guest:{self.guest_id}"]
        if self.account_id is not None:
            keys.append(f"account:{self.account_id}")
        return tuple(keys)


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_DB_PATH, timeout=20.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=20000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _initialize() -> None:
    with _INIT_LOCK, _connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
                guest_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS login_limits (
                bucket TEXT PRIMARY KEY,
                started_at INTEGER NOT NULL,
                failures INTEGER NOT NULL,
                locked_until INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                document_json TEXT,
                messages_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chats_account_updated ON chats(account_id, updated_at DESC);
        """)


_initialize()


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return email


def _password_hash(password: str, salt: bytes | None = None) -> str:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise HTTPException(status_code=422, detail=f"Password must be {PASSWORD_MIN}–{PASSWORD_MAX} characters.")
    if not any(character.isalpha() for character in password):
        raise HTTPException(status_code=422, detail="Password must include at least one letter.")
    if not any(character.isdigit() for character in password):
        raise HTTPException(status_code=422, detail="Password must include at least one number.")
    if not any(not character.isalnum() and not character.isspace() for character in password):
        raise HTTPException(status_code=422, detail="Password must include at least one special character.")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**17, r=8, p=1, dklen=32, maxmem=256 * 1024 * 1024)
    return f"scrypt$131072$8$1${salt.hex()}${derived.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest_hex)), maxmem=256 * 1024 * 1024)
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError, MemoryError):
        return False


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """Keep unknown-account login checks close to the real scrypt cost."""
    return _password_hash("dummy-password-for-timing-equalization-1")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_session(db: sqlite3.Connection, account_id: int | None, guest_id: str | None = None) -> tuple[str, Session]:
    token = secrets.token_urlsafe(32)
    guest_id = guest_id or secrets.token_urlsafe(24)
    ttl = SESSION_TTL if account_id is not None else GUEST_TTL
    expires = int(time.time()) + ttl
    db.execute("INSERT INTO sessions(token_hash, account_id, guest_id, expires_at) VALUES(?, ?, ?, ?)", (_token_hash(token), account_id, guest_id, expires))
    email = None
    if account_id is not None:
        row = db.execute("SELECT email FROM accounts WHERE id=?", (account_id,)).fetchone()
        email = row["email"] if row else None
    return token, Session(guest_id, account_id, email)


def set_session_cookie(response: Response, token: str, authenticated: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL if authenticated else GUEST_TTL,
        httponly=True, secure=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true",
        samesite="strict", path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true", samesite="strict", path="/")


def get_session(request: Request, required: bool = True) -> Session | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        if required:
            raise HTTPException(status_code=401, detail="Start a guest session or sign in.")
        return None
    with _connect() as db:
        row = db.execute("SELECT s.guest_id, s.account_id, s.expires_at, a.email FROM sessions s LEFT JOIN accounts a ON a.id=s.account_id WHERE s.token_hash=?", (_token_hash(token),)).fetchone()
        if row is None or row["expires_at"] < int(time.time()):
            db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
            if required:
                raise HTTPException(status_code=401, detail="Session expired. Start a new guest session or sign in.")
            return None
        return Session(row["guest_id"], row["account_id"], row["email"])


def create_guest(response: Response, request: Request) -> Session:
    existing = get_session(request, required=False)
    if existing is not None:
        return existing
    with _connect() as db:
        token, session = _issue_session(db, None)
    set_session_cookie(response, token, False)
    return session


def signup(email_value: str, password: str, request: Request, response: Response) -> Session:
    email = normalize_email(email_value)
    password_hash = _password_hash(password)
    with _connect() as db:
        try:
            cursor = db.execute("INSERT INTO accounts(email, password_hash, created_at) VALUES(?, ?, ?)", (email, password_hash, int(time.time())))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="An account with this email already exists.") from exc
        previous = get_session(request, required=False)
        if request.cookies.get(SESSION_COOKIE):
            db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(request.cookies[SESSION_COOKIE]),))
        token, session = _issue_session(db, int(cursor.lastrowid), previous.guest_id if previous else None)
    set_session_cookie(response, token, True)
    return session


def _login_bucket(request: Request, email: str) -> str:
    forwarded = request.client.host if request.client else "unknown"
    value = f"{forwarded}|{email}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def login(email_value: str, password: str, request: Request, response: Response) -> Session:
    email = normalize_email(email_value)
    bucket = _login_bucket(request, email)
    now = int(time.time())
    invalid_credentials = False
    with _connect() as db:
        limit = db.execute("SELECT * FROM login_limits WHERE bucket=?", (bucket,)).fetchone()
        if limit and limit["locked_until"] > now:
            raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again in 15 minutes.", headers={"Retry-After": str(limit["locked_until"] - now)})
        row = db.execute("SELECT id, password_hash FROM accounts WHERE email=?", (email,)).fetchone()
        stored_hash = row["password_hash"] if row else _dummy_password_hash()
        password_matches = _verify_password(password, stored_hash)
        valid = bool(row and password_matches)
        if not valid:
            started = limit["started_at"] if limit and now - limit["started_at"] < 900 else now
            failures = (limit["failures"] if limit and started == limit["started_at"] else 0) + 1
            locked_until = now + 900 if failures >= 5 else 0
            db.execute("INSERT INTO login_limits(bucket, started_at, failures, locked_until) VALUES(?, ?, ?, ?) ON CONFLICT(bucket) DO UPDATE SET started_at=excluded.started_at, failures=excluded.failures, locked_until=excluded.locked_until", (bucket, started, failures, locked_until))
            invalid_credentials = True
        else:
            db.execute("DELETE FROM login_limits WHERE bucket=?", (bucket,))
            previous = get_session(request, required=False)
            if request.cookies.get(SESSION_COOKIE):
                db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(request.cookies[SESSION_COOKIE]),))
            token, session = _issue_session(db, int(row["id"]), previous.guest_id if previous else None)
    if invalid_credentials:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    set_session_cookie(response, token, True)
    return session


def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with _connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
    clear_session_cookie(response)


def list_chats(account_id: int) -> list[dict]:
    import json
    with _connect() as db:
        rows = db.execute("SELECT * FROM chats WHERE account_id=? ORDER BY updated_at DESC LIMIT 60", (account_id,)).fetchall()
    chats = []
    for row in rows:
        try:
            document = json.loads(row["document_json"]) if row["document_json"] else None
            messages = json.loads(row["messages_json"])
        except (TypeError, ValueError):
            continue
        if document and isinstance(document, dict):
            from backend.app.services.document_registry import get_document_registry
            if not get_document_registry().has_access(document.get("document_id", ""), (f"account:{account_id}",)):
                document = None
                messages = [
                    {**message, "sources": []} if isinstance(message, dict) else message
                    for message in messages
                ] if isinstance(messages, list) else []
        chats.append({"id": row["id"], "title": row["title"], "document": document, "messages": messages, "createdAt": row["created_at"], "updatedAt": row["updated_at"]})
    return chats


def save_chat(account_id: int, value: dict, owner_keys: tuple[str, ...] = ()) -> dict:
    import json
    chat_id = value.get("id")
    title = value.get("title")
    messages = value.get("messages")
    document = value.get("document")
    if not isinstance(chat_id, str) or not 1 <= len(chat_id) <= 120 or not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        raise HTTPException(status_code=422, detail="Chat ID or title is invalid.")
    if not isinstance(messages, list) or len(messages) > 300:
        raise HTTPException(status_code=422, detail="A chat can contain at most 300 messages.")
    if document is not None and (not isinstance(document, dict) or not isinstance(document.get("document_id"), str)):
        raise HTTPException(status_code=422, detail="Chat document metadata is invalid.")
    if document is not None:
        from backend.app.services.document_registry import get_document_registry
        registry = get_document_registry()
        if not registry.has_access(document["document_id"], (*owner_keys, f"account:{account_id}")):
            raise HTTPException(status_code=403, detail="This account does not have access to the document in this chat.")
        registry.grant_access(document["document_id"], (f"account:{account_id}",))
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"} or not isinstance(message.get("content"), str) or len(message["content"]) > 100_000:
            raise HTTPException(status_code=422, detail="Chat message is invalid or too large.")
    now = int(time.time() * 1000)
    created_at = value.get("createdAt")
    created_at = created_at if isinstance(created_at, int) and not isinstance(created_at, bool) else now
    document_json = json.dumps(document, ensure_ascii=False, separators=(",", ":")) if document else None
    messages_json = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    if len(messages_json) > 500_000:
        raise HTTPException(status_code=413, detail="This conversation is too large to save.")
    with _connect() as db:
        owner = db.execute("SELECT account_id FROM chats WHERE id=?", (chat_id,)).fetchone()
        if owner and owner["account_id"] != account_id:
            raise HTTPException(status_code=404, detail="Chat not found.")
        db.execute("INSERT INTO chats(id, account_id, title, document_json, messages_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, document_json=excluded.document_json, messages_json=excluded.messages_json, updated_at=excluded.updated_at", (chat_id, account_id, title.strip(), document_json, messages_json, created_at, now))
    return {"id": chat_id, "saved": True}


def delete_chat(account_id: int, chat_id: str) -> None:
    with _connect() as db:
        db.execute("DELETE FROM chats WHERE id=? AND account_id=?", (chat_id, account_id))

