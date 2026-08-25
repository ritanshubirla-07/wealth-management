from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models import Client, Account

router = APIRouter(prefix="/client", tags=["Client"])

class ClientCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    pan: Optional[str] = None

class AccountResponse(BaseModel):
    id: int
    account_type: Optional[str]
    account_number: Optional[str]
    portfolio_name: Optional[str]

    class Config:
        from_attributes = True

from datetime import datetime

class ClientResponse(BaseModel):
    id: int
    name: str
    phone: Optional[str]
    pan: Optional[str]
    created_at: Optional[datetime] = None
    accounts: List[AccountResponse] = []

    class Config:
        from_attributes = True

@router.post("/", response_model=ClientResponse, status_code=status.HTTP_201_CREATED)
def create_client(client_in: ClientCreate, db: Session = Depends(get_db)):
    db_client = Client(name=client_in.name, phone=client_in.phone, pan=client_in.pan)
    db.add(db_client)
    db.commit()
    db.refresh(db_client)
    return db_client

@router.get("/", response_model=List[ClientResponse])
def get_clients(db: Session = Depends(get_db)):
    """Get all clients."""
    clients = db.execute(select(Client)).scalars().all()
    return list(clients)

@router.get("/{client_id}", response_model=ClientResponse)
def get_client(client_id: int, db: Session = Depends(get_db)):
    """Get a single client by ID."""
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return client

@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_client(client_id: int, db: Session = Depends(get_db)):
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    db.delete(client)
    db.commit()
    return None
