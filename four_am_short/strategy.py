"""Pure, deterministic one-minute-bar simulation of the premarket short setup.

Bars label the beginning of their minute. The early window is [start, end),
and an early high is timestamped at the bar's end by default, so the requested
wait has certainly elapsed. Repeated highs restart that wait by default.
When late-gap discovery is enabled, stocks first qualifying after the early
window get their own fixed observation window and the same high-based delay.

OHLC bars do not reveal the order of their high and low. We evaluate both
open-high-low-close and open-low-high-close paths. The conservative policy
chooses a stop if either path stops; otherwise it takes a target only when
both paths reach that target *after entry*. A target on just one path leaves
the position open. The alternate policies select their named path explicitly.
This is a local adverse-path assumption, not a claim of worst possible P&L
across every possible future price path.

Fill event timestamps label their bar; they are not tick-accurate timestamps.
Sell limits allow improvement at the open, with adverse entry slippage capped
at the limit. Stops gap at the open, targets receive no favorable gap-price
improvement, and buys include configured adverse exit slippage. Time exits
require the exact cutoff bar and use its open before intrabar processing.

Optional re-entry uses the original early high and starts at the next minute
after the first trade's stop bar. A minute bar cannot establish the prices
available after its stop fill, so that bar is never reused for re-entry.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import isfinite
from typing import Sequence
from zoneinfo import ZoneInfo

from .config import StrategyConfig
from .models import Bar, DataError, PreviousClose, TradeResult
from .pricing import order_price
from .setups import select_setup


EASTERN = ZoneInfo("America/New_York")
ONE_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class _PathResult:
    entry_price: float | None
    exit_reason: str = ""
    exit_price: float | None = None


def _at(day: date, value: str) -> datetime:
    try:
        return datetime.combine(day, time.fromisoformat(value), tzinfo=EASTERN)
    except (TypeError, ValueError) as exc:
        raise DataError(f"Invalid strategy time: {value!r}") from exc


def _stamp(bar: Bar) -> datetime:
    if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
        raise DataError("Bar timestamps must be timezone-aware")
    if bar.timestamp.second or bar.timestamp.microsecond:
        raise DataError("Minute-bar timestamps must align to the start of a minute")
    return bar.timestamp.astimezone(EASTERN)


def _price_percent(price: float, percent: float, sign: int) -> float:
    """Keep exact decimal boundary prices such as 10 * 1.30 equal to 13."""
    return float(
        Decimal(str(price))
        * (Decimal(1) + Decimal(sign) * Decimal(str(percent)) / Decimal(100))
    )


def _entry_fill(price: float, limit: float, config: StrategyConfig) -> float:
    return max(limit, price * (1 - config.entry_slippage_bps / 10_000))


def _entry_bar_out_of_range(bar: Bar, limit: float, config: StrategyConfig) -> bool:
    """Reject an out-of-range modeled fill without inventing a capped price.

    If an opening fill is ineligible, skip its entire minute. A later minute
    may still fill before the deadline; OHLC data cannot establish a fresh
    eligibility check and order submission within the rejected minute.
    """
    return bar.high >= limit and not config.entry_price_allowed(
        _entry_fill(max(bar.open, limit), limit, config)
    )


def _buy_fill(price: float, config: StrategyConfig) -> float:
    return price * (1 + config.exit_slippage_bps / 10_000)


def _path(
    bar: Bar,
    path_name: str,
    existing_entry: float | None,
    limit: float,
    config: StrategyConfig,
) -> _PathResult:
    """Walk a piecewise-linear OHLC path, ignoring prices before the entry."""
    points = (
        (bar.open, bar.high, bar.low, bar.close)
        if path_name == "ohlc"
        else (bar.open, bar.low, bar.high, bar.close)
    )
    entry = existing_entry
    if entry is None and bar.open >= limit:
        entry = _entry_fill(bar.open, limit, config)

    if entry is not None:
        stop = _price_percent(entry, config.stop_loss_percent, 1)
        target = _price_percent(entry, config.profit_target_percent, -1)
        # An already-open position can gap through the stop. Checking new
        # fills here also handles adverse entry slippage beyond a tight stop.
        if bar.open >= stop:
            return _PathResult(entry, "stop_loss", _buy_fill(bar.open, config))
        if bar.open <= target:
            return _PathResult(entry, "profit_target", _buy_fill(target, config))

    for start, end in zip(points, points[1:]):
        if entry is None:
            if start < limit <= end:
                entry = _entry_fill(limit, limit, config)
                stop = _price_percent(entry, config.stop_loss_percent, 1)
                target = _price_percent(entry, config.profit_target_percent, -1)
                start = limit
            else:
                continue
        if end >= start and start <= stop <= end:
            return _PathResult(entry, "stop_loss", _buy_fill(stop, config))
        if end <= start and end <= target <= start:
            return _PathResult(entry, "profit_target", _buy_fill(target, config))
    return _PathResult(entry)


def _choose_path(
    ohlc: _PathResult, olhc: _PathResult, policy: str
) -> _PathResult:
    if policy == "ohlc":
        return ohlc
    if policy == "olhc":
        return olhc
    if policy != "conservative":
        raise DataError(f"Unknown intrabar policy: {policy}")
    stopped = [p for p in (ohlc, olhc) if p.exit_reason == "stop_loss"]
    if stopped:
        return max(stopped, key=lambda p: p.exit_price or 0)
    if ohlc.exit_reason == olhc.exit_reason == "profit_target":
        return max((ohlc, olhc), key=lambda p: p.exit_price or 0)
    # If only one path reaches the target, the other is a feasible path that
    # retains the position. Carry precisely that state into the next minute.
    return next(p for p in (ohlc, olhc) if not p.exit_reason)


def _record_entry(
    result: TradeResult, entry: float, timestamp: datetime, config: StrategyConfig
) -> None:
    result.entry_time = timestamp.isoformat()
    result.entry_price = entry
    result.shares = config.shares
    result.stop_price = _price_percent(entry, config.stop_loss_percent, 1)
    result.target_price = _price_percent(entry, config.profit_target_percent, -1)
    result.status = "incomplete"


def _record_exit(
    result: TradeResult,
    price: float,
    reason: str,
    timestamp: datetime,
    config: StrategyConfig,
) -> TradeResult:
    assert result.entry_price is not None
    result.status = "trade"
    result.reason = ""
    result.exit_time = timestamp.isoformat()
    result.exit_price = price
    result.exit_reason = reason
    result.profit_target_hit = reason == "profit_target"
    result.gross_pnl = (result.entry_price - price) * config.shares
    result.commission = config.shares * config.commission_per_share_per_side * 2
    result.locate_cost = config.shares * config.locate_fee_per_share
    result.net_pnl = result.gross_pnl - result.commission - result.locate_cost
    return result


def simulate(
    day: date,
    symbol: str,
    bars: Sequence[Bar],
    previous_close: PreviousClose,
    config: StrategyConfig,
) -> TradeResult:
    """Return one completed trade, skip, or unresolved position for a symbol/day.

    ``previous_close`` must already be the previous regular session's close,
    normalized for any intervening stock split by the data provider. Sparse
    minute bars are allowed because minutes without qualifying trades need
    not have aggregate bars. Missing an exact time-exit bar never produces a
    made-up close, and an unresolved position has no realized P&L.
    """
    if previous_close.trading_date >= day:
        raise DataError("Previous close must precede the simulation date")
    if not isfinite(previous_close.close) or previous_close.close <= 0:
        raise DataError("Previous close must be finite and positive")
    if config.high_time_reference not in {"bar_start", "bar_end"}:
        raise DataError("high_time_reference must be bar_start or bar_end")
    if config.repeated_high_policy not in {"first", "last"}:
        raise DataError("repeated_high_policy must be first or last")

    start = _at(day, config.early_start)
    end = _at(day, config.early_end)
    deadline = _at(day, config.entry_deadline)
    cutoff = _at(day, config.time_exit)
    ordered = sorted((bar for bar in bars if _stamp(bar).date() == day), key=_stamp)
    seen = set()
    for bar in ordered:
        stamp = _stamp(bar)
        if stamp in seen:
            raise DataError(f"Duplicate minute bar at {stamp.isoformat()}")
        seen.add(stamp)
        prices = (bar.open, bar.high, bar.low, bar.close)
        if not all(isfinite(p) and p > 0 for p in prices):
            raise DataError(f"Invalid bar price at {stamp.isoformat()}")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise DataError(f"Inconsistent OHLC bar at {stamp.isoformat()}")

    threshold_decimal = Decimal(str(previous_close.close)) * (
        Decimal(1) + Decimal(str(config.gap_percent)) / Decimal(100)
    )
    result = TradeResult(
        date=day.isoformat(),
        symbol=symbol,
        status="skipped",
        previous_close_date=previous_close.trading_date.isoformat(),
        previous_close=previous_close.close,
        gap_threshold=float(threshold_decimal),
        high_time_basis=config.high_time_reference,
        notes=(
            "Event times label minute-bar starts; early-high time uses "
            f"{config.high_time_reference}; intrabar policy={config.intrabar_policy}."
        ),
    )
    if previous_close.split_factor != 1:
        result.notes += (
            f" Previous close split-normalized: source={previous_close.source_close},"
            f" factor={previous_close.split_factor}, normalized={previous_close.close}."
        )
    scan_end = deadline if config.late_gap_enabled else end
    early = [bar for bar in ordered if start <= _stamp(bar) < scan_end]
    if not early:
        result.reason = "no_premarket_bars" if config.late_gap_enabled else "no_early_bars"
        return result

    setup = select_setup(day, ordered, previous_close.close, config)
    if setup is None:
        high = max(bar.high for bar in early)
        high_bars = [bar for bar in early if bar.high == high]
        high_bar = high_bars[-1] if config.repeated_high_policy == "last" else high_bars[0]
        high_stamp = _stamp(high_bar)
        if config.high_time_reference == "bar_end":
            high_stamp += ONE_MINUTE
    else:
        high_bar, high_stamp = setup.high_bar, setup.high_time
        high = high_bar.high
    result.early_high = high
    result.early_high_bar_time = _stamp(high_bar).isoformat()
    result.early_high_time = high_stamp.isoformat()
    if setup is None:
        result.reason = "gap_threshold_not_exceeded"
        return result
    result.first_gap_time = _stamp(setup.first_gap_bar).isoformat()
    result.first_gap_bar_high = setup.first_gap_bar.high
    if setup.late_gap:
        result.notes += (
            f" Late gap: high measured in the {config.late_gap_window_minutes}-minute"
            " window starting at the first qualifying bar, then frozen."
        )

    activation = setup.active_at
    limit = order_price(_price_percent(high, config.entry_below_high_percent, -1))
    result.order_active_time = activation.isoformat()
    result.entry_limit = limit
    return _simulate_order(result, ordered, activation, deadline, cutoff, limit, config)


def _simulate_order(
    result: TradeResult,
    ordered: Sequence[Bar],
    activation: datetime,
    deadline: datetime,
    cutoff: datetime,
    limit: float,
    config: StrategyConfig,
) -> TradeResult:
    """Apply common entry/exit fills to one independently identified attempt."""
    if not config.entry_price_allowed(limit):
        result.reason = "entry_price_out_of_range"
        return result
    if activation >= deadline:
        result.reason = "activation_at_or_after_deadline"
        return result

    rejected_entry_price = False
    for bar in ordered:
        stamp = _stamp(bar)
        if stamp < activation:
            continue
        if stamp > cutoff:
            break
        if result.entry_price is None and stamp >= deadline:
            break
        if stamp == cutoff:
            # The cutoff's open takes precedence over its later high/low.
            if result.entry_price is not None:
                return _record_exit(
                    result, _buy_fill(bar.open, config), "time_exit", stamp, config
                )
            break

        if result.entry_price is None and _entry_bar_out_of_range(bar, limit, config):
            rejected_entry_price = True
            continue
        ohlc = _path(bar, "ohlc", result.entry_price, limit, config)
        olhc = _path(bar, "olhc", result.entry_price, limit, config)
        if ohlc != olhc:
            result.ambiguous_bars += 1
        chosen = _choose_path(ohlc, olhc, config.intrabar_policy)
        if result.entry_price is None and chosen.entry_price is not None:
            _record_entry(result, chosen.entry_price, stamp, config)
        if chosen.exit_price is not None:
            return _record_exit(result, chosen.exit_price, chosen.exit_reason, stamp, config)

    if result.entry_price is None:
        result.reason = (
            "entry_price_out_of_range" if rejected_entry_price
            else "entry_not_filled_before_deadline"
        )
    else:
        result.status = "incomplete"
        result.reason = "missing_time_exit_bar"
        result.notes += " Position remains unresolved; realized P&L is unavailable."
    return result


def simulate_trades(
    day: date,
    symbol: str,
    bars: Sequence[Bar],
    previous_close: PreviousClose,
    config: StrategyConfig,
) -> list[TradeResult]:
    """Return the first attempt and, after a completed stop, one optional retry.

    Re-entry uses the original early high, actual fill-based stop/target,
    configured re-entry deadline and time exit, and the initial share count.
    It reuses already-paid locates; only round-trip commissions are charged
    again. A second stop cannot produce a third attempt. ``simulate`` remains
    the compatible single-attempt API for callers that only need that result.
    """
    initial = simulate(day, symbol, bars, previous_close, config)
    rows = [initial]
    if not config.reentry.enabled or initial.status != "trade" or initial.exit_reason != "stop_loss":
        return rows

    assert initial.early_high is not None
    activation = datetime.fromisoformat(initial.exit_time).astimezone(EASTERN) + ONE_MINUTE
    limit = order_price(_price_percent(initial.early_high, config.reentry.entry_above_high_percent, 1))
    result = TradeResult(
        date=initial.date,
        symbol=initial.symbol,
        status="skipped",
        previous_close_date=initial.previous_close_date,
        previous_close=initial.previous_close,
        gap_threshold=initial.gap_threshold,
        first_gap_time=initial.first_gap_time,
        first_gap_bar_high=initial.first_gap_bar_high,
        early_high=initial.early_high,
        early_high_bar_time=initial.early_high_bar_time,
        early_high_time=initial.early_high_time,
        high_time_basis=initial.high_time_basis,
        order_active_time=activation.isoformat(),
        entry_limit=limit,
        notes=(
            initial.notes
            + " Re-entry activates at the next minute after the first stop bar;"
            " same-bar post-stop prices cannot be inferred. Original early high"
            " retained; previously paid locates reused without additional locate cost."
        ),
        trade_number=2,
    )
    reentry_config = replace(
        config,
        entry_deadline=config.reentry.entry_deadline,
        stop_loss_percent=config.reentry.stop_loss_percent,
        profit_target_percent=config.reentry.profit_target_percent,
        time_exit=config.reentry.time_exit,
        locate_fee_per_share=0.0,
    )
    ordered = sorted((bar for bar in bars if _stamp(bar).date() == day), key=_stamp)
    rows.append(_simulate_order(
        result, ordered, activation, _at(day, reentry_config.entry_deadline),
        _at(day, reentry_config.time_exit), limit, reentry_config,
    ))
    return rows
