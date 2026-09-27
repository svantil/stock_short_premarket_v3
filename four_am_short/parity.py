"""Read-only comparisons of broker records and historical bar simulations.

An SIP REST rerun removes the vendor difference, but does not recreate the
quotes, bar revisions, availability of locates or arrival order seen live.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import asdict, fields
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import StrategyConfig, parse_strategy
from .models import Bar, DataError, EASTERN, PreviousClose
from .strategy import simulate_trades


def stamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise DataError("Comparison timestamps must be strings with a timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise DataError("Comparison timestamps must include a timezone")
    return result.astimezone(EASTERN)


def strategy_snapshot(raw: dict) -> StrategyConfig:
    if not isinstance(raw, dict) or raw.keys() - {f.name for f in fields(StrategyConfig)}:
        raise DataError("Invalid saved strategy snapshot")
    values = dict(raw)
    shares = values.pop("shares", 1000)
    return parse_strategy(values, shares)


def strategy_differences(left: dict, right: dict, prefix: str = "") -> list[str]:
    differences = []
    for key in sorted(left.keys() | right.keys()):
        path = prefix + key
        if isinstance(left.get(key), dict) and isinstance(right.get(key), dict):
            differences.extend(strategy_differences(left[key], right[key], path + "."))
        elif left.get(key) != right.get(key):
            differences.append(path)
    return differences


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in ("strategy.py", "setups.py", "pricing.py", "config.py", "models.py"):
        path = Path(__file__).with_name(name)
        if path.exists():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def report_for_day(root: Path, day: date, explicit: Path | None = None) -> tuple[Path | None, list[dict], dict]:
    # candidates.csv contains every attempt, including skipped/unfilled rows.
    # trades.csv alone would erase precisely the missing-entry cases we audit.
    files = list((explicit or root).glob("*candidates.csv")) if explicit else list(root.rglob("*candidates.csv"))
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = [row for row in csv.DictReader(handle) if row.get("date") == day.isoformat()]
        if not rows:
            continue
        configs = list(path.parent.glob("*config.resolved.json"))
        strategy = {}
        if len(configs) == 1:
            saved = json.loads(configs[0].read_text())
            strategy = {**saved["strategy"], "shares": saved.get("shares", saved["strategy"].get("shares", 1000))}
        return path.parent, rows, strategy
    if explicit:
        raise DataError(f"No backtest rows for {day} in {explicit}")
    return None, [], {}


def context_for_symbol(day_state: dict, ticker: str, fallback: StrategyConfig,
                       report_rows: list[dict]) -> dict:
    candidate = day_state.get("candidates", {}).get(ticker, {})
    trades = [t for t in day_state.get("trades", {}).values() if t.get("symbol") == ticker]
    initial = next((t for t in trades if t.get("trade_number", 1) == 1), {})
    evidence = latest_entry_evidence(initial)
    audit = day_state.get("audit", {})
    # A per-trade snapshot is authoritative; the first day snapshot may predate
    # a restart/configuration change, so don't silently assume it applied.
    raw_strategy = evidence.get("strategy") or asdict(fallback)
    strategy = strategy_snapshot(raw_strategy)
    source = "recorded live entry strategy" if evidence.get("strategy") else "current live configuration (historical settings unverified)"
    previous = evidence.get("previous_close") or candidate.get("previous_close")
    previous_source = "saved live close"
    matching = next((row for row in report_rows if row.get("symbol") == ticker and int(row.get("trade_number") or 1) == 1), {})
    if previous is None and matching.get("previous_close"):
        previous = float(matching["previous_close"])
        previous_source = "saved backtest close; live close unavailable"
    previous_date = evidence.get("previous_close_date") or audit.get("previous_close_date")
    date_source = "live evidence" if previous_date else "unavailable"
    if not previous_date and matching.get("previous_close_date") and matching.get("previous_close"):
        if previous is not None and math.isclose(float(previous), float(matching["previous_close"]), rel_tol=1e-10):
            previous_date = matching["previous_close_date"]
            date_source = "matching backtest prior-session date"
    return {"strategy": asdict(strategy), "strategy_source": source, "previous_close": previous,
            "previous_close_source": previous_source, "previous_close_date": previous_date,
            "previous_close_date_source": date_source}


def latest_entry_evidence(trade: dict) -> dict:
    """A deferred unpaid locate may have retried after a configuration reload."""
    attempts = trade.get("entry_attempt_evidence", [])
    return attempts[-1] if attempts and isinstance(attempts[-1], dict) else trade.get("entry_evidence", {})


def request_window(day: date, contexts: dict) -> tuple[str, str]:
    rules = [strategy_snapshot(item["strategy"]) for item in contexts.values()]
    start = min(rule.early_start for rule in rules)
    cutoffs = [clock for rule in rules for clock in
               ([rule.time_exit, rule.reentry.time_exit] if rule.reentry.enabled else [rule.time_exit])]
    end = datetime.combine(day, time.fromisoformat(max(cutoffs)), EASTERN) + timedelta(minutes=1)
    if end.date() != day:
        raise DataError("Comparison requires a time exit before 23:59")
    if datetime.now(EASTERN) < end:
        raise DataError(f"Wait until {end.isoformat()} for the complete backtest window")
    return start, end.strftime("%H:%M")


def encode_bar(bar: Bar) -> dict:
    return {**asdict(bar), "timestamp": bar.timestamp.isoformat()}


def decode_bars(rows: list[dict], day: date) -> list[Bar]:
    if not isinstance(rows, list):
        raise DataError("Invalid saved SIP bars")
    bars = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise DataError("Invalid saved SIP bar object")
        timestamp = stamp(row["timestamp"])
        prices = [row[name] for name in ("open", "high", "low", "close")]
        volume = row.get("volume", 0)
        if (timestamp.date() != day or timestamp.second or timestamp.microsecond or timestamp in seen
                or any(isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or p <= 0 for p in prices)
                or isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(volume) or volume < 0
                or prices[1] < max(prices) or prices[2] > min(prices)):
            raise DataError("Invalid, duplicate or wrong-date SIP bar")
        seen.add(timestamp)
        bars.append(Bar(timestamp, *prices, volume))
    return sorted(bars, key=lambda b: b.timestamp)


def simulate_dataset(dataset: dict, day: date, symbols: list[str]) -> tuple[dict, dict]:
    if not isinstance(dataset, dict) or dataset.get("schema") != 1 or dataset.get("feed") != "alpaca_sip" or dataset.get("date") != str(day):
        raise DataError("SIP dataset schema, feed or date does not match this comparison")
    if not isinstance(dataset.get("symbols"), dict):
        raise DataError("SIP dataset symbols must be an object")
    results, errors = {}, {}
    for ticker in symbols:
        item = dataset.get("symbols", {}).get(ticker)
        if item is None:
            errors[ticker] = "No saved SIP data for this symbol"
            continue
        try:
            if not isinstance(item, dict):
                raise DataError("SIP symbol data must be an object")
            previous_date = date.fromisoformat(item["previous_close_date"])
            previous = item["previous_close"]
            if previous_date >= day or isinstance(previous, bool) or not isinstance(previous, (int, float)) or not math.isfinite(previous) or previous <= 0:
                raise DataError("Invalid saved prior close/date")
            rules = strategy_snapshot(item["strategy"])
            bars = decode_bars(item["bars"], day)
            if not bars:
                raise DataError("No SIP bars returned; cannot establish a comparable session")
            results[ticker] = [asdict(row) for row in simulate_trades(day, ticker, bars, PreviousClose(previous_date, previous), rules)]
        except (DataError, KeyError, TypeError, ValueError) as exc:
            errors[ticker] = str(exc)
    return results, errors


LIVE_FIELDS = ("symbol", "date", "status", "trade_number", "entry_limit", "entry_time", "entry_avg_price",
               "entry_filled_qty", "requested_qty", "stop_price", "target_price", "exit_time", "exit_avg_price",
               "exit_reason", "realized_pnl", "note", "active_at", "entry_evidence", "entry_attempt_evidence", "exit_signal_evidence")
SETUP_FIELDS = ("previous_close", "first_gap_time", "first_gap_bar_high", "early_high", "early_high_bar_time",
                "early_high_time", "active_at", "entry_limit", "late_gap", "window_end")


def normalized(row: dict, *, live: bool = False) -> dict:
    if not live:
        return row
    result = {name: row.get(name) for name in LIVE_FIELDS if name in row}
    result.update(entry_price=row.get("entry_avg_price"), exit_price=row.get("exit_avg_price"),
                  gross_pnl=row.get("realized_pnl"), shares=row.get("entry_filled_qty", 0),
                  reason=row.get("note", ""))
    locate = row.get("locate", {})
    selected = next((offer for offer in locate.get("comparisons", []) if offer.get("selected") and offer.get("actual_total_cost") is not None), None)
    # Reused locates carry the original comparison list, but incur no new fee.
    result["recorded_locate_cost"] = (0.0 if locate.get("status") == "reused" else selected["actual_total_cost"] if selected else None)
    return result


def compare(day: date, symbols: list[str], day_state: dict, report_rows: list[dict],
            report_strategy: dict, contexts: dict, simulations: dict, errors: dict) -> dict:
    output = {"date": str(day), "timezone": "America/New_York", "symbols": {}, "simulation_errors": errors}
    for ticker in symbols:
        candidate = day_state.get("candidates", {}).get(ticker, {})
        live = sorted([normalized(t, live=True) for t in day_state.get("trades", {}).values() if t.get("symbol") == ticker], key=lambda t: t.get("trade_number", 1))
        saved = [row for row in report_rows if row.get("symbol") == ticker]
        sip = simulations.get(ticker, [])
        notes = []
        if not candidate and not live:
            notes.append("Missing from saved live candidates/trades; saved list alone cannot prove it never qualified.")
        if not saved:
            notes.append("Missing from selected backtest report for this date.")
        context = contexts[ticker]
        for attempt in live:
            recorded = latest_entry_evidence(attempt).get("strategy")
            if recorded and strategy_differences(context["strategy"], recorded):
                notes.append(f"Live attempt {attempt.get('trade_number', 1)} used different recorded settings; this SIP run uses the initial-entry configuration for both attempts.")
        if not context["strategy_source"].startswith("recorded"):
            notes.append("Historical live strategy was not recorded; SIP simulation uses explicitly assumed settings.")
        simulated_rules = strategy_snapshot(context["strategy"])
        if any(t.get("entry_price") is not None and not simulated_rules.entry_price_allowed(t["entry_price"]) for t in live):
            notes.append("Recorded live fill is outside this simulation's entry-price range; current limits may have been added after that trade.")
        if report_strategy:
            changed = strategy_differences(context["strategy"], report_strategy)
            if changed:
                notes.append("Strategy differs from saved backtest: " + ", ".join(changed))
        else:
            notes.append("Saved backtest configuration unavailable; strategy equality cannot be checked.")
        if context["previous_close_source"] != "saved live close":
            notes.append(context["previous_close_source"])
        if ticker in errors:
            notes.append(errors[ticker])
        # Compare setup fields independently from quote/broker fill differences.
        setup = (latest_entry_evidence(live[0]).get("setup") if live else None) or candidate
        for label, rows in (("saved backtest", saved), ("historical SIP", sip)):
            if not rows or not setup:
                continue
            initial = next((r for r in rows if int(r.get("trade_number") or 1) == 1), {})
            changed = []
            for left, right in (("first_gap_time", "first_gap_time"), ("early_high", "early_high"),
                                ("early_high_bar_time", "early_high_bar_time"), ("active_at", "order_active_time"), ("entry_limit", "entry_limit")):
                a, b = setup.get(left), initial.get(right)
                if a is None or b is None or a == "" or b == "":
                    continue
                same = (stamp(a) == stamp(b)) if "time" in left or left == "active_at" else math.isclose(float(a), float(b), rel_tol=1e-9)
                if not same:
                    changed.append(left)
            if changed:
                notes.append(f"Live setup differs from {label}: " + ", ".join(changed))
        output["symbols"][ticker] = {"live_setup": {k: candidate[k] for k in SETUP_FIELDS if k in candidate},
                                      "simulation_context": context, "live": live, "saved_backtest": saved,
                                      "sip_historical": sip, "notes": notes}
    return output


def render(report: dict) -> str:
    def price(value):
        return "—" if value is None or value == "" else f"${float(value):.5f}".rstrip("0").rstrip(".")

    def when(value):
        return stamp(value).strftime("%H:%M:%S") if value else "—"

    lines = [f"# Live/backtest comparison — {report['date']}", "", "All times Eastern. Historical timestamps label minute starts; live fills use broker timestamps.", "",
             "SIP historical simulation uses retrospective Alpaca bars and the shared strategy. It does not replay live quotes, corrections as received, locates, order latency or partial fills. Live gross P/L excludes broker fees. Saved backtest reports may predate code changes.", ""]
    for ticker, item in report["symbols"].items():
        lines.extend([f"## {ticker}", "", "| Source / attempt | Shares filled | Entry / time | Stop | Target | Exit / time | Gross P/L | Status / reason |",
                      "|---|---:|---|---:|---:|---|---:|---|"])
        for label, key in (("Live", "live"), ("Saved backtest", "saved_backtest"), ("SIP historical", "sip_historical")):
            for row in item[key]:
                entered = row.get("entry_price") not in (None, "")
                shares = row.get("shares", 0) if entered else 0
                lines.append(f"| {label} / {row.get('trade_number', 1)} | {shares} | {price(row.get('entry_price'))} / {when(row.get('entry_time'))} | {price(row.get('stop_price'))} | {price(row.get('target_price'))} | {price(row.get('exit_price'))} / {when(row.get('exit_time'))} | {price(row.get('gross_pnl'))} | {row.get('status', '')} / {row.get('exit_reason') or row.get('reason') or '—'} |")
        lines.append("")
        setup = item["live_setup"]
        if setup:
            lines.append(f"Saved live setup: first gap {when(setup.get('first_gap_time'))}; high {price(setup.get('early_high'))}; eligible {when(setup.get('active_at'))}; sell limit {price(setup.get('entry_limit'))}.")
            lines.append("")
        for label, key in (("Saved backtest", "saved_backtest"), ("SIP historical", "sip_historical")):
            initial = next((row for row in item[key] if int(row.get("trade_number") or 1) == 1), None)
            if initial:
                lines.append(f"{label} setup: first gap {when(initial.get('first_gap_time'))}; high {price(initial.get('early_high'))}; eligible {when(initial.get('order_active_time'))}; sell limit {price(initial.get('entry_limit'))}.")
                lines.append("")
        for note in item["notes"]:
            lines.append("- " + note)
        lines.append("")
    for note in report.get("notes", []):
        lines.append(note)
    return "\n".join(lines)
