#!/usr/bin/env python3
"""Audit stopped-out first trades and later highs using the configured provider's offline bars.

The source report must match the supplied configuration and current input hash.
No credentials are read and no network requests or trading operations are made.
Future highs are descriptive hindsight, never used as an entry signal.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

from four_am_short.config import load_config
from four_am_short.data_sources import create_client, provider_label
from four_am_short.massive import API_BASE
from four_am_short.models import EASTERN, DataError, PreviousClose
from four_am_short.strategy import simulate


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    left = math.floor(index)
    right = math.ceil(index)
    return ordered[left] + (ordered[right] - ordered[left]) * (index - left)


def describe(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    trim = math.floor(len(ordered) * 0.1)
    trimmed = ordered[trim:len(ordered) - trim] if trim else ordered
    return {
        "count": len(values), "mean": statistics.mean(values),
        "median": statistics.median(values), "p25": percentile(values, 0.25),
        "p75": percentile(values, 0.75), "p90": percentile(values, 0.9),
        "minimum": min(values), "maximum": max(values),
        "ten_percent_each_tail_trimmed_mean": statistics.mean(trimmed),
        "mean_excluding_single_largest": statistics.mean(ordered[:-1]) if len(ordered) > 1 else None,
    }


def at(day: date, clock: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=EASTERN)


def offset(price: float, high: float) -> float:
    return (price / high - 1) * 100


def cache_path(config, symbol: str, day: date, *, client=None) -> Path:
    if config.data.provider == "alpaca":
        if client is None:
            client = create_client(config.data, offline=True)
        return client.minute_cache_path(symbol, day)
    identity = {
        "base_url": API_BASE,
        "path": f"/v2/aggs/ticker/{quote(symbol, safe='')}/range/1/minute/{day}/{day}",
        "params": {"adjusted": "false", "sort": "asc", "limit": "50000"},
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return config.data.cache_dir / "massive-v1" / f"{digest}.json"


def inspect_stop(source: dict, config, client, cutoffs: list[str]) -> dict:
    day = date.fromisoformat(source["date"])
    high = float(source["early_high"])
    stopped = datetime.fromisoformat(source["exit_time"]).astimezone(EASTERN)
    activation = stopped + timedelta(minutes=1)
    cached = cache_path(config, source["symbol"], day, client=client)
    row = {
        "date": source["date"], "symbol": source["symbol"],
        "early_high": high, "early_high_bar_time": source["early_high_bar_time"],
        "initial_entry_price": float(source["entry_price"]),
        "initial_entry_time": source["entry_time"],
        "initial_stop_fill": float(source["exit_price"]), "initial_stop_bar_time": stopped.isoformat(),
        "initial_stop_fill_above_high_percent": offset(float(source["exit_price"]), high),
        "reentry_activation": activation.isoformat(),
        "cache_path": str(cached), "cache_sha256": sha256(cached) if cached.exists() else "",
        "analysis_status": "ok", "analysis_error": "",
        "stop_bar_high": None, "stop_bar_high_above_high_percent": None,
        "next_available_bar_time": "", "next_available_open": None,
        "next_open_above_high_percent": None, "minutes_from_activation_to_next_bar": None,
    }
    for cutoff in cutoffs:
        prefix = cutoff.replace(":", "")
        row.update({
            f"{prefix}_activation_before_cutoff": activation < at(day, cutoff),
            f"{prefix}_next_open_eligible": False,
            f"{prefix}_observed_bars": 0, f"{prefix}_potential_minutes": max(0, int((at(day, cutoff) - activation).total_seconds() / 60)),
            f"{prefix}_unobserved_minutes": None, f"{prefix}_last_bar_time": "",
            f"{prefix}_maximum_high": None, f"{prefix}_maximum_high_bar_time": "",
            f"{prefix}_maximum_above_early_high_percent": None,
            f"{prefix}_exact_cutoff_bar_present": False,
        })
    try:
        bars = client.minute_bars(source["symbol"], day)
        previous = PreviousClose(date.fromisoformat(source["previous_close_date"]), float(source["previous_close"]))
        replay = simulate(day, source["symbol"], bars, previous, config.strategy)
        if replay.status != "trade" or replay.exit_reason != "stop_loss" or replay.exit_time != source["exit_time"]:
            raise DataError("Initial stop no longer reproduces from cached bars and current configuration")
        for field in ("early_high", "entry_price", "exit_price"):
            if not math.isclose(getattr(replay, field), float(source[field]), rel_tol=1e-10, abs_tol=1e-10):
                raise DataError(f"Replayed initial {field} differs from source report")
        stop_bar = next((bar for bar in bars if bar.timestamp == stopped), None)
        if stop_bar:
            row["stop_bar_high"] = stop_bar.high
            row["stop_bar_high_above_high_percent"] = offset(stop_bar.high, high)
        later = [bar for bar in bars if bar.timestamp >= activation]
        if later:
            first = later[0]
            row.update({
                "next_available_bar_time": first.timestamp.isoformat(), "next_available_open": first.open,
                "next_open_above_high_percent": offset(first.open, high),
                "minutes_from_activation_to_next_bar": int((first.timestamp - activation).total_seconds() / 60),
            })
        for cutoff in cutoffs:
            prefix = cutoff.replace(":", "")
            boundary = at(day, cutoff)
            eligible = [bar for bar in later if bar.timestamp < boundary]
            row[f"{prefix}_next_open_eligible"] = bool(eligible)
            row[f"{prefix}_observed_bars"] = len(eligible)
            row[f"{prefix}_unobserved_minutes"] = row[f"{prefix}_potential_minutes"] - len(eligible)
            row[f"{prefix}_exact_cutoff_bar_present"] = any(bar.timestamp == boundary for bar in bars)
            if eligible:
                maximum = max(eligible, key=lambda bar: bar.high)
                row.update({
                    f"{prefix}_last_bar_time": eligible[-1].timestamp.isoformat(),
                    f"{prefix}_maximum_high": maximum.high,
                    f"{prefix}_maximum_high_bar_time": maximum.timestamp.isoformat(),
                    f"{prefix}_maximum_above_early_high_percent": offset(maximum.high, high),
                })
    except (DataError, OSError, ValueError) as exc:
        row["analysis_status"] = "unavailable"
        row["analysis_error"] = str(exc)
    return row


def summarize(rows: list[dict], cutoffs: list[str], offsets: list[float]) -> dict:
    valid = [row for row in rows if row["analysis_status"] == "ok"]
    result = {
        "stopped_initial_trades": len(rows), "valid_cached_replays": len(valid),
        "unavailable_cached_replays": len(rows) - len(valid),
        "unique_symbols": len({row["symbol"] for row in rows}),
        "initial_stop_fill_above_high_percent": describe([row["initial_stop_fill_above_high_percent"] for row in rows]),
        "cutoffs": {},
    }
    for cutoff in cutoffs:
        prefix = cutoff.replace(":", "")
        eligible = [row for row in valid if row[f"{prefix}_next_open_eligible"]]
        metric = f"{prefix}_maximum_above_early_high_percent"
        result["cutoffs"][cutoff] = {
            "activation_at_or_after_cutoff": sum(not row[f"{prefix}_activation_before_cutoff"] for row in rows),
            "activation_before_cutoff": sum(row[f"{prefix}_activation_before_cutoff"] for row in rows),
            "eligible_with_observed_post_stop_bar": len(eligible),
            "eligible_time_but_no_observed_bar": sum(row[f"{prefix}_activation_before_cutoff"] and not row[f"{prefix}_next_open_eligible"] for row in valid),
            "eligible_with_sparse_minutes": sum(row[f"{prefix}_unobserved_minutes"] > 0 for row in eligible),
            "eligible_without_exact_cutoff_bar": sum(not row[f"{prefix}_exact_cutoff_bar_present"] for row in eligible),
            "next_open_above_high_percent": describe([row["next_open_above_high_percent"] for row in eligible]),
            "future_max_above_high_percent": describe([row[metric] for row in eligible]),
            "offset_reach": [{
                "entry_above_high_percent": level,
                "denominator_observed_eligible": len(eligible),
                "next_open_already_at_or_above": sum(row["next_open_above_high_percent"] >= level - 1e-9 for row in eligible),
                "future_high_at_or_above": sum(row[metric] >= level - 1e-9 for row in eligible),
                "next_open_already_at_or_above_percent": (100 * sum(row["next_open_above_high_percent"] >= level - 1e-9 for row in eligible) / len(eligible)) if eligible else None,
                "future_high_at_or_above_percent": (100 * sum(row[metric] >= level - 1e-9 for row in eligible) / len(eligible)) if eligible else None,
            } for level in offsets],
            "largest_future_excursions": [{
                "date": row["date"], "symbol": row["symbol"], "above_high_percent": row[metric],
                "early_high": row["early_high"], "future_high": row[f"{prefix}_maximum_high"],
                "future_high_bar_time": row[f"{prefix}_maximum_high_bar_time"],
            } for row in sorted(eligible, key=lambda item: item[metric], reverse=True)[:5]],
        }
    return result


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def markdown(summary: dict) -> str:
    overall = summary["bands"]["all"]
    lines = [
        "# Post-stop excursion analysis — 4am short", "",
        f"Source report: `{summary['provenance']['source_report']}`.",
        f"Input SHA256: `{summary['provenance']['input_sha256']}`.", "",
        f"Found **{overall['stopped_initial_trades']} initial stop-outs**; reproduced {overall['valid_cached_replays']} from offline minute bars. "
        f"Unavailable or mismatching caches: {overall['unavailable_cached_replays']}.", "",
        "All offsets are percentages above the original setup-window high, from the early window or a late qualifier's fixed window. The stop fill is known at the stop. "
        "The next available minute's open is the first modeled re-entry opportunity. Future maximums use bars after the stop minute and strictly before the cutoff. "
        "The stop minute's high is recorded separately because its order relative to the stop cannot be inferred.", "",
        "| Period | Cutoff | Eligible observed | Avg next open | Median next open | Avg future max | Median future max | Future max P75 | Future max P90 | Max |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    def fmt(value):
        return f"{value:.2f}%" if value is not None else "—"
    for band, stats in summary["bands"].items():
        for cutoff, part in stats["cutoffs"].items():
            opens = part["next_open_above_high_percent"]
            maxima = part["future_max_above_high_percent"]
            lines.append(f"| {band} | {cutoff} | {part['eligible_with_observed_post_stop_bar']} | {fmt(opens.get('mean'))} | {fmt(opens.get('median'))} | {fmt(maxima.get('mean'))} | {fmt(maxima.get('median'))} | {fmt(maxima.get('p75'))} | {fmt(maxima.get('p90'))} | {fmt(maxima.get('maximum'))} |")
    stops = overall["initial_stop_fill_above_high_percent"]
    lines.extend(["", f"Initial stop-fill offset: mean {fmt(stops.get('mean'))}, median {fmt(stops.get('median'))}, minimum {fmt(stops.get('minimum'))}, maximum {fmt(stops.get('maximum'))}.", ""])
    for cutoff, part in overall["cutoffs"].items():
        future = part["future_max_above_high_percent"]
        lines.extend([
            f"## Before {cutoff} Eastern", "",
            f"{part['activation_at_or_after_cutoff']} initial stops activate too late; {part['eligible_time_but_no_observed_bar']} are early enough but have no observed eligible bar. "
            f"{part['eligible_with_sparse_minutes']} eligible cases have sparse minutes; {part['eligible_without_exact_cutoff_bar']} lack an exact cutoff bar. "
            "Missing aggregate minutes can mean no qualifying trades; they are not interpolated, and available highs do not establish quote-level fillability.", "",
            f"Future maximum mean excluding the largest: {fmt(future.get('mean_excluding_single_largest'))}; "
            f"10% trimmed from each tail: {fmt(future.get('ten_percent_each_tail_trimmed_mean'))}.", "",
            "| Entry offset | Already at/above at next open | Reached before cutoff | Observed denominator |",
            "|---:|---:|---:|---:|",
        ])
        for reach in part["offset_reach"]:
            lines.append(f"| {reach['entry_above_high_percent']:g}% | {reach['next_open_already_at_or_above']} ({fmt(reach['next_open_already_at_or_above_percent'])}) | {reach['future_high_at_or_above']} ({fmt(reach['future_high_at_or_above_percent'])}) | {reach['denominator_observed_eligible']} |")
        lines.append("")
    lines.extend([
        "", "## Interpretation", "",
        "The average future maximum is hindsight, not an entry signal or a guaranteed achievable average fill. "
        "If the price is already above a sell-short limit when re-entry activates, the simulator fills at that next available open (subject to configured slippage), so low offsets can produce the same trade. "
        "A higher offset may avoid immediate re-entry, but it also skips stocks that reverse before reaching it. Use the parameter sweep's realized outcomes and chronological validation to compare that tradeoff.", "",
        "The Jan–Jun / Jul–Sep split is a chronological diagnostic on an already available dataset. It is not untouched out-of-sample evidence if settings were selected after reviewing both periods. "
        "Analysis is conditional on initial stop-outs successfully observed in the source run; excluded source data errors/incomplete initial trades are not assumed to have no stop. "
        "One-minute highs are trade aggregates rather than executable SIP bid/ask quotes. Exact deadline bars are excluded from entry opportunities; missing exact time-exit bars can make a separate simulation unresolved.", "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("backtest_4am_short.json"))
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cutoffs", nargs="+", default=["09:00", "09:20", "09:30"])
    parser.add_argument("--training-through", type=date.fromisoformat, default=date(2026, 6, 30))
    args = parser.parse_args()
    config = load_config(args.config)
    report = args.source_report.resolve()
    config_file = report / "4am_short_config.resolved.json"
    source_summary_file = report / "4am_short_summary.json"
    source_trades_file = report / "4am_short_trades.csv"
    source_summary = json.loads(source_summary_file.read_text())
    # Resolved snapshots contain absolute paths. Re-load them to apply defaults
    # added since older reports were generated, including the Massive provider.
    source_config = load_config(config_file).snapshot()
    current_snapshot = config.snapshot()
    if source_config != current_snapshot:
        raise DataError("Source report resolved config must exactly match current config")
    input_hash = sha256(config.input_file)
    if source_summary.get("input_sha256") != input_hash:
        raise DataError("Source report input hash does not match current raw input")
    if source_summary.get("interrupted") or source_summary.get("unprocessed_candidates"):
        raise DataError("Source report must have processed every requested candidate")
    for cutoff in args.cutoffs:
        if len(cutoff) != 5 or time.fromisoformat(cutoff).strftime("%H:%M") != cutoff:
            raise DataError("Cutoffs must use HH:MM")
    with source_trades_file.open(newline="", encoding="utf-8") as handle:
        stops = [row for row in csv.DictReader(handle) if row["status"] == "trade" and int(row.get("trade_number") or 1) == 1 and row["exit_reason"] == "stop_loss"]
    if len({(row["date"], row["symbol"]) for row in stops}) != len(stops):
        raise DataError("Source report contains duplicate initial stop-outs")
    client = create_client(config.data, offline=True)
    rows = [inspect_stop(row, config, client, args.cutoffs) for row in stops]
    offsets = [2.5 * index for index in range(13)]
    bands = {
        "all": rows,
        f"train_through_{args.training_through}": [row for row in rows if date.fromisoformat(row["date"]) <= args.training_through],
        f"validation_after_{args.training_through}": [row for row in rows if date.fromisoformat(row["date"]) > args.training_through],
    }
    summary = {
        "created_at": datetime.now(EASTERN).isoformat(), "timezone": str(EASTERN),
        "provenance": {
            "config_file": str(args.config.resolve()), "config_file_sha256": sha256(args.config),
            "input_file": str(config.input_file), "input_sha256": input_hash,
            "source_report": str(report), "source_config_snapshot": str(config_file),
            "source_config_snapshot_sha256": sha256(config_file),
            "source_trades_csv": str(source_trades_file), "source_trades_csv_sha256": sha256(source_trades_file),
            "source_summary_json": str(source_summary_file), "source_summary_json_sha256": sha256(source_summary_file),
            "source_initial_trades": source_summary["statistics"]["initial_trades"],
            "source_errors": source_summary["statistics"]["errors"],
            "source_incomplete": source_summary["statistics"]["incomplete"],
            "data_provider": config.data.provider,
            "bar_source": f"Validated {provider_label(config.data)} offline cache, unadjusted one-minute trade aggregates",
            "configuration_snapshot": current_snapshot,
        },
        "method": {
            "selection": "Completed initial trades with exit_reason=stop_loss; each is replayed against cached bars",
            "activation": "Initial stop bar timestamp + 1 minute",
            "boundaries": "Include bars at/after activation and strictly before each cutoff; all times Eastern",
            "next_open": "First observed minute open at/after activation, only included in cutoff statistics if before cutoff",
            "future_maximum": "Maximum bar high at/after activation and strictly before cutoff; hindsight only",
            "sparse_bars": "Not interpolated; absent minutes can indicate no eligible trades, not necessarily a feed outage",
            "quantiles": "Linear interpolation on sorted observations at (n-1)*quantile",
            "offsets_percent": offsets,
        },
        "bands": {name: summarize(values, args.cutoffs, offsets) for name, values in bands.items()},
    }
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "excursion_stopped_stocks.csv", rows)
    reach_rows = []
    for band, stats in summary["bands"].items():
        for cutoff, part in stats["cutoffs"].items():
            reach_rows.extend({"band": band, "cutoff_eastern": cutoff, **row} for row in part["offset_reach"])
    write_csv(output / "excursion_offset_reach.csv", reach_rows)
    (output / "excursion_summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "excursion_analysis.md").write_text(markdown(summary), encoding="utf-8")
    print(json.dumps({"output_dir": str(output), "stopped_trades": len(rows), "unavailable": sum(row["analysis_status"] != "ok" for row in rows), "cutoffs": summary["bands"]["all"]["cutoffs"]}, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
