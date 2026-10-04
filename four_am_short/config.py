"""Strict JSON configuration. Paths are relative to the JSON file."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path

from . import STRATEGY_ID, STRATEGY_NAME
from .models import DataError


@dataclass(frozen=True)
class ReentryConfig:
    enabled: bool = False
    entry_above_high_percent: float = 5.0
    stop_loss_percent: float = 20.0
    profit_target_percent: float = 40.0
    entry_deadline: str = "09:20"
    time_exit: str = "09:20"


@dataclass(frozen=True)
class StrategyConfig:
    shares: int = 1000
    gap_percent: float = 30.0
    early_start: str = "04:00"
    early_end: str = "04:15"
    late_gap_enabled: bool = False
    late_gap_window_minutes: int = 15
    wait_after_high_minutes: int = 10
    entry_below_high_percent: float = 10.0
    min_entry_price: float | None = None
    max_entry_price: float | None = None
    entry_deadline: str = "06:00"
    stop_loss_percent: float = 30.0
    profit_target_percent: float = 12.5
    time_exit: str = "09:30"
    high_time_reference: str = "bar_end"
    repeated_high_policy: str = "last"
    intrabar_policy: str = "conservative"
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    commission_per_share_per_side: float = 0.0
    locate_fee_per_share: float = 0.0
    reentry: ReentryConfig = field(default_factory=ReentryConfig)

    def entry_price_allowed(self, price: float) -> bool:
        """Whether an entry price is valid and within the inclusive bounds."""
        return (
            not isinstance(price, bool)
            and isinstance(price, (int, float))
            and math.isfinite(price)
            and price > 0
            and (self.min_entry_price is None or price >= self.min_entry_price)
            and (self.max_entry_price is None or price <= self.max_entry_price)
        )


@dataclass(frozen=True)
class DataConfig:
    api_key_env: str = "MASSIVE_API_KEY"
    env_file: Path | None = Path(".env")
    cache_dir: Path = Path(".cache/massive")
    timeout_seconds: float = 30.0
    max_retries: int = 3
    request_delay_seconds: float = 0.25
    previous_close_lookback_days: int = 14
    calendar_symbol: str = "SPY"
    provider: str = "massive"
    alpaca_api_key_env: str = "ALPACA_API_KEY"
    alpaca_secret_key_env: str = "ALPACA_SECRET_KEY"


@dataclass(frozen=True)
class BacktestConfig:
    input_file: Path
    output_dir: Path
    strategy: StrategyConfig
    data: DataConfig
    from_date: date | None = None
    to_date: date | None = None
    symbols: tuple[str, ...] = ()
    strategy_id: str = STRATEGY_ID
    strategy_name: str = STRATEGY_NAME

    def snapshot(self) -> dict:
        raw = asdict(self)
        raw["shares"] = raw["strategy"].pop("shares")
        # Round trip through JSON to normalize Paths/dates, never include secrets.
        return json.loads(json.dumps(raw, default=str))


def iso_date(value: object, name: str = "date") -> date:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except ValueError as exc:
        raise DataError(f"{name} must use YYYY-MM-DD") from exc


def clock(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise DataError(f"{name} must use HH:MM Eastern time")
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise DataError(f"{name} must use HH:MM Eastern time") from exc
    return value


def number(value: object, name: str, *, minimum: float = 0, strict: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError(f"{name} must be a JSON number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise DataError(f"{name} must be a finite JSON number") from exc
    if not math.isfinite(result) or result < minimum or (strict and result == minimum):
        raise DataError(f"{name} must be finite and {'greater than' if strict else 'at least'} {minimum}")
    return result


def integer(value: object, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise DataError(f"{name} must be an integer >= {minimum}")
    return value


def object_section(raw: object, allowed: set[str], name: str) -> dict:
    if not isinstance(raw, dict):
        raise DataError(f"{name} must be a JSON object")
    unknown = raw.keys() - allowed
    if unknown:
        raise DataError(f"Unknown {name} parameters: {', '.join(sorted(unknown))}")
    return dict(raw)


def _path(value: object, base: Path, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DataError(f"{name} must be a nonempty path string")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def parse_strategy(raw: object, shares: object = 1000) -> StrategyConfig:
    """Validate file configuration and recorded strategy snapshots identically."""
    values = object_section(raw, {f.name for f in fields(StrategyConfig)} - {"shares"}, "strategy")
    values["shares"] = integer(shares, "shares", 1)
    strategy = asdict(StrategyConfig(**values))
    if type(strategy["late_gap_enabled"]) is not bool:
        raise DataError("strategy.late_gap_enabled must be a JSON boolean")
    integer(strategy["late_gap_window_minutes"], "late_gap_window_minutes", 1)
    for name in ("early_start", "early_end", "entry_deadline", "time_exit"):
        clock(strategy[name], name)
    if not strategy["early_start"] < strategy["early_end"] < strategy["entry_deadline"] <= strategy["time_exit"]:
        raise DataError("Require early_start < early_end < entry_deadline <= time_exit on the same Eastern date")
    integer(strategy["wait_after_high_minutes"], "wait_after_high_minutes", 0)
    for name in ("gap_percent", "entry_below_high_percent", "stop_loss_percent", "profit_target_percent", "entry_slippage_bps", "exit_slippage_bps", "commission_per_share_per_side", "locate_fee_per_share"):
        strategy[name] = number(strategy[name], name, strict=name in {"stop_loss_percent", "profit_target_percent"})
    for name in ("min_entry_price", "max_entry_price"):
        if strategy[name] is not None:
            strategy[name] = number(strategy[name], f"strategy.{name}", strict=True)
    if (strategy["min_entry_price"] is not None and strategy["max_entry_price"] is not None
            and strategy["min_entry_price"] > strategy["max_entry_price"]):
        raise DataError("Require strategy.min_entry_price <= strategy.max_entry_price")
    for name in ("entry_below_high_percent", "profit_target_percent"):
        if strategy[name] >= 100:
            raise DataError(f"{name} must be less than 100")
    if strategy["entry_slippage_bps"] >= 10000:
        raise DataError("entry_slippage_bps must be less than 10000")
    for name, options in {"high_time_reference": ("bar_end", "bar_start"), "repeated_high_policy": ("first", "last"), "intrabar_policy": ("conservative", "ohlc", "olhc")}.items():
        if strategy[name] not in options:
            raise DataError(f"{name} must be one of {', '.join(options)}")
    reentry = asdict(ReentryConfig())
    reentry.update(object_section(strategy["reentry"], set(reentry), "strategy.reentry"))
    if type(reentry["enabled"]) is not bool:
        raise DataError("strategy.reentry.enabled must be a JSON boolean")
    for name in ("entry_above_high_percent", "stop_loss_percent", "profit_target_percent"):
        reentry[name] = number(reentry[name], f"strategy.reentry.{name}", strict=name != "entry_above_high_percent")
    if reentry["profit_target_percent"] >= 100:
        raise DataError("strategy.reentry.profit_target_percent must be less than 100")
    for name in ("entry_deadline", "time_exit"):
        clock(reentry[name], f"strategy.reentry.{name}")
    if not strategy["early_end"] < reentry["entry_deadline"] <= reentry["time_exit"]:
        raise DataError("Require early_end < reentry.entry_deadline <= reentry.time_exit on the same Eastern date")
    strategy["reentry"] = ReentryConfig(**reentry)
    return StrategyConfig(**strategy)


def load_config(path: Path) -> BacktestConfig:
    path = path.expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise DataError(f"Cannot read JSON config {path}: {exc}") from exc
    raw = object_section(raw, {"strategy_id", "strategy_name", "input_file", "output_dir", "shares", "strategy", "data", "from_date", "to_date", "symbols"}, "config")
    for name, expected in (("strategy_id", STRATEGY_ID), ("strategy_name", STRATEGY_NAME)):
        if raw.get(name, expected) != expected:
            raise DataError(f"{name} must be {expected!r} for the 4am short backtest")
    if "input_file" not in raw:
        raise DataError("input_file is required")
    strategy = parse_strategy(raw.get("strategy", {}), raw.get("shares", 1000))
    data = asdict(DataConfig())
    data.update(object_section(raw.get("data", {}), set(data), "data"))
    for name in ("timeout_seconds", "request_delay_seconds"):
        data[name] = number(data[name], name, strict=name == "timeout_seconds")
    integer(data["max_retries"], "max_retries", 0)
    integer(data["previous_close_lookback_days"], "previous_close_lookback_days", 1)
    if not isinstance(data["provider"], str) or data["provider"] not in {"massive", "alpaca"}:
        raise DataError("data.provider must be massive or alpaca")
    for name in ("api_key_env", "alpaca_api_key_env", "alpaca_secret_key_env"):
        if not isinstance(data[name], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", data[name]):
            raise DataError(f"{name} must be an environment variable name")
    data["calendar_symbol"] = symbol(data["calendar_symbol"])
    data["cache_dir"] = _path(str(data["cache_dir"]) if isinstance(data["cache_dir"], Path) else data["cache_dir"], path.parent, "cache_dir")
    if data["env_file"] is not None:
        data["env_file"] = _path(str(data["env_file"]) if isinstance(data["env_file"], Path) else data["env_file"], path.parent, "env_file")
    start = iso_date(raw["from_date"], "from_date") if raw.get("from_date") is not None else None
    end = iso_date(raw["to_date"], "to_date") if raw.get("to_date") is not None else None
    if start and end and start > end:
        raise DataError("from_date must be on or before to_date")
    symbols = raw.get("symbols", [])
    if not isinstance(symbols, list):
        raise DataError("symbols must be an array of ticker strings")
    return BacktestConfig(
        input_file=_path(raw["input_file"], path.parent, "input_file"),
        output_dir=_path(raw.get("output_dir", f"outcome/{STRATEGY_ID}"), path.parent, "output_dir"),
        strategy=strategy, data=DataConfig(**data),
        from_date=start, to_date=end, symbols=tuple(dict.fromkeys(symbol(s) for s in symbols)),
    )


def symbol(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", value):
        raise DataError(f"Invalid stock symbol: {value!r}")
    return value.upper()


def read_api_key(config: DataConfig) -> str | None:
    """Read only the requested credential, without importing other .env settings."""
    return _read_credential(config, (config.api_key_env,))


def read_alpaca_credentials(config: DataConfig) -> tuple[str | None, str | None]:
    """Use the live feed's credential names/aliases; never load broker settings."""
    key_names = (config.alpaca_api_key_env,)
    secret_names = (config.alpaca_secret_key_env,)
    if config.alpaca_api_key_env == "ALPACA_API_KEY":
        key_names += ("APCA_API_KEY_ID",)
    if config.alpaca_secret_key_env == "ALPACA_SECRET_KEY":
        secret_names += ("APCA_API_SECRET_KEY",)
    return _read_credential(config, key_names), _read_credential(config, secret_names)


def _read_credential(config: DataConfig, names: tuple[str, ...]) -> str | None:
    for name in names:
        key = os.environ.get(name, "").strip()
        if key:
            return key
    if config.env_file is None or not config.env_file.exists():
        return None
    try:
        values = {}
        for line in config.env_file.read_text(encoding="utf-8-sig").splitlines():
            text = line.strip()
            if text.startswith("export "):
                text = text[7:].lstrip()
            name, separator, value = text.partition("=")
            if separator and name.strip() in names:
                value = value.strip()
                if value.startswith(("'", '"')):
                    quote = value[0]
                    end = value.find(quote, 1)
                    if end < 0:
                        raise DataError(f"Unclosed credential quote in {config.env_file}")
                    value = value[1:end]
                else:
                    value = value.split(" #", 1)[0].strip()
                values.setdefault(name.strip(), value)
        return next((values[name] for name in names if values.get(name)), None)
    except OSError as exc:
        raise DataError(f"Cannot read credential file {config.env_file}") from exc
    return None
