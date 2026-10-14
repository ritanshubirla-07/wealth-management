from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[Optional[str]] = mapped_column(String(254), nullable=True, unique=True, index=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    pan: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))

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


class UploadedDocument(Base):
    __tablename__ = "uploaded_documents"
    __table_args__ = (UniqueConstraint("client_id", "sha256", name="uq_uploaded_document_client_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    uploaded_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))


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
    # ISIN joins a holding to market data unambiguously; quantity is what makes
    # mark-to-market revaluation possible (quantity x live price).
    isin: Mapped[Optional[str]] = mapped_column(String(12), nullable=True, index=True)
    quantity: Mapped[Optional[float]] = mapped_column(nullable=True)
    # Exchange codes when the statement supplies them. A BSE code reaches
    # BSE-only listings that have no NSE symbol.
    nse_symbol: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    bse_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    account: Mapped["Account"] = relationship("Account", back_populates="holdings")


class MarketData(Base):
    """One row per security, ingested from a Screener.in CSV export.

    Keyed on nothing in particular by design: a given export may identify
    companies by ISIN, by NSE/BSE code, or by name alone, so all three are
    stored and matching falls back through them in that order of reliability.
    """

    __tablename__ = "market_data"

    id: Mapped[int] = mapped_column(primary_key=True)
    isin: Mapped[Optional[str]] = mapped_column(String(12), nullable=True, index=True)
    nse_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)
    bse_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    # Normalised company name used as the last-resort join key
    match_key: Mapped[Optional[str]] = mapped_column(String(300), nullable=True, index=True)

    industry: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    cmp: Mapped[Optional[float]] = mapped_column(nullable=True)
    # Where the price came from: "NSE close", "BSE close", "AMFI NAV"
    # or "screener.in". Official sources are preferred; Screener is the fallback.
    price_source: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    market_cap: Mapped[Optional[float]] = mapped_column(nullable=True)
    market_cap_tier: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    pe: Mapped[Optional[float]] = mapped_column(nullable=True)
    roe: Mapped[Optional[float]] = mapped_column(nullable=True)
    roce: Mapped[Optional[float]] = mapped_column(nullable=True)
    debt_to_equity: Mapped[Optional[float]] = mapped_column(nullable=True)
    dividend_yield: Mapped[Optional[float]] = mapped_column(nullable=True)
    book_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    eps: Mapped[Optional[float]] = mapped_column(nullable=True)
    face_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    sales_growth: Mapped[Optional[float]] = mapped_column(nullable=True)
    profit_growth: Mapped[Optional[float]] = mapped_column(nullable=True)

    # Any column of the export we did not recognise, kept verbatim
    extras_json: Mapped[Optional[str]] = mapped_column(nullable=True)
    # Full scraped company page: top ratios + P&L / balance sheet / cash flow /
    # ratios / shareholding / CAGR boxes, as returned by parse_financials()
    fundamentals_json: Mapped[Optional[str]] = mapped_column(nullable=True)
    fundamentals_as_of: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # Yahoo Finance statements, kept in their own column rather than merged into
    # fundamentals_json: the two sources define several lines differently (lease
    # liabilities in debt, right-of-use assets in PPE), so each stays whole and
    # the scorer picks per parameter instead of splicing one series from both.
    yahoo_json: Mapped[Optional[str]] = mapped_column(nullable=True)
    yahoo_as_of: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    yahoo_symbol: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # Yahoo's GICS-like classification, kept in columns of its own so the
    # holdings table can read them without parsing yahoo_json per row, and so
    # Screener's sparser `industry` above is preserved rather than overwritten.
    yahoo_industry: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    yahoo_sector: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    source_file: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    as_of: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))
