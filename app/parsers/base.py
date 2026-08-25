from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from datetime import date

@dataclass
class ParsedHolding:
    security_name: str
    isin: Optional[str] = None
    quantity: Optional[float] = None
    avg_cost: Optional[float] = None
    total_cost: Optional[float] = None
    current_price: Optional[float] = None
    current_value: float = 0.0
    accrued_income: Optional[float] = None
    unrealized_gain: Optional[float] = None
    gain_pct: Optional[float] = None
    weight_pct: Optional[float] = None
    xirr: Optional[float] = None
    sector: Optional[str] = None
    asset_class: str = 'equity'
    scrip_type: Optional[str] = None
    status: str = 'free'
    folio_number: Optional[str] = None
    scheme_category: Optional[str] = None

@dataclass
class ParsedDocument:
    client_name: str
    account_type: str  # 'demat', 'pms', 'mutual_fund'
    account_number: Optional[str] = None
    dp_id: Optional[str] = None
    custodian: Optional[str] = None
    portfolio_name: Optional[str] = None
    statement_date: Optional[date] = None
    holdings: list[ParsedHolding] = field(default_factory=list)
    total_value: float = 0.0
    total_cost: Optional[float] = None

class BaseParser(ABC):
    @abstractmethod
    def parse(self, text: str) -> ParsedDocument:
        pass
