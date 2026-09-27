"""Readable candidate details shared by the terminal and saved text report."""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import StrategyConfig
from .models import EASTERN, TradeResult


_EXIT_LABELS = {
    "profit_target": "PROFIT TARGET",
    "stop_loss": "STOP LOSS",
    "time_exit": "TIME EXIT",
}


def _price(value: float | None) -> str:
    if value is None:
        return "N/A"
    # Preserve sub-penny prices without printing floating-point tail noise.
    whole, fraction = f"{value:,.8f}".split(".")
    return f"${whole}.{fraction.rstrip('0').ljust(2, '0')}"


def _money(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else "+" if signed and value > 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _clock(value: str) -> str:
    if not value:
        return "N/A"
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=EASTERN)
    stamp = stamp.astimezone(EASTERN)
    pattern = "%I:%M:%S %p %Z" if stamp.second else "%I:%M %p %Z"
    return stamp.strftime(pattern).lstrip("0")


def _schedule(day: str, clock: str) -> str:
    return _clock(f"{day}T{clock}:00")


def _target_hit(result: TradeResult) -> str:
    if result.entry_price is None:
        return "N/A"
    if result.status == "incomplete" or not result.exit_reason:
        return "UNRESOLVED"
    if result.profit_target_hit is not None:
        return "YES" if result.profit_target_hit else "NO"
    # Also render older in-memory records whose explicit flag is unset.
    return "YES" if result.exit_reason == "profit_target" else "NO"


def format_candidate(
    result: TradeResult,
    config: StrategyConfig,
    *,
    index: int | None = None,
    total: int | None = None,
) -> str:
    """Format a candidate without conflating target attainment and profitability.

    First-gap and setup-high prices are observed *bar highs*, not exact prints
    at the reported minute-start timestamps. The separate wait reference can
    instead identify the end of the high's minute.
    """
    progress = f"[{index}/{total}] " if index is not None and total is not None else f"[{index}] " if index is not None else ""
    outcome = result.reason or result.exit_reason
    label = _EXIT_LABELS.get(outcome, outcome.replace("_", " "))
    header = f"{progress}{result.date} {result.symbol}: {result.status.upper()}"
    if label:
        header += f" | {label}"
    if result.net_pnl is not None:
        header += f" | Net {_money(result.net_pnl, signed=True)}"
    if result.status == "error" and result.notes:
        header += f" | {result.notes}"
    reentry = result.trade_number == 2
    header += " | Re-entry (trade 2)" if reentry else " | First entry (trade 1)"

    # A failed request or absent discovery bars has no trade timeline to show.
    if result.early_high is None and result.entry_price is None:
        return header

    lines = [header]

    def add(value: str) -> None:
        lines.append(f"  {value}")

    late_setup = False
    if config.late_gap_enabled and result.first_gap_time:
        first_gap = datetime.fromisoformat(result.first_gap_time)
        if first_gap.tzinfo is None:
            first_gap = first_gap.replace(tzinfo=EASTERN)
        late_setup = first_gap.astimezone(EASTERN).strftime("%H:%M") >= config.early_end
    high_label = "Late setup high" if late_setup else "Early high"
    high_name = high_label.lower()

    add("Times are Eastern; fill and signal timestamps label minute-bar starts.")
    add(f"Previous regular-session close: {_price(result.previous_close)} ({result.previous_close_date or 'N/A'})")
    add(f"Gap qualification: price > {_price(result.gap_threshold)} (+{config.gap_percent:g}%)")
    if result.first_gap_time:
        add(f"First qualifying bar: {_clock(result.first_gap_time)}; bar high {_price(result.first_gap_bar_high)}")
    else:
        add("First qualifying bar: NONE (gap threshold not exceeded)")
    if config.late_gap_enabled:
        add(f"Discovery window: {_schedule(result.date, config.early_start)} to {_schedule(result.date, config.entry_deadline)} (end excluded)")
    if late_setup:
        late_end = first_gap + timedelta(minutes=config.late_gap_window_minutes)
        add(f"Late setup window: {_clock(result.first_gap_time)} to {_clock(late_end.isoformat())} (end excluded)")
        add("High freezes after this window; entry waits for window completion and the high delay.")
    else:
        add(f"Early window: {_schedule(result.date, config.early_start)} to {_schedule(result.date, config.early_end)} (end excluded)")
    if result.early_high is not None:
        add(f"{high_label}: {_price(result.early_high)}; source bar {_clock(result.early_high_bar_time)}")
        basis = result.high_time_basis or config.high_time_reference
        if reentry:
            add(f"Re-entry follows the first trade's stop; the original {high_name} is retained.")
            add("Backtest assumption: order activates at the next minute after the first stop bar.")
            add("Locates reuse the first trade's shares; no additional locate fee.")
        else:
            add(f"Wait reference: {_clock(result.early_high_time)} ({basis.replace('_', ' ')}); wait {config.wait_after_high_minutes} minutes")
    if result.entry_limit is not None:
        if reentry:
            add(f"Sell limit: {_price(result.entry_limit)} ({config.reentry.entry_above_high_percent:g}% above {high_name})")
        else:
            add(f"Sell limit: {_price(result.entry_limit)} ({config.entry_below_high_percent:g}% below {high_name})")
        rules = config.reentry if reentry else config
        add(f"Order active: {_clock(result.order_active_time)}; entry strictly before {_schedule(result.date, rules.entry_deadline)}")
        add(f"Time exit: {_schedule(result.date, rules.time_exit)}")

    if result.entry_price is None:
        add("Entry: NOT FILLED")
        add("Exit: N/A; profit target hit: N/A")
        return "\n".join(lines)

    add(f"Entry: SHORT {result.shares:,} shares at {_price(result.entry_price)}; {_clock(result.entry_time)}")
    rules = config.reentry if reentry else config
    add(f"Stop: {_price(result.stop_price)} (+{rules.stop_loss_percent:g}%); target: {_price(result.target_price)} (-{rules.profit_target_percent:g}%)")
    if result.exit_price is None:
        add("Exit: UNRESOLVED (position remains open in the backtest)")
    else:
        exit_label = _EXIT_LABELS.get(result.exit_reason, result.exit_reason.replace("_", " ").upper())
        add(f"Exit: COVER at {_price(result.exit_price)}; {_clock(result.exit_time)}; {exit_label}")
    add(f"Profit target hit: {_target_hit(result)}")
    if result.net_pnl is None:
        add("Realized P/L: UNAVAILABLE until the position is closed")
    else:
        add(f"Gross P/L: {_money(result.gross_pnl, signed=True)}; commission: {_money(result.commission)}; locate: {_money(result.locate_cost)}; net P/L: {_money(result.net_pnl, signed=True)}")
    if result.ambiguous_bars:
        add(f"Ambiguous minute bars: {result.ambiguous_bars}; intrabar policy: {config.intrabar_policy}")
    return "\n".join(lines)
