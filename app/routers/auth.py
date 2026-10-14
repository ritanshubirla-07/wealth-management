import re
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import COOKIE_NAME, SESSION_SECONDS, admin_credentials_match, create_session_token, hash_password, verify_password
from app.database import get_db
from app.models import Account, Client

router = APIRouter(prefix="/auth", tags=["Authentication"])


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


def _set_session(response: Response, claims: dict) -> None:
    response.set_cookie(
        COOKIE_NAME,
        create_session_token(claims),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=os.getenv("COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes"},
        samesite="lax",
        path="/",
    )


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(data: SignupRequest, response: Response, db: Session = Depends(get_db)):
    email = data.email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    existing = db.execute(select(Client.id).where(func.lower(Client.email) == email)).first()
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    client = Client(name=data.name.strip(), email=email, password_hash=hash_password(data.password))
    db.add(client)
    db.commit()
    db.refresh(client)
    _set_session(response, {"role": "client", "client_id": client.id, "name": client.name})
    return {"role": "client", "client_id": client.id, "name": client.name, "has_uploads": False}


@router.post("/login")
def login(data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    username = data.username.strip()
    if admin_credentials_match(username, data.password):
        _set_session(response, {"role": "admin", "name": "Administrator"})
        return {"role": "admin", "name": "Administrator"}

    client = db.execute(
        select(Client).where(func.lower(Client.email) == username.lower())
    ).scalars().first()
    if not client or not verify_password(data.password, client.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")

    has_uploads = db.execute(select(Account.id).where(Account.client_id == client.id).limit(1)).first() is not None
    _set_session(response, {"role": "client", "client_id": client.id, "name": client.name})
    return {"role": "client", "client_id": client.id, "name": client.name, "has_uploads": has_uploads}


@router.get("/me")
def current_user(request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "session", None)
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in.")
    if user["role"] == "admin":
        return {"role": "admin", "name": user.get("name", "Administrator")}
    client = db.get(Client, user.get("client_id"))
    if not client:
        raise HTTPException(status_code=401, detail="Client account no longer exists.")
    has_uploads = db.execute(select(Account.id).where(Account.client_id == client.id).limit(1)).first() is not None
    return {"role": "client", "client_id": client.id, "name": client.name, "has_uploads": has_uploads}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "signed out"}