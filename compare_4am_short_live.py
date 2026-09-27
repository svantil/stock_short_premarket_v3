#!/usr/bin/env python3
"""Compare saved live fills, backtest results and optional historical SIP bars.

Only --fetch-sip accesses the network, using GET-only market-data/calendar APIs.
No broker client is constructed and no trading service is started.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from four_am_short.config import iso_date, load_config, symbol
from four_am_short.live.config import PROJECT, load_live_settings
from four_am_short.live.data import AlpacaData, AlpacaDataError
from four_am_short.models import DataError, EASTERN
from four_am_short.parity import (code_fingerprint, compare, context_for_symbol, encode_bar,
                                  render, report_for_day, request_window, simulate_dataset)


async def fetch_sip(settings, day: date, contexts: dict) -> dict:
    if not settings.data.api_key or not settings.data.secret_key:
        raise DataError("Alpaca data credentials are required for --fetch-sip")
    if not contexts:
        raise DataError("No symbols have a saved previous close to simulate")
    start, end = request_window(day, contexts)
    data = AlpacaData(settings.data)
    try:
        if any(not item.get("previous_close_date") for item in contexts.values()):
            # Fetch dates only; never substitute today's split-adjusted daily
            # prices for the actual prior close saved by a past live session.
            calendar = await data._get(data._trading_url + "/v2/calendar", {
                "start": str(day - timedelta(days=31)), "end": str(day)})
            sessions = {date.fromisoformat(row["date"]) for row in calendar}
            if day not in sessions:
                raise DataError("Requested date is not an exchange session")
            previous = max(d for d in sessions if d < day)
            for item in contexts.values():
                if not item.get("previous_close_date"):
                    item.update(previous_close_date=str(previous), previous_close_date_source="Alpaca exchange calendar")
        history = await data.backfill(list(contexts), day, start, end)
    finally:
        await data.close()
    return {"schema": 1, "feed": "alpaca_sip", "date": str(day), "retrieved_at": datetime.now(EASTERN).isoformat(),
            "simulator_sha256": code_fingerprint(),
            "kind": "retrospective_rest", "start": start, "end_exclusive": end,
            "symbols": {ticker: {**context, "bars": [encode_bar(bar) for bar in history.get(ticker, [])]}
                        for ticker, context in contexts.items()}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=str(datetime.now(EASTERN).date()), help="Eastern session date (default today)")
    parser.add_argument("--live-config", type=Path, default=PROJECT / "live_4am_short.json")
    parser.add_argument("--live-state", type=Path, help="Override saved live state path")
    parser.add_argument("--backtest-dir", type=Path, help="Specific backtest report directory; otherwise latest containing this date")
    parser.add_argument("--symbols", nargs="+", help="Limit symbols; otherwise union of saved live and backtest symbols")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fetch-sip", action="store_true", help="GET historical Alpaca SIP bars and save them for offline comparison")
    mode.add_argument("--sip-data", type=Path, help="Use previously saved sip_data.json without market-data requests")
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "outcome/4am_short_comparison")
    args = parser.parse_args(argv)
    try:
        day = iso_date(args.date)
        settings = load_live_settings(args.live_config)
        state_path = args.live_state or settings.state_path
        state = json.loads(state_path.read_text())
        day_state = state.get("days", {}).get(str(day))
        if not isinstance(day_state, dict):
            raise DataError(f"No saved live session for {day}")
        config = load_config(settings.strategy_config_path)
        directory, rows, report_strategy = report_for_day(config.output_dir, day, args.backtest_dir)
        tickers = sorted({symbol(t) for t in args.symbols}) if args.symbols else sorted(
            set(day_state.get("candidates", {})) | {t["symbol"] for t in day_state.get("trades", {}).values()} | {r["symbol"] for r in rows})
        if not tickers:
            raise DataError("No saved symbols to compare")
        contexts = {ticker: context_for_symbol(day_state, ticker, settings.strategy, rows) for ticker in tickers}
        dataset = None
        if args.fetch_sip:
            usable = {ticker: dict(item) for ticker, item in contexts.items() if item["previous_close"] is not None}
            dataset = asyncio.run(fetch_sip(settings, day, usable))
        elif args.sip_data:
            dataset = json.loads(args.sip_data.read_text())
        dataset_requested = args.fetch_sip or args.sip_data is not None
        simulations, errors = simulate_dataset(dataset, day, tickers) if dataset_requested else ({}, {})
        if dataset_requested:
            # Preserve the configuration/prior close used by this exact dataset
            # when a user repeats the analysis after changing live settings.
            for ticker in tickers:
                if isinstance(dataset["symbols"].get(ticker), dict):
                    contexts[ticker] = {key: value for key, value in dataset["symbols"][ticker].items() if key != "bars"}
        result = compare(day, tickers, day_state, rows, report_strategy, contexts, simulations, errors)
        result.update(schema=1, generated_at=datetime.now(EASTERN).isoformat(), simulator_sha256=code_fingerprint(),
                      sources={"live_state": str(state_path), "backtest_directory": str(directory) if directory else None,
                               "sip_dataset": str(args.sip_data) if args.sip_data else "fetched" if args.fetch_sip else None})
        if not dataset_requested:
            result["notes"] = ["SIP simulation not requested; use --fetch-sip or --sip-data to compare the same market-data feed."]
        elif dataset.get("simulator_sha256") != result["simulator_sha256"]:
            result["notes"] = ["SIP dataset simulator fingerprint is missing or differs from the current simulator; saved inputs do not guarantee identical results across code changes."]
        target = args.output_dir / f"{day}_{datetime.now(EASTERN):%H%M%S}_{uuid4().hex[:6]}"
        target.mkdir(parents=True, exist_ok=False)
        (target / "comparison.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        if dataset_requested:
            (target / "sip_data.json").write_text(json.dumps(dataset, indent=2, allow_nan=False) + "\n")
        text = render(result)
        (target / "comparison.md").write_text(text)
        print(text)
        print(f"\nSaved comparison: {target}")
        return 3 if errors else 0
    except (DataError, AlpacaDataError, OSError, ValueError, KeyError, TypeError) as exc:
        # Do not print settings, HTTP headers, broker details or credentials.
        print(f"Comparison failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
