"""Small, credential-free snapshots of the inputs behind live decisions.

These are observations, not a tick replay. Broker fills remain authoritative;
minute bars and quotes are saved separately so later feed corrections cannot
silently rewrite the explanation for an already submitted trade.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Iterable

from ..config import StrategyConfig
from ..models import Bar

SCHEMA = 1
DATA_FEED = "alpaca_sip"
SETUP_FIELDS = (
    "symbol", "date", "trade_number", "previous_close", "gap_threshold",
    "first_gap_time", "first_gap_bar_high", "early_high", "late_gap",
    "early_high_bar_time", "early_high_time", "window_end", "active_at",
    "entry_limit", "entry_deadline", "time_exit", "stop_percent", "target_percent",
)


def quote_snapshot(quote: dict | None) -> dict | None:
    return {key: quote[key] for key in ("timestamp", "bid", "ask") if key in quote} if quote else None


def session_snapshot(session_id: str, now: datetime, strategy: StrategyConfig,
                     previous_close_date: str | None, execution: dict) -> dict:
    return {"session_id": session_id, "started_at": now.isoformat(),
            "strategy": asdict(strategy), "previous_close_date": previous_close_date,
            "execution": dict(execution)}


def entry_snapshot(*, session_id: str, now: datetime, strategy: StrategyConfig,
                   candidate: dict, previous_close_date: str | None,
                   quote: dict | None, bars: Iterable[Bar], bars_source: str,
                   window_finalized: bool, early_window_finalized: bool) -> dict:
    return {
        "schema": SCHEMA, "data_feed": DATA_FEED, "session_id": session_id,
        "decision_time": now.isoformat(), "strategy": asdict(strategy),
        "previous_close": candidate.get("previous_close"),
        "previous_close_date": previous_close_date,
        "quote": quote_snapshot(quote),
        "setup": {key: candidate[key] for key in SETUP_FIELDS if key in candidate},
        "window_finalized": window_finalized,
        "early_window_finalized": early_window_finalized,
        "setup_bars_source": bars_source,
        "setup_bars": [
            {"timestamp": bar.timestamp.isoformat(), "open": bar.open, "high": bar.high,
             "low": bar.low, "close": bar.close, "volume": bar.volume}
            for bar in sorted(bars, key=lambda bar: bar.timestamp)
        ],
    }


def exit_snapshot(*, session_id: str, now: datetime, trade: dict,
                  reason: str, quote: dict | None) -> dict:
    return {
        "schema": SCHEMA, "data_feed": DATA_FEED, "session_id": session_id,
        "decision_time": now.isoformat(), "reason": reason,
        "quote": quote_snapshot(quote), "entry_avg_price": trade.get("entry_avg_price"),
        "entry_filled_qty": trade.get("entry_filled_qty"),
        "stop_price": trade.get("stop_price"), "target_price": trade.get("target_price"),
        "time_exit": trade.get("time_exit"),
    }
