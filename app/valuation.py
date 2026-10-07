"""Single source of truth for what a portfolio is currently worth.

Every tab (Overview, Portfolio, Risk) values holdings through :func:`value_holdings`
so the headline total, the holdings table and the concentration percentages can
never disagree with each other.

Valuation basis, per holding:

* **live** — ``CMP x quantity``, used whenever a scraped/imported price *and* a
  share quantity are both known.
* **statement** — the value printed on the statement, used otherwise.

The mix is deliberate. Valuing only the matched holdings would make them sum to
100% of a smaller portfolio and quietly drop the rest (cash lines, delisted
scrips and mutual funds have no equity price by nature). Callers are given the
counts so a partly-live valuation is always disclosed rather than implied.
"""

from dataclasses import dataclass, field
from typing import Optional

from app.market_data import build_lookup, match_holding


@dataclass
class HoldingValue:
    holding_id: int
    effective_value: float          # what the dashboard shows and weights against
    statement_value: float          # value as printed on the statement
    live_value: Optional[float]     # CMP x quantity, or None
    cmp: Optional[float]
    basis: str                      # "live" | "statement"
    price_source: Optional[str] = None   # "NSE close" | "BSE close" | "AMFI NAV" | "screener.in"
    market: object = None           # matched MarketData row, or None
    matched_by: Optional[str] = None


@dataclass
class PortfolioValuation:
    by_holding: dict = field(default_factory=dict)
    market_total: float = 0.0       # headline: live where known, statement otherwise
    statement_total: float = 0.0    # what the statements alone add up to
    live_total: float = 0.0         # the live-priced subset only
    priced_count: int = 0
    statement_count: int = 0
    market_rows_available: bool = False

    @property
    def fully_priced(self) -> bool:
        return self.priced_count > 0 and self.statement_count == 0

    def get(self, holding_id: int) -> Optional[HoldingValue]:
        return self.by_holding.get(holding_id)

    def weight(self, holding_id: int) -> float:
        """Weight of one holding in the current market value of the portfolio."""
        entry = self.by_holding.get(holding_id)
        if not entry or not self.market_total:
            return 0.0
        return round(entry.effective_value / self.market_total * 100, 2)

    def as_dict(self) -> dict:
        """Disclosure block for API responses."""
        return {
            "available": self.market_rows_available,
            "priced_holdings": self.priced_count,
            "statement_priced_holdings": self.statement_count,
            "market_value": round(self.market_total, 2),
            "statement_value": round(self.statement_total, 2),
            "live_value_partial": round(self.live_total, 2) if self.priced_count else None,
            "fully_priced": self.fully_priced,
        }


def value_holdings(holdings, market_rows) -> PortfolioValuation:
    """Value every holding at the best basis available."""
    by_isin, by_code, by_name = build_lookup(market_rows or [])
    result = PortfolioValuation(market_rows_available=bool(market_rows))

    for h in holdings:
        statement_value = h.current_value or 0.0
        market, matched_by = match_holding(h, by_isin, by_code, by_name)

        # Mark to market only with BOTH a price and a share count. Deriving
        # quantity from value/price would just reproduce the statement figure.
        live_value = None
        if market and market.cmp and h.quantity:
            live_value = round(market.cmp * h.quantity, 2)

        effective = live_value if live_value is not None else statement_value
        result.by_holding[h.id] = HoldingValue(
            holding_id=h.id,
            effective_value=effective,
            statement_value=statement_value,
            live_value=live_value,
            cmp=market.cmp if market else None,
            basis="live" if live_value is not None else "statement",
            price_source=getattr(market, "price_source", None) if market else None,
            market=market,
            matched_by=matched_by,
        )

        result.market_total += effective
        result.statement_total += statement_value
        if live_value is not None:
            result.live_total += live_value
            result.priced_count += 1
        else:
            result.statement_count += 1

    return result
