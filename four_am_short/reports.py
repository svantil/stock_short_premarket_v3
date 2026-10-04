"""Auditable candidate records and realized, closed-trade summaries."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import STRATEGY_ID, STRATEGY_NAME, __version__
from .config import BacktestConfig, StrategyConfig
from .models import EASTERN, TradeResult
from .trade_details import format_candidate


def summarize(rows: list[TradeResult]) -> dict:
    candidates = [row for row in rows if row.trade_number == 1]
    reentries = [row for row in rows if row.trade_number == 2]
    triggered = [row for row in candidates if row.first_gap_time]
    entered = [row for row in triggered if row.entry_price is not None]
    completed_entries = sum(row.status == "trade" for row in entered)
    trades = sorted((row for row in rows if row.status == "trade"), key=lambda r: (r.exit_time, r.symbol))
    pnl = [float(row.net_pnl) for row in trades]
    total = sum(pnl)
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    # Identical minute timestamps cannot establish which symbol exited first.
    by_exit_time: dict[str, float] = defaultdict(float)
    by_day: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        by_exit_time[trade.exit_time] += float(trade.net_pnl)
        by_day[trade.date].append(float(trade.net_pnl))
    # Match report precision so binary roundoff cannot turn a flat day into a win.
    daily_pnl = [round(math.fsum(values), 6) for values in by_day.values()]
    equity = peak = drawdown = 0.0
    for _, value in sorted(by_exit_time.items()):
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    count = Counter(row.status for row in rows)
    return {
        "candidates": len(candidates), "trades": len(trades),
        "initial_trades": sum(row.trade_number == 1 for row in trades),
        "reentry_attempts": len(reentries),
        "reentry_trades": sum(row.status == "trade" for row in reentries),
        "reentry_skipped": sum(row.status == "skipped" for row in reentries),
        "reentry_incomplete": sum(row.status == "incomplete" for row in reentries),
        "gap_triggered": len(triggered), "gap_traded": len(entered),
        "gap_not_traded": len(triggered) - len(entered),
        "gap_traded_percent": round(len(entered) / len(triggered) * 100, 4) if triggered else 0.0,
        "gap_completed": completed_entries,
        "gap_unresolved": len(entered) - completed_entries,
        "skipped": count["skipped"], "errors": count["error"], "incomplete": count["incomplete"],
        "wins": len(wins), "losses": len(losses), "breakeven": len(pnl) - len(wins) - len(losses),
        "win_percent": round(len(wins) / len(pnl) * 100, 4) if pnl else 0.0,
        "loss_percent": round(len(losses) / len(pnl) * 100, 4) if pnl else 0.0,
        "gross_pnl": round(sum(float(r.gross_pnl) for r in trades), 6),
        "commission": round(sum(float(r.commission) for r in trades), 6),
        "locate_cost": round(sum(float(r.locate_cost) for r in trades), 6),
        "net_pnl": round(total, 6),
        "net_winning_pnl": round(sum(wins), 6), "net_losing_pnl": round(sum(losses), 6),
        "profit_factor": round(sum(wins) / -sum(losses), 6) if losses else None,
        "average_net_pnl": round(total / len(pnl), 6) if pnl else None,
        "average_winner_net_pnl": round(sum(wins) / len(wins), 6) if wins else None,
        "average_loser_net_pnl": round(sum(losses) / len(losses), 6) if losses else None,
        "biggest_winner_net_pnl": round(max(wins), 6) if wins else None,
        "biggest_loser_net_pnl": round(min(losses), 6) if losses else None,
        "max_closed_trade_drawdown": round(drawdown, 6),
        "winning_days": sum(value > 0 for value in daily_pnl),
        "losing_days": sum(value < 0 for value in daily_pnl),
        "breakeven_days": sum(value == 0 for value in daily_pnl),
        "targets": sum(r.exit_reason == "profit_target" for r in trades),
        "stops": sum(r.exit_reason == "stop_loss" for r in trades),
        "time_exits": sum(r.exit_reason == "time_exit" for r in trades),
        "ambiguous_bars": sum(r.ambiguous_bars for r in rows),
    }


def _period_summaries(rows: list[TradeResult], date_length: int, *, trade_number: int | None = None) -> list[dict]:
    grouped: dict[str, list[TradeResult]] = defaultdict(list)
    for row in rows:
        period_rows = grouped[row.date[:date_length]]
        if trade_number is None or row.trade_number == trade_number:
            period_rows.append(row)
    return [
        {"strategy_id": STRATEGY_ID, "strategy_name": STRATEGY_NAME,
         "period": period, **summarize(grouped[period])}
        for period in sorted(grouped)
    ]


def monthly_summaries(rows: list[TradeResult]) -> list[dict]:
    """Include every processed month, including months with no completed trades."""
    return _period_summaries(rows, 7)


def reentry_monthly_summaries(rows: list[TradeResult]) -> list[dict]:
    """Re-entry performance for every processed month, including zero-trade months."""
    return _period_summaries(rows, 7, trade_number=2)


def gap_not_traded_reasons(rows: list[TradeResult]) -> dict[str, int]:
    return dict(sorted(Counter(
        row.reason or "unspecified" for row in rows
        if row.trade_number == 1 and row.first_gap_time and row.entry_price is None
    ).items()))


def format_gap_summary(rows: list[TradeResult], config: StrategyConfig, *, partial: bool = False) -> str:
    """Show how confirmed gap setups converted into actual short entries."""
    headers = ("Month", "Gap Triggers", "Traded", "Not Traded", "Traded%")
    overall = summarize(rows)

    def cells(period: str, stats: dict) -> tuple[str, ...]:
        return (period, f"{stats['gap_triggered']:,}", f"{stats['gap_traded']:,}",
                f"{stats['gap_not_traded']:,}", f"{stats['gap_traded_percent']:.2f}%")

    values = [cells(month["period"], month) for month in monthly_summaries(rows)]
    total = cells("TOTAL", overall)
    widths = [max(len(header), *(len(row[index]) for row in [*values, total]))
              for index, header in enumerate(headers)]

    def line(row: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[index]) if index == 0 else value.rjust(widths[index])
                         for index, value in enumerate(row))

    separator = "  ".join("-" * width for width in widths)
    discovery_end = config.entry_deadline if config.late_gap_enabled else config.early_end
    lines = [
        f"Gap-to-trade summary - {STRATEGY_NAME}" + (" (PARTIAL)" if partial else ""),
        f"Confirmed move >{config.gap_percent:g}% above the prior regular close during {config.early_start} <= time < {discovery_end} Eastern.",
        "Each stock/date counts as one setup. Traded means the first entry filled, including unresolved exits; re-entries do not add gap triggers.",
        line(headers), separator, *(line(row) for row in values), separator, line(total),
        f"Entered results: {overall['gap_completed']:,} completed; {overall['gap_unresolved']:,} unresolved exits.",
    ]
    reasons = gap_not_traded_reasons(rows)
    if reasons:
        labels = {
            "entry_not_filled_before_deadline": f"Limit did not fill before {config.entry_deadline} Eastern",
            "activation_at_or_after_deadline": f"Order activation at or after {config.entry_deadline} Eastern",
        }
        lines.append("Qualified but not traded:")
        lines.extend(f"  {labels.get(reason, reason.replace('_', ' '))}: {count:,}"
                     for reason, count in reasons.items())
    unconfirmed = [row for row in rows if row.trade_number == 1 and not row.first_gap_time]
    if unconfirmed:
        no_early = sum(row.reason in {"no_early_bars", "no_premarket_bars"} for row in unconfirmed)
        below = sum(row.reason == "gap_threshold_not_exceeded" for row in unconfirmed)
        unavailable = sum(row.status in {"error", "incomplete"} for row in unconfirmed)
        missing_bars = "no discovery bars" if config.late_gap_enabled else "no early bars"
        lines.append(f"Other processed candidates without a confirmed trigger: {len(unconfirmed):,} "
                     f"({below:,} below threshold; {no_early:,} {missing_bars}; {unavailable:,} errors/incomplete).")
        if unavailable:
            lines.append("Errors/incomplete results do not establish whether the gap rule was met.")
    return "\n".join(lines)


def _money(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def _profit_factor(stats: dict) -> str:
    value = stats["profit_factor"]
    return f"{value:.2f}" if value is not None else "inf" if stats["wins"] else "--"


def format_monthly_summary(rows: list[TradeResult], *, partial: bool = False, reentry_only: bool = False) -> str:
    """Format monthly performance and a separately calculated all-period total."""
    headers = ("Month", "Trades", "Wins", "Losses", "Win%", "Loss%", "Net P/L", "Avg/Trade", "PF",
               "Max DD", "Win Days", "Loss Days", "Stop Stocks")
    months = reentry_monthly_summaries(rows) if reentry_only else monthly_summaries(rows)
    overall = summarize([row for row in rows if row.trade_number == 2] if reentry_only else rows)

    def cells(period: str, stats: dict) -> tuple[str, ...]:
        return (
            period, f"{stats['trades']:,}", f"{stats['wins']:,}", f"{stats['losses']:,}",
            f"{stats['win_percent']:.2f}%", f"{stats['loss_percent']:.2f}%",
            _money(stats["net_pnl"]), _money(stats["average_net_pnl"]), _profit_factor(stats),
            _money(stats["max_closed_trade_drawdown"]), f"{stats['winning_days']:,}",
            f"{stats['losing_days']:,}", f"{stats['stops']:,}",
        )

    values = [cells(month["period"], month) for month in months]
    total = cells("TOTAL", overall)
    widths = [max(len(header), *(len(row[index]) for row in [*values, total]))
              for index, header in enumerate(headers)]

    def line(row: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[index]) if index == 0 else value.rjust(widths[index])
                         for index, value in enumerate(row))

    separator = "  ".join("-" * width for width in widths)
    title = "Re-entry monthly summary" if reentry_only else "Monthly summary"
    heading = f"{title} - {STRATEGY_NAME}" + (" (PARTIAL)" if partial else "")
    lines = [heading]
    if reentry_only:
        lines.append("Only re-entry trades (trade 2); amounts use net P/L after configured fees.")
        lines.append(f"Re-entry attempts: {overall['reentry_attempts']:,}; {overall['reentry_trades']:,} completed, "
                     f"{overall['reentry_skipped']:,} skipped, {overall['reentry_incomplete']:,} incomplete.")
    if overall["errors"] or overall["incomplete"]:
        unit = "attempt" if reentry_only else "candidate"
        lines.append(f"Completed trades only: {overall['errors']} {unit} errors and {overall['incomplete']} incomplete results excluded from performance.")
    lines.extend([
        "PF = winning net P/L / absolute losing net P/L; inf = wins with no losses; -- = undefined.",
        "Max DD = realized drawdown within the period; Win/Loss Days use each day's combined "
        + ("re-entry net P/L." if reentry_only else "net P/L."),
        "Stop Stocks = total stop-loss exits in the period; "
        + ("each stopped re-entry counts." if reentry_only else "each stopped trade counts, including re-entries."),
        line(headers), separator, *(line(row) for row in values), separator, line(total),
    ])
    return "\n".join(lines)


def format_trade_summary(rows: list[TradeResult], *, partial: bool = False) -> str:
    """End-of-run winner/loser statistics, measured per completed net trade."""
    stats = summarize(rows)
    metrics = (
        ("Avg winner", _money(stats["average_winner_net_pnl"])),
        ("Avg loser", _money(stats["average_loser_net_pnl"])),
        ("Biggest winner", _money(stats["biggest_winner_net_pnl"])),
        ("Biggest loser", _money(stats["biggest_loser_net_pnl"])),
        ("Avg trade", _money(stats["average_net_pnl"])),
        ("Profit factor", _profit_factor(stats)),
    )
    lines = [
        f"Trade summary - {STRATEGY_NAME}" + (" (PARTIAL)" if partial else ""),
        f"Completed trades: {stats['trades']:,}; amounts use net P/L after configured commissions and locate fees.",
    ]
    if stats["reentry_attempts"]:
        lines.append(f"First trades: {stats['initial_trades']:,} completed. Re-entry attempts: {stats['reentry_attempts']:,}; "
                     f"{stats['reentry_trades']:,} completed, {stats['reentry_skipped']:,} skipped, {stats['reentry_incomplete']:,} incomplete.")
    if stats["errors"] or stats["incomplete"]:
        lines.append(f"Excluded: {stats['errors']:,} candidate errors and {stats['incomplete']:,} incomplete results.")
    lines.append("Avg trade includes breakeven trades; inf = wins with no losses; -- = undefined.")
    width = max(len(label) for label, _ in metrics)
    lines.extend(f"{label:<{width}}  {value}" for label, value in metrics)
    return "\n".join(lines)


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_reports(config: BacktestConfig, rows: list[TradeResult], *, requested_count: int, interrupted: bool = False, input_content: bytes | None = None, execution: dict | None = None) -> tuple[Path, dict]:
    stamp = datetime.now(EASTERN).strftime("%Y%m%dT%H%M%S_%f")
    directory = config.output_dir / f"{STRATEGY_ID}_{stamp}_{uuid4().hex[:6]}"
    directory.mkdir(parents=True, exist_ok=False)
    def report_path(filename: str) -> Path:
        return directory / f"{STRATEGY_ID}_{filename}"

    input_content = config.input_file.read_bytes() if input_content is None else input_content
    report_path("input.snapshot.txt").write_bytes(input_content)
    all_rows = [asdict(row) for row in rows]
    columns = [field.name for field in fields(TradeResult)]
    write_csv(report_path("candidates.csv"), all_rows, columns)
    write_csv(report_path("trades.csv"), [row for row in all_rows if row["status"] == "trade"], columns)
    details_header = (
        f"{STRATEGY_NAME} backtest - all times Eastern (America/New_York)\n"
        f"Data source: {'Alpaca SIP' if config.data.provider == 'alpaca' else 'Massive'}\n"
        "Times identify one-minute bars, not exact trade ticks. The setup-high wait reference is shown separately.\n\n"
    )
    candidate_indices = {key: index for index, key in enumerate(
        dict.fromkeys((row.date, row.symbol) for row in rows if row.trade_number == 1), 1)}
    details = "\n\n".join(
        format_candidate(row, config.strategy, index=candidate_indices.get((row.date, row.symbol)), total=requested_count)
        for row in rows
    )
    monthly_table = format_monthly_summary(rows, partial=interrupted)
    gap_table = format_gap_summary(rows, config.strategy, partial=interrupted)
    trade_summary = format_trade_summary(rows, partial=interrupted)
    report_path("gap_summary.txt").write_text(gap_table + "\n", encoding="utf-8")
    sections = [details, gap_table, monthly_table]
    if config.strategy.reentry.enabled:
        sections.append(format_monthly_summary(rows, partial=interrupted, reentry_only=True))
        write_csv(report_path("reentry_monthly.csv"), reentry_monthly_summaries(rows),
                  ["strategy_id", "strategy_name", "period", *summarize([])])
    sections.append(trade_summary)
    report_path("trade_details.txt").write_text(details_header + "\n\n".join(sections) + "\n", encoding="utf-8")
    for name, length in (("daily", 10), ("monthly", 7)):
        summaries = _period_summaries(rows, length)
        write_csv(report_path(f"{name}.csv"), summaries, ["strategy_id", "strategy_name", "period", *summarize([])])
    summary = {
        "strategy_id": STRATEGY_ID, "strategy_name": STRATEGY_NAME,
        "version": __version__, "timezone": "America/New_York",
        "data_provider": config.data.provider,
        "data_feed": "sip" if config.data.provider == "alpaca" else "Massive aggregates",
        "created_at": datetime.now(EASTERN).isoformat(),
        "requested_candidates": requested_count, "processed_candidates": len(candidate_indices),
        "unprocessed_candidates": requested_count - len(candidate_indices), "interrupted": interrupted,
        "input_sha256": hashlib.sha256(input_content).hexdigest(),
        "execution": execution or {},
        "statistics": summarize(rows),
        "monthly_statistics": monthly_summaries(rows),
        "gap_not_traded_reasons": gap_not_traded_reasons(rows),
        "status_reasons": dict(sorted(Counter(row.reason or row.exit_reason for row in rows).items())),
        "assumptions": {
            "price_data": (
                "Raw Alpaca SIP one-minute bars; exact previous-session regular close normalized to the trade date's share basis using matched raw/split-adjusted bars. Later splits cancel from the adjustment ratios."
                if config.data.provider == "alpaca" else
                "Unadjusted Massive one-minute aggregates; prior regular close normalized for trade-date splits."
            ),
            "time_precision": "Intrabar fills carry the bar-start timestamp, not an observed execution timestamp.",
            "high_time_reference": config.strategy.high_time_reference,
            "repeated_high_policy": config.strategy.repeated_high_policy,
            "late_gap_enabled": config.strategy.late_gap_enabled,
            "late_gap_window_minutes": config.strategy.late_gap_window_minutes,
            "setup_high": "Original early-window qualifiers retain their early-window high. When late gaps are enabled, new qualifiers after the early window use a fixed late_gap_window_minutes window from the first qualifying minute and freeze its high. Entry waits for both window completion and the high-based delay. The early_high fields retain the setup high for both paths.",
            "intrabar_policy": config.strategy.intrabar_policy,
            "liquidity": "Full-size fills assumed; quotes, queue, borrow availability and volume limits are not modeled.",
            "costs": "Configured locate fee charged once on the first filled trade; re-entry reuses those shares with no second locate fee. Per-side commissions apply to both trades. Actual locate charges for unfilled orders are not modeled.",
            "pnl_scope": "Only completed trades, including enabled re-entries; unresolved positions excluded and counted as incomplete. Independent stock/day setups without portfolio limits.",
            "drawdown": "Realized closed-trade equity in exit-time order, grouping equal minute timestamps, starting at zero; no intratrade mark-to-market.",
            "day_outcomes": "Winning and losing days use combined net P/L of completed trades on each Eastern trading date; breakeven days count separately.",
            "gap_funnel": "Counts only trade_number=1: confirmed triggers have first_gap_time; traded triggers have an entry_price, including unresolved positions. Re-entries do not duplicate stock/date setup counts.",
            "stops": "Total completed trades with a stop_loss exit in the period; each stopped initial or re-entry trade counts separately.",
            "reentry": "At most one re-entry after a completed first stop-loss, using the original setup high. Earliest backtest activation is the next minute after the stop bar because intrabar event timing is unknown. Live entries may activate as soon as the stop is fully filled and confirmed flat. Existing borrow reuse is assumed in the backtest; live checks DAS availability and never purchases new locates for re-entry.",
        },
    }
    if config.strategy.reentry.enabled:
        summary["reentry_statistics"] = summarize([row for row in rows if row.trade_number == 2])
        summary["reentry_monthly_statistics"] = reentry_monthly_summaries(rows)
    for filename, content in (("summary.json", summary), ("config.resolved.json", config.snapshot())):
        report_path(filename).write_text(json.dumps(content, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return directory, summary
