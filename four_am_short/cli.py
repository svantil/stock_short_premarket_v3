"""Command-line backtest orchestration. No broker connections or execution."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path

from . import STRATEGY_ID, STRATEGY_NAME
from .config import iso_date, load_config, read_api_key, symbol
from .inputs import read_candidates
from .massive import MassiveClient
from .models import EASTERN, DataError, TradeResult
from .reports import format_gap_summary, format_monthly_summary, format_trade_summary, summarize, write_reports
from .strategy import simulate_trades
from .trade_details import format_candidate


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=f"{STRATEGY_NAME} backtest (all times Eastern).")
    result.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / f"backtest_{STRATEGY_ID}.json")
    result.add_argument("--validate-only", action="store_true", help="Validate JSON and input without network requests")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Use cached Massive responses only; no API key required")
    mode.add_argument("--refresh-cache", action="store_true", help="Fetch again and replace cached market data")
    result.add_argument("--from-date", help="Inclusive date filter, YYYY-MM-DD")
    result.add_argument("--to-date", help="Inclusive date filter, YYYY-MM-DD")
    result.add_argument("--symbols", nargs="+", help="Only these tickers")
    result.add_argument("--max-candidates", type=int, help="Limit sorted date/symbol pairs for a small trial")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.from_date:
            config = replace(config, from_date=iso_date(args.from_date, "from-date"))
        if args.to_date:
            config = replace(config, to_date=iso_date(args.to_date, "to-date"))
        if config.from_date and config.to_date and config.from_date > config.to_date:
            raise DataError("from-date must be on or before to-date")
        if args.symbols:
            config = replace(config, symbols=tuple(dict.fromkeys(symbol(s) for s in args.symbols)))
        input_content = config.input_file.read_bytes()
        candidates = read_candidates(config, content=input_content)
        if args.max_candidates is not None:
            if args.max_candidates < 1:
                raise DataError("max-candidates must be at least 1")
            candidates = candidates[:args.max_candidates]
        print(f"Strategy: {STRATEGY_NAME}\nInput: {config.input_file}\nCandidates: {len(candidates)}; shares: {config.strategy.shares}; timezone: America/New_York", flush=True)
        if args.validate_only:
            print("Configuration and input are valid. No market-data requests made.")
            return 0
        key = None if args.offline else read_api_key(config.data)
        if not args.offline and not key:
            raise DataError(f"Set {config.data.api_key_env} in your environment or {config.data.env_file}")
        client = MassiveClient(config.data, key, offline=args.offline, refresh_cache=args.refresh_cache)
        print("Trade times identify one-minute bars in Eastern time; high-bar and wait-reference times are shown separately.", flush=True)
        rows: list[TradeResult] = []
        interrupted = False
        try:
            for index, candidate in enumerate(candidates, 1):
                day, ticker = candidate.trading_date, candidate.symbol
                # Do not cache or report a still-evolving backtest window as a completed result.
                exit_times = [config.strategy.time_exit]
                if config.strategy.reentry.enabled:
                    exit_times.append(config.strategy.reentry.time_exit)
                ready_at = datetime.combine(day, time.fromisoformat(max(exit_times)), EASTERN) + timedelta(minutes=1)
                if datetime.now(EASTERN) < ready_at:
                    candidate_rows = [TradeResult(day.isoformat(), ticker, "incomplete", "session_not_finished")]
                else:
                    try:
                        previous = client.previous_close(ticker, day)
                        bars = client.minute_bars(ticker, day)
                        candidate_rows = simulate_trades(day, ticker, bars, previous, config.strategy)
                    except DataError as exc:
                        candidate_rows = [TradeResult(day.isoformat(), ticker, "error", "market_data_error", notes=str(exc))]
                rows.extend(candidate_rows)
                for row in candidate_rows:
                    print(format_candidate(row, config.strategy, index=index, total=len(candidates)), flush=True)
        except KeyboardInterrupt:
            interrupted = True
            print("\nInterrupted; saving processed results.", file=sys.stderr)
        directory, summary = write_reports(config, rows, requested_count=len(candidates), interrupted=interrupted, input_content=input_content, execution={"offline": args.offline, "refresh_cache": args.refresh_cache, "max_candidates": args.max_candidates})
        stats = summary["statistics"]
        print(f"\nReports: {directory}\nTrades: {stats['trades']}; skipped: {stats['skipped']}; errors: {stats['errors']}; incomplete: {stats['incomplete']}")
        print(f"Closed-trade net P/L: ${stats['net_pnl']:,.2f}; win rate: {stats['win_percent']:.2f}%")
        print("\n" + format_gap_summary(rows, config.strategy, partial=interrupted))
        print("\n" + format_monthly_summary(rows, partial=interrupted))
        if config.strategy.reentry.enabled:
            print("\n" + format_monthly_summary(rows, partial=interrupted, reentry_only=True))
        print("\n" + format_trade_summary(rows, partial=interrupted))
        today = datetime.now(EASTERN).date().isoformat()
        today_rows = [row for row in rows if row.date == today]
        today_stats = summarize(today_rows)
        today_pnl = f"${today_stats['net_pnl']:,.2f}" if today_rows else "--"
        partial = " (PARTIAL)" if interrupted or today_stats["errors"] or today_stats["incomplete"] else ""
        print(f"\n{today} | Net P/L: {today_pnl}{partial}")
        if interrupted:
            return 130
        return 3 if stats["errors"] or stats["incomplete"] else 0
    except (DataError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
