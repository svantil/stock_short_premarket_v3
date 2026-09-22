#!/usr/bin/env python3
"""Compare a finite re-entry grid with fixed first trades and chronological samples.

Uses the backtest's Massive data and fill model. Never connects to a broker or
changes the shared/live trading configuration. Run with --offline for cache only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

from four_am_short.config import ReentryConfig, clock, integer, iso_date, load_config, number, object_section, read_api_key
from four_am_short.inputs import read_candidates
from four_am_short.massive import MassiveClient
from four_am_short.models import DataError, EASTERN, TradeResult
from four_am_short.reports import monthly_summaries, summarize
from four_am_short.reentry_sweep_sim import prepare_case, simulate_prepared
from four_am_short.strategy import simulate, simulate_trades

PROJECT = Path(__file__).resolve().parent
PARAMETERS = ("entry_above_high_percent", "stop_loss_percent", "profit_target_percent", "entry_deadline", "time_exit")
STATISTICS = ("trades", "wins", "losses", "win_percent", "net_pnl", "average_net_pnl", "profit_factor",
              "max_closed_trade_drawdown", "average_winner_net_pnl", "average_loser_net_pnl",
              "biggest_winner_net_pnl", "biggest_loser_net_pnl", "winning_days", "losing_days",
              "stops", "targets", "time_exits", "skipped", "incomplete", "errors")


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False, default=str) + "\n")


def csv_rows(path, rows, columns=None):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def load_grid(path):
    raw = object_section(json.loads(path.read_text()), {
        "backtest_config", "output_dir", "training_end_date", "minimum_training_trades",
        "minimum_full_sample_trades", "grid"}, "sweep")
    required = {"backtest_config", "training_end_date", "grid"}
    if missing := required - raw.keys():
        raise DataError(f"Missing sweep settings: {', '.join(sorted(missing))}")
    for key in ("backtest_config", "output_dir"):
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise DataError(f"{key} must be a nonempty path string")
    grid = object_section(raw.get("grid"), set(PARAMETERS), "grid")
    if set(grid) != set(PARAMETERS):
        raise DataError("Grid must supply every re-entry parameter")
    for key, values in grid.items():
        if not isinstance(values, list) or not values:
            raise DataError(f"grid.{key} must be a nonempty array")
        for value in values:
            if key in {"entry_deadline", "time_exit"}:
                clock(value, key)
            else:
                number(value, key, strict=key != "entry_above_high_percent")
                if key == "profit_target_percent" and value >= 100:
                    raise DataError("Profit targets must be less than 100%")
        grid[key] = list(dict.fromkeys(values))
    raw["grid"] = grid
    iso_date(raw["training_end_date"], "training_end_date")
    integer(raw.get("minimum_training_trades", 20), "minimum_training_trades", 1)
    integer(raw.get("minimum_full_sample_trades", 30), "minimum_full_sample_trades", 1)
    return raw


def combinations(grid, early_end):
    return [dict(zip(PARAMETERS, values)) for values in itertools.product(*(grid[key] for key in PARAMETERS))
            if early_end < values[3] <= values[4]]


def evaluate(cases, strategy, parameters, split):
    config = replace(strategy, reentry=ReentryConfig(enabled=True, **parameters))
    rows = [simulate_prepared(case, config) for case in cases]
    train = [row for row in rows if row.date <= split]
    later = [row for row in rows if row.date > split]
    result = dict(parameters)
    for prefix, sample in (("all", rows), ("train", train), ("validation", later)):
        stats = summarize(sample)
        result.update({f"{prefix}_{key}": stats[key] for key in STATISTICS})
    return result, rows


def ranking_key(row, prefix):
    return (-row[f"{prefix}_net_pnl"], row[f"{prefix}_max_closed_trade_drawdown"],
            -row[f"{prefix}_trades"], *(row[key] for key in PARAMETERS))


def load_cases(config, offline, ready_clock):
    key = None if offline else read_api_key(config.data)
    if not offline and not key:
        raise DataError(f"Set {config.data.api_key_env} or use --offline")
    client = MassiveClient(config.data, key, offline=offline)
    content = config.input_file.read_bytes()
    candidates = read_candidates(config, content=content)
    cases, initial_rows, source_cases = [], [], []
    started = time.monotonic()
    for index, candidate in enumerate(candidates, 1):
        day, symbol = candidate.trading_date, candidate.symbol
        ready = datetime.fromisoformat(f"{day}T{ready_clock}:00").replace(tzinfo=EASTERN) + timedelta(minutes=1)
        if datetime.now(EASTERN) < ready:
            row = TradeResult(str(day), symbol, "incomplete", "session_not_finished")
        else:
            try:
                previous = client.previous_close(symbol, day)
                bars = client.minute_bars(symbol, day)
                row = simulate(day, symbol, bars, previous, config.strategy)
                if row.status == "trade" and row.exit_reason == "stop_loss":
                    cases.append(prepare_case(row, bars))
                    source_cases.append((day, symbol, bars, previous))
            except DataError as exc:
                row = TradeResult(str(day), symbol, "error", "market_data_error", notes=str(exc))
        initial_rows.append(row)
        if index % 200 == 0 or index == len(candidates):
            print(f"Loaded {index}/{len(candidates)} candidates; {len(cases)} initial stop-outs; {time.monotonic()-started:.1f}s", flush=True)
    return cases, initial_rows, source_cases, content


def money(value):
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def factor(value, wins):
    return f"{value:.2f}" if value is not None else "inf" if wins else "--"


def markdown_table(items):
    lines = ["| Configuration | Entry above high | Stop | Target | Entry cutoff | Time exit | Trades | Net P/L | PF | Max DD | Training P/L | Later-period P/L |",
             "|---|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|"]
    for label, row in items:
        lines.append(f"| {label} | {row['entry_above_high_percent']:g}% | {row['stop_loss_percent']:g}% | {row['profit_target_percent']:g}% | "
                     f"{row['entry_deadline']} | {row['time_exit']} | {row['all_trades']} | {money(row['all_net_pnl'])} | "
                     f"{factor(row['all_profit_factor'], row['all_wins'])} | {money(row['all_max_closed_trade_drawdown'])} | "
                     f"{money(row['train_net_pnl'])} | {money(row['validation_net_pnl'])} |")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT / "sweep_4am_short_reentry.json")
    parser.add_argument("--offline", action="store_true", help="Use cached Massive data only")
    parser.add_argument("--output-dir", type=Path, help="Explicit directory; existing files are not overwritten")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    spec_path = args.config.expanduser().resolve()
    spec = load_grid(spec_path)
    config_path = (spec_path.parent / spec["backtest_config"]).resolve()
    config = load_config(config_path)
    grid = combinations(spec["grid"], config.strategy.early_end)
    if not grid:
        raise DataError("No valid grid combinations")
    print(f"Re-entry sweep: {len(grid):,} combinations; {config.strategy.shares} shares; initial strategy fixed", flush=True)
    if args.validate_only:
        print("Sweep configuration valid. No data requests made.")
        return 0
    folder = args.output_dir.resolve() if args.output_dir else (spec_path.parent / spec.get("output_dir", "outcome/reentry_sweep") / datetime.now(EASTERN).strftime("%Y%m%dT%H%M%S")).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    reserved = ("sweep_spec.json", "backtest_config.snapshot.json", "input.snapshot.txt", "initial_statistics.json",
                "initial_candidates.csv", "data_exclusions.json", "all_combinations.csv", "sweep_manifest.json")
    if any((folder / name).exists() for name in reserved):
        raise DataError("Sweep results already exist in this output directory")
    original_bytes = config_path.read_bytes()
    dump(folder / "sweep_spec.json", spec)
    dump(folder / "backtest_config.snapshot.json", config.snapshot())
    split = spec["training_end_date"]
    ready_clock = max(config.strategy.time_exit, config.strategy.reentry.time_exit, *spec["grid"]["time_exit"])
    cases, initial_rows, source_cases, input_content = load_cases(config, args.offline, ready_clock)
    (folder / "input.snapshot.txt").write_bytes(input_content)
    dump(folder / "initial_statistics.json", summarize(initial_rows))
    csv_rows(folder / "initial_candidates.csv", [asdict(row) for row in initial_rows])
    errors = [asdict(row) for row in initial_rows if row.status in {"error", "incomplete"}]
    dump(folder / "data_exclusions.json", errors)
    if not cases:
        raise DataError("No completed primary stop-outs available; see data_exclusions.json")
    baseline_parameters = {key: getattr(config.strategy.reentry, key) for key in PARAMETERS}
    baseline, baseline_rows = evaluate(cases, config.strategy, baseline_parameters, split)
    # Cross-check every baseline result against the actual backtest, before the grid.
    for produced, source in zip(baseline_rows, source_cases):
        day, symbol, bars, previous = source
        reference = simulate_trades(day, symbol, bars, previous, replace(config.strategy, reentry=replace(config.strategy.reentry, enabled=True)))[1]
        if asdict(produced) != asdict(reference):
            raise DataError(f"Prepared evaluator differs from original backtest for {day}/{symbol}")
    print(f"Verified all {len(cases)} baseline re-entries against the original simulator", flush=True)
    results = []
    started = time.monotonic()
    for index, parameters in enumerate(grid, 1):
        result, _ = evaluate(cases, config.strategy, parameters, split)
        result["baseline"] = parameters == baseline_parameters
        results.append(result)
        if index % 100 == 0 or index == len(grid):
            elapsed = time.monotonic() - started
            print(f"Evaluated {index:,}/{len(grid):,}; elapsed {elapsed:.1f}s; estimated remaining {elapsed/index*(len(grid)-index):.1f}s", flush=True)
    min_train = spec.get("minimum_training_trades", 20)
    min_all = spec.get("minimum_full_sample_trades", 30)
    for row in results:
        row["training_selection_eligible"] = row["train_trades"] >= min_train and row["train_incomplete"] == 0 and row["train_errors"] == 0
        row["full_sample_selection_eligible"] = row["all_trades"] >= min_all and row["all_incomplete"] == 0 and row["all_errors"] == 0
    train_ranked = sorted((row for row in results if row["training_selection_eligible"]), key=lambda row: ranking_key(row, "train"))
    full_ranked = sorted((row for row in results if row["full_sample_selection_eligible"]), key=lambda row: ranking_key(row, "all"))
    for label, ranked in (("train_rank", train_ranked), ("full_sample_rank", full_ranked)):
        ranks = {tuple(row[key] for key in PARAMETERS): rank for rank, row in enumerate(ranked, 1)}
        for row in results:
            row[label] = ranks.get(tuple(row[key] for key in PARAMETERS))
    ranked_all = sorted(results, key=lambda row: ranking_key(row, "all"))
    csv_rows(folder / "all_combinations.csv", ranked_all)
    if train_ranked:
        csv_rows(folder / "top_training_settings.csv", train_ranked[:25])
    if full_ranked:
        csv_rows(folder / "top_full_sample_settings.csv", full_ranked[:25])
    comparisons = [("Current settings", baseline)]
    if train_ranked:
        comparisons.append((f"Selected using dates through {split}", train_ranked[0]))
    if full_ranked:
        comparisons.append(("Best full-sample fit (hindsight)", full_ranked[0]))
    comparison_stats = {}
    for label, row in comparisons:
        slug = "baseline" if label == "Current settings" else "training_winner" if label.startswith("Selected") else "full_sample_winner"
        params = {key: row[key] for key in PARAMETERS}
        _, detail = evaluate(cases, config.strategy, params, split)
        csv_rows(folder / f"{slug}_trades.csv", [asdict(item) for item in detail])
        months = monthly_summaries(detail)
        csv_rows(folder / f"{slug}_monthly.csv", months)
        selected_config = config.snapshot()
        selected_config["strategy"]["reentry"] = {"enabled": True, **params}
        dump(folder / f"{slug}_backtest.json", selected_config)
        comparison_stats[slug] = {"parameters": params, "reentry": summarize(detail),
                                  "combined": summarize(initial_rows + detail), "monthly": months}
    # Show entry-offset sensitivity with every other current setting held fixed.
    sensitivity = [row for row in results if all(row[key] == baseline_parameters[key] for key in PARAMETERS[1:])]
    sensitivity.sort(key=lambda row: row["entry_above_high_percent"])
    if sensitivity:
        csv_rows(folder / "entry_offset_sensitivity.csv", sensitivity)
    manifest = {
        "created_at": datetime.now(EASTERN).isoformat(), "grid_combinations": len(results),
        "evaluations": len(results) * len(cases), "offline": args.offline,
        "input_file": str(config.input_file), "input_sha256": hashlib.sha256(input_content).hexdigest(),
        "config_path": str(config_path), "config_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "configuration_unchanged": config_path.read_bytes() == original_bytes,
        "date_range": [min(row.date for row in initial_rows), max(row.date for row in initial_rows)],
        "training_end_date": split, "stop_out_cases": len(cases),
        "training_stop_out_cases": sum(case.initial.date <= split for case in cases),
        "validation_stop_out_cases": sum(case.initial.date > split for case in cases),
        "initial_statistics": summarize(initial_rows), "data_exclusion_count": len(errors),
        "minimum_training_trades": min_train, "minimum_full_sample_trades": min_all,
        "training_eligible_combinations": len(train_ranked), "full_sample_eligible_combinations": len(full_ranked),
        "selection": "Highest training net P/L with minimum training fills and no unresolved training trades; ties lower training drawdown, then more fills, then parameter order. Later-period outcomes are not used for selection.",
        "comparisons": comparison_stats,
        "limitations": ["Exploratory historical comparison; the user has already examined this history, so later dates are not a pristine untouched holdout.",
                        "Next-minute re-entry after the stop bar, one retry, original early high. Marketable sell limits can improve above their configured entry level.",
                        "Full-share fills and reusable locates assumed. Configured fees/slippage only; no spread, queues, partial fills, or liquidity constraints.",
                        "Missing first-trade data excluded identically across all configurations. Incomplete re-entries are reported and excluded from performance, not counted as zero-P/L trades.",
                        "No re-entry contributes $0 incremental P/L. All ranked P/L is additional to the fixed initial strategy; the full-sample maximum is a hindsight fit."],
    }
    dump(folder / "sweep_manifest.json", manifest)
    report = ["# 4am short — re-entry parameter comparison", "",
              f"{len(results):,} parameter combinations, {config.strategy.shares:,} shares per trade. First-trade rules held fixed.",
              f"Input dates: {manifest['date_range'][0]} through {manifest['date_range'][1]}. {len(cases)} completed first-trade stop-outs: "
              f"{manifest['training_stop_out_cases']} through {split}, {manifest['validation_stop_out_cases']} afterward.", "",
              "All P/L below is **re-entry only**, after configured costs. Not re-entering adds $0.", "",
              markdown_table(comparisons), "", "## Selection and data coverage", "", manifest["selection"], "",
              f"Minimum completed fills: {min_train} for training selection; {min_all} for the full-sample comparison. "
              f"{sum(row.status == 'error' for row in initial_rows)} initial data errors and "
              f"{sum(row.status == 'incomplete' for row in initial_rows)} unresolved/incomplete first trades. See data_exclusions.json.", "",
              "## Entry-offset sensitivity", "", markdown_table([("Other current settings fixed", row) for row in sensitivity]), "",
              "## Interpretation", "", *[f"- {text}" for text in manifest["limitations"]], "",
              "The active backtest/live settings were not changed. Selected configuration snapshots are saved here for review.", "",
              "Every combination is in all_combinations.csv; selected per-trade and monthly reports are in this directory.", ""]
    (folder / "sweep_report.md").write_text("\n".join(report))
    print(markdown_table(comparisons), flush=True)
    print(f"Results: {folder}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DataError, OSError, ValueError) as error:
        raise SystemExit(f"Sweep failed: {error}") from error
