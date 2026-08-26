from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    pan: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    accounts: Mapped[List["Account"]] = relationship(
        "Account", back_populates="client", cascade="all,delete-orphan"
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    account_type: Mapped[str] = mapped_column(String(50))
    account_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    portfolio_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    source_file: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    statement_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    has_cost_data: Mapped[bool] = mapped_column(default=False)
    raw_markdown: Mapped[Optional[str]] = mapped_column(nullable=True)
    raw_json: Mapped[Optional[str]] = mapped_column(nullable=True)
    overview_json: Mapped[Optional[str]] = mapped_column(nullable=True)
    performance_json: Mapped[Optional[str]] = mapped_column(nullable=True)
    insights_json: Mapped[Optional[str]] = mapped_column(nullable=True)
    risk_analysis_json: Mapped[Optional[str]] = mapped_column(nullable=True)

    client: Mapped["Client"] = relationship("Client", back_populates="accounts")
    holdings: Mapped[List["Holding"]] = relationship(
        "Holding", back_populates="account", cascade="all,delete-orphan"
    )


class Holding(Base):
    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    security_name: Mapped[str] = mapped_column(String(300))
    current_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    total_cost: Mapped[Optional[float]] = mapped_column(nullable=True)
    gain_pct: Mapped[Optional[float]] = mapped_column(nullable=True)
    weight_pct: Mapped[Optional[float]] = mapped_column(nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    account: Mapped["Account"] = relationship("Account", back_populates="holdings")
