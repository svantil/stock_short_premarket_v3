"""Shared setup selection for historical simulation and closed live bars.

The original early window stays intact. Later first-time qualifiers start a
fixed observation window, then use the same frozen high and waiting rules.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Sequence

from .config import StrategyConfig
from .models import Bar, EASTERN


@dataclass(frozen=True)
class Setup:
    first_gap_bar: Bar
    high_bar: Bar
    high_time: datetime
    active_at: datetime
    late_gap: bool
    window_end: datetime


def select_setup(
    day: date, bars: Sequence[Bar], previous_close: float, config: StrategyConfig,
) -> Setup | None:
    """Select the one initial setup, without using post-window highs.

    Callers validate prices/timestamps; live callers supply completed bars only.
    Re-running after its window completes yields the same frozen setup, making
    backfills and backtests independent of the order in which bars arrived.
    """
    def at(clock: str) -> datetime:
        return datetime.combine(day, time.fromisoformat(clock), tzinfo=EASTERN)

    start, end = at(config.early_start), at(config.early_end)
    scan_end = at(config.entry_deadline) if config.late_gap_enabled else end
    ordered = sorted((bar for bar in bars if start <= bar.timestamp < scan_end),
                     key=lambda bar: bar.timestamp)
    threshold = Decimal(str(previous_close)) * (
        Decimal(1) + Decimal(str(config.gap_percent)) / Decimal(100)
    )
    first = next((bar for bar in ordered if Decimal(str(bar.high)) > threshold), None)
    if first is None:
        return None

    minute = timedelta(minutes=1)
    wait = timedelta(minutes=config.wait_after_high_minutes)

    def high_time(bar: Bar) -> datetime:
        return bar.timestamp.astimezone(EASTERN) + (
            minute if config.high_time_reference == "bar_end" else timedelta()
        )

    late = first.timestamp >= end
    window_start = first.timestamp if late else start
    window_end = (first.timestamp + timedelta(minutes=config.late_gap_window_minutes)
                  if late else end)
    window = [bar for bar in ordered if window_start <= bar.timestamp < window_end]
    high = max(bar.high for bar in window)
    matches = [bar for bar in window if bar.high == high]
    high_bar = matches[-1] if config.repeated_high_policy == "last" else matches[0]
    reference = high_time(high_bar)
    active = max(window_end, reference + wait)
    return Setup(first, high_bar, reference, active.astimezone(EASTERN), late,
                 window_end.astimezone(EASTERN))
