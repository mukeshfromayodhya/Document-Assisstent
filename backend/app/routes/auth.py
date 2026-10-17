"""Account and session endpoints for the local browser client."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.app.services import auth_service
from backend.app.services.auth_service import Session

router = APIRouter()
chats_router = APIRouter()


class SignupCredentials(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=12, max_length=128)


class LoginCredentials(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    # Keep legacy accounts with shorter passwords able to sign in.
    password: str = Field(..., min_length=5, max_length=128)


class ChatPayload(BaseModel):
    chat: dict[str, Any]


def require_session(request: Request) -> Session:
    return auth_service.get_session(request, required=True)


def require_account(session: Session = Depends(require_session)) -> Session:
    if not session.authenticated or session.account_id is None:
        raise HTTPException(status_code=401, detail="Sign in to use saved chat history.")
    return session


@router.get("/session")
def session_status(request: Request):
    session = auth_service.get_session(request, required=False)
    if session is None:
        return {"authenticated": False, "email": None}
    return {"authenticated": session.authenticated, "email": session.email}


@router.post("/guest")
def start_guest(request: Request, response: Response):
    session = auth_service.create_guest(response, request)
    return {"authenticated": session.authenticated, "email": None, "guest": True}


@router.post("/signup")
def create_account(payload: SignupCredentials, request: Request, response: Response):
    session = auth_service.signup(payload.email, payload.password, request, response)
    return {"authenticated": True, "email": session.email}


@router.post("/login")
def sign_in(payload: LoginCredentials, request: Request, response: Response):
    session = auth_service.login(payload.email, payload.password, request, response)
    return {"authenticated": True, "email": session.email}


@router.post("/logout")
def sign_out(request: Request, response: Response):
    auth_service.logout(request, response)
    return {"authenticated": False}


@chats_router.get("")
def get_chats(session: Session = Depends(require_account)):
    return {"chats": auth_service.list_chats(session.account_id)}


@chats_router.put("/{chat_id}")
def put_chat(chat_id: str, payload: ChatPayload, session: Session = Depends(require_account)):
    value = payload.chat
    if value.get("id") != chat_id:
        raise HTTPException(status_code=422, detail="Chat ID does not match the request path.")
    return auth_service.save_chat(session.account_id, value, session.owner_keys)


@chats_router.delete("/{chat_id}")
def remove_chat(chat_id: str, session: Session = Depends(require_account)):
    auth_service.delete_chat(session.account_id, chat_id)
    return {"deleted": True}
