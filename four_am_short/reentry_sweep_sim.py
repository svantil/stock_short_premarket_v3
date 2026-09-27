"""Prepared, equivalent re-entry simulation for historical parameter sweeps.

The first trade and source bars must already have passed ``strategy.simulate``.
Preparation sorts timestamps once. Each evaluation preserves the reference
simulator's fills, metadata, ambiguity counts, and incomplete-position rules,
while computing an open position's decimal price thresholds only once.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Sequence

from .config import StrategyConfig
from .models import Bar, DataError, TradeResult
from .pricing import order_price
from .strategy import (
    EASTERN,
    ONE_MINUTE,
    _at,
    _buy_fill,
    _choose_path,
    _entry_bar_out_of_range,
    _path,
    _price_percent,
    _record_entry,
    _record_exit,
    _stamp,
)


@dataclass(frozen=True)
class PreparedReentryCase:
    """Serializable case; callers must treat the copied initial row as read-only."""

    initial: TradeResult
    bars: tuple[Bar, ...]
    stamps: tuple[datetime, ...]
    day: date
    activation: datetime
    first_index: int


def prepare_case(initial: TradeResult, bars: Sequence[Bar]) -> PreparedReentryCase:
    """Prepare validated history for one completed first-trade stop-loss.

    This intentionally does not repeat OHLC validation: the initial row must
    come from the reference simulator using this same price history.
    """
    if initial.trade_number != 1 or initial.status != "trade" or initial.exit_reason != "stop_loss":
        raise DataError("A re-entry sweep requires a completed first-trade stop-loss")
    if initial.early_high is None or not initial.exit_time:
        raise DataError("Initial stop-loss is missing its early high or exit time")
    day = date.fromisoformat(initial.date)
    activation = datetime.fromisoformat(initial.exit_time).astimezone(EASTERN) + ONE_MINUTE
    ordered = sorted(
        ((stamp, bar) for bar in bars if (stamp := _stamp(bar)).date() == day),
        key=lambda item: item[0],
    )
    stamps = tuple(stamp for stamp, _ in ordered)
    return PreparedReentryCase(
        initial=replace(initial),
        bars=tuple(bar for _, bar in ordered),
        stamps=stamps,
        day=day,
        activation=activation,
        first_index=bisect_left(stamps, activation),
    )


def simulate_prepared(case: PreparedReentryCase, config: StrategyConfig) -> TradeResult:
    """Return trade 2, equivalent to ``simulate_trades(..., config)[1]``.

    The config that produced the initial trade must match this config except
    for its re-entry parameters. Re-entry must be enabled. Neither the source
    bars nor the initial result are mutated.
    """
    if not config.reentry.enabled:
        raise DataError("Re-entry must be enabled for a re-entry sweep")
    if config.intrabar_policy not in {"conservative", "ohlc", "olhc"}:
        raise DataError(f"Unknown intrabar policy: {config.intrabar_policy}")
    initial = case.initial
    assert initial.early_high is not None
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
        order_active_time=case.activation.isoformat(),
        entry_limit=limit,
        notes=(
            initial.notes
            + " Re-entry activates at the next minute after the first stop bar;"
            " same-bar post-stop prices cannot be inferred. Original early high"
            " retained; previously paid locates reused without additional locate cost."
        ),
        trade_number=2,
    )
    deadline = _at(case.day, config.reentry.entry_deadline)
    cutoff = _at(case.day, config.reentry.time_exit)
    if not config.entry_price_allowed(limit):
        result.reason = "entry_price_out_of_range"
        return result
    if case.activation >= deadline:
        result.reason = "activation_at_or_after_deadline"
        return result

    retry_config = replace(
        config,
        entry_deadline=config.reentry.entry_deadline,
        stop_loss_percent=config.reentry.stop_loss_percent,
        profit_target_percent=config.reentry.profit_target_percent,
        time_exit=config.reentry.time_exit,
        locate_fee_per_share=0.0,
    )
    stop = target = None
    rejected_entry_price = False
    for index in range(case.first_index, len(case.bars)):
        bar = case.bars[index]
        stamp = case.stamps[index]
        if stamp > cutoff:
            break
        if result.entry_price is None and stamp >= deadline:
            break
        if stamp == cutoff:
            if result.entry_price is not None:
                return _record_exit(
                    result, _buy_fill(bar.open, retry_config), "time_exit", stamp, retry_config
                )
            break

        if result.entry_price is None:
            if bar.high < limit:
                continue
            if _entry_bar_out_of_range(bar, limit, retry_config):
                rejected_entry_price = True
                continue
            # The entry minute can hit its low before the sell limit becomes
            # active. Use the full reference paths for that chronology.
            ohlc = _path(bar, "ohlc", None, limit, retry_config)
            olhc = _path(bar, "olhc", None, limit, retry_config)
            result.ambiguous_bars += ohlc != olhc
            chosen = _choose_path(ohlc, olhc, config.intrabar_policy)
            if chosen.entry_price is not None:
                _record_entry(result, chosen.entry_price, stamp, retry_config)
                stop, target = result.stop_price, result.target_price
            if chosen.exit_price is not None:
                return _record_exit(
                    result, chosen.exit_price, chosen.exit_reason, stamp, retry_config
                )
            continue

        # Once a short is open, gap checks happen before either OHLC path.
        # With the open between the thresholds, the paths can differ only
        # when BOTH thresholds are touched: high-first stops; low-first wins.
        assert stop is not None and target is not None
        if bar.open >= stop:
            return _record_exit(
                result, _buy_fill(bar.open, retry_config), "stop_loss", stamp, retry_config
            )
        if bar.open <= target:
            return _record_exit(
                result, _buy_fill(target, retry_config), "profit_target", stamp, retry_config
            )
        stopped = bar.high >= stop
        targeted = bar.low <= target
        if stopped and targeted:
            result.ambiguous_bars += 1
        if stopped and not (targeted and config.intrabar_policy == "olhc"):
            return _record_exit(
                result, _buy_fill(stop, retry_config), "stop_loss", stamp, retry_config
            )
        if targeted:
            return _record_exit(
                result, _buy_fill(target, retry_config), "profit_target", stamp, retry_config
            )

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
