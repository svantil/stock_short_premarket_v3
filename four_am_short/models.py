"""Data contracts shared by the historical provider and strategy."""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import STRATEGY_ID, STRATEGY_NAME

EASTERN = ZoneInfo("America/New_York")


class DataError(ValueError):
    """Invalid input or unavailable/invalid market data."""


@dataclass(frozen=True)
class Candidate:
    trading_date: date
    symbol: str


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


@dataclass(frozen=True)
class PreviousClose:
    trading_date: date
    close: float
    source_close: float | None = None
    split_factor: float = 1


@dataclass
class TradeResult:
    date: str
    symbol: str
    status: str
    reason: str = ""
    previous_close_date: str = ""
    previous_close: float | None = None
    gap_threshold: float | None = None
    first_gap_time: str = ""
    first_gap_bar_high: float | None = None
    early_high: float | None = None
    early_high_bar_time: str = ""
    early_high_time: str = ""
    high_time_basis: str = ""
    order_active_time: str = ""
    entry_limit: float | None = None
    entry_time: str = ""
    entry_price: float | None = None
    shares: int = 0
    stop_price: float | None = None
    target_price: float | None = None
    exit_time: str = ""
    exit_price: float | None = None
    exit_reason: str = ""
    profit_target_hit: bool | None = None
    gross_pnl: float | None = None
    commission: float | None = None
    locate_cost: float | None = None
    net_pnl: float | None = None
    ambiguous_bars: int = 0
    notes: str = ""
    strategy_id: str = STRATEGY_ID
    strategy_name: str = STRATEGY_NAME
    trade_number: int = 1
