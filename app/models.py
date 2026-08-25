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
    account_type: Mapped[str] = mapped_column(String(50))  # 'demat', 'pms', 'mf'
    account_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    portfolio_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    dp_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    custodian: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    source_file: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    statement_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    client: Mapped["Client"] = relationship("Client", back_populates="accounts")
    holdings: Mapped[List["Holding"]] = relationship(
        "Holding", back_populates="account", cascade="all,delete-orphan"
    )


class Holding(Base):
    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    security_name: Mapped[str] = mapped_column(String(300))
    isin: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    quantity: Mapped[Optional[float]] = mapped_column(nullable=True)
    avg_cost: Mapped[Optional[float]] = mapped_column(nullable=True)
    total_cost: Mapped[Optional[float]] = mapped_column(nullable=True)
    current_price: Mapped[Optional[float]] = mapped_column(nullable=True)
    current_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    accrued_income: Mapped[Optional[float]] = mapped_column(nullable=True)
    unrealized_gain: Mapped[Optional[float]] = mapped_column(nullable=True)
    gain_pct: Mapped[Optional[float]] = mapped_column(nullable=True)
    weight_pct: Mapped[Optional[float]] = mapped_column(nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    asset_class: Mapped[Optional[str]] = mapped_column(String(50), default="equity")
    market_cap: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    scrip_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    as_of_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    account: Mapped["Account"] = relationship("Account", back_populates="holdings")


class AnalysisCache(Base):
    """Stores pre-computed LLM analysis per client/account/section.
    account_id=NULL means family (cross-account) view."""
    __tablename__ = "analysis_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    section: Mapped[str] = mapped_column(String(50))  # overview, risk, performance, insights
    data_json: Mapped[str] = mapped_column(String(10000))  # JSON string
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
