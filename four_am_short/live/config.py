"""Independent live settings; strategy rules come from the named backtest JSON."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ..config import StrategyConfig, load_config

PROJECT = Path(__file__).resolve().parents[2]
OFFER_LOCATE_ROUTES = frozenset({"LOCATE4", "LOCATE6"})
CAPPED_LOCATE_ROUTES = frozenset({"LOCATE10"})
UNCAPPED_LOCATE_ROUTES = frozenset({"LOCATE1", "LOCATE7", "LOCATE8", "LOCATE12", "LOCATE14"})


@dataclass(frozen=True)
class DataSettings:
    api_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    paper: bool = False
    rest_timeout_seconds: float = 20.0
    batch_size: int = 200
    rest_concurrency: int = 3
    reconnect_seconds: float = 3.0
    quote_max_age_seconds: float = 5.0
    future_tolerance_seconds: float = 1.0


@dataclass(frozen=True)
class DasSettings:
    host: str = "127.0.0.1"
    port: int = 9800
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    account: str = field(default="", repr=False)
    route: str = "ARCAE"
    backup_route: str = "CBATS"
    timeout_seconds: float = 10.0
    health_check_seconds: float = 10.0
    reconnect_seconds: float = 5.0
    paper: bool = True
    locate_route: str = ""
    locate_route_type: int = 0
    locate_limit_price_supported: bool = False
    locate_routes: tuple[str, ...] = ("LOCATE4", "LOCATE6", "LOCATE10")
    locate_quote_wait_seconds: float = 5.0
    locate_limit_price_routes: tuple[str, ...] = ("LOCATE10",)
    locate_quote_routes: tuple[str, ...] = ()
    allow_uncapped_locate_purchases: bool = False
    locate_offer_only: bool = False
    locate_offer_routes: tuple[str, ...] = ("LOCATE4", "LOCATE6")
    locate_priced_only: bool = True


@dataclass(frozen=True)
class LiveSettings:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    data: DataSettings = field(default_factory=DataSettings)
    das: DasSettings = field(default_factory=DasSettings)
    config_path: Path = PROJECT / "live_4am_short.json"
    strategy_config_path: Path = PROJECT / "backtest_4am_short.json"
    state_dir: Path = PROJECT / "state/4am_short"
    mode: str = "monitor"
    host: str = "127.0.0.1"
    port: int = 8003
    max_positions: int = 0
    max_locate_price: float = 0.06
    locate_trigger_below_entry_percent: float | None = 1.0
    cover_cushion_percent: float = 1.0
    cover_replace_seconds: float = 3.0
    poll_seconds: float = 1.0
    final_bar_wait_seconds: float = 35.0
    shutdown_grace_seconds: float = 30.0
    symbols: tuple[str, ...] = ()
    demo: bool = False

    @property
    def execution_enabled(self) -> bool:
        return self.mode in {"das_paper", "das_live"} and not self.demo

    @property
    def identity(self) -> str:
        account = f"{self.das.host}:{self.das.port}/{self.das.account}" if self.execution_enabled else "monitor"
        return hashlib.sha256(f"4am_short/{self.mode}/{account}".encode()).hexdigest()[:20]

    @property
    def state_path(self) -> Path:
        return self.state_dir / f"4am_short_{self.mode}_{self.identity}.json"

    def public_rules(self) -> dict:
        return {
            **asdict(self.strategy), "timezone": "America/New_York",
            "strategy_config": str(self.strategy_config_path),
            "max_positions": self.max_positions, "max_locate_price": self.max_locate_price,
            "locate_trigger_below_entry_percent": self.locate_trigger_below_entry_percent,
            "cover_cushion_percent": self.cover_cushion_percent,
            "final_bar_wait_seconds": self.final_bar_wait_seconds,
            "data_feed": "Alpaca live SIP", "execution_broker": "DAS CMD API",
            "order_route": self.das.route, "backup_route": self.das.backup_route,
            "das_health_check_seconds": self.das.health_check_seconds,
            "das_reconnect_seconds": self.das.reconnect_seconds,
            "locate_routes": list(self.das.locate_routes),
            "locate_quote_routes": list(self.das.locate_quote_routes),
            "allow_uncapped_locate_purchases": self.das.allow_uncapped_locate_purchases,
        }


def _environment(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            name, separator, value = line.partition("=")
            if separator and name.strip() and not name.startswith("#"):
                value = value.strip()
                if value.startswith(("'", '"')):
                    end = value.find(value[0], 1)
                    if end < 0:
                        raise ValueError("Unclosed quote in the live credential file")
                    value = value[1:end]
                else:
                    value = value.split(" #", 1)[0].strip()
                values[name.strip()] = value
    values.update(os.environ)
    return values


def _section(raw: object, allowed: set[str], name: str) -> dict:
    if not isinstance(raw, dict) or raw.keys() - allowed:
        raise ValueError(f"Invalid or unknown {name} settings")
    return raw


def _positive(value: object, name: str, *, zero: bool = False, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and type(value) is not int):
        raise ValueError(f"{name} must be {'an integer' if integer else 'a number'}")
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(f"Invalid {name}")
    return value


def load_live_settings(path: Path | str = PROJECT / "live_4am_short.json") -> LiveSettings:
    path = Path(path).expanduser().resolve()
    raw = json.loads(path.read_text())
    _section(raw, {"strategy_id", "strategy_config", "env_file", "mode", "state_dir", "ui", "alpaca", "das", "execution", "symbols"}, "live")
    if raw.get("strategy_id", "4am_short") != "4am_short":
        raise ValueError("This launcher supports only 4am short")
    def local(value: str) -> Path:
        return (path.parent / Path(value).expanduser()).resolve()
    strategy_path = local(raw.get("strategy_config", "backtest_4am_short.json"))
    strategy = load_config(strategy_path).strategy
    env = _environment(local(raw.get("env_file", ".env")))
    mode = raw.get("mode", "monitor")
    if mode not in {"monitor", "das_paper", "das_live"}:
        raise ValueError("mode must be monitor, das_paper, or das_live")
    alpaca = dict(_section(raw.get("alpaca", {}), {"paper", "rest_timeout_seconds", "batch_size", "rest_concurrency", "reconnect_seconds", "quote_max_age_seconds", "future_tolerance_seconds"}, "alpaca"))
    if "paper" in alpaca and type(alpaca["paper"]) is not bool:
        raise ValueError("alpaca.paper must be a boolean")
    for name in set(alpaca) - {"paper"}:
        _positive(alpaca[name], name, zero=name == "future_tolerance_seconds", integer=name in {"batch_size", "rest_concurrency"})
    data = DataSettings(api_key=env.get("ALPACA_API_KEY") or env.get("APCA_API_KEY_ID", ""), secret_key=env.get("ALPACA_SECRET_KEY") or env.get("APCA_API_SECRET_KEY", ""), **alpaca)
    das_values = dict(_section(raw.get("das", {}), {"route", "backup_route", "timeout_seconds", "health_check_seconds", "reconnect_seconds", "locate_quote_wait_seconds", "locate_offer_routes", "locate_limit_price_routes", "locate_quote_routes", "allow_uncapped_locate_purchases"}, "das"))
    if "allow_uncapped_locate_purchases" in das_values and type(das_values["allow_uncapped_locate_purchases"]) is not bool:
        raise ValueError("allow_uncapped_locate_purchases must be a boolean")
    for name in ("route", "backup_route"):
        if name in das_values:
            route = das_values[name]
            if not isinstance(route, str) or (not route and name == "route") or (route and not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]*", route)):
                raise ValueError(f"Invalid DAS {name}")
            if route in {"ALL", "ALLROUTE", "LIMIT", "MARKET", "STOP", "ARCAEL", "CBATSL"}:
                raise ValueError("Use a DAS CMD base route, such as ARCAE or CBATS")
    for name in ("timeout_seconds", "health_check_seconds", "reconnect_seconds", "locate_quote_wait_seconds"):
        if name in das_values:
            _positive(das_values[name], name)
    for name in ("locate_offer_routes", "locate_limit_price_routes", "locate_quote_routes"):
        if name in das_values:
            routes = das_values[name]
            if not isinstance(routes, list) or any(not isinstance(route, str) or not re.fullmatch(r"LOCATE[0-9]+", route) for route in routes):
                raise ValueError(f"Invalid {name}")
            das_values[name] = tuple(dict.fromkeys(routes))
    offer_routes = das_values.get("locate_offer_routes", ("LOCATE4", "LOCATE6"))
    priced_routes = das_values.get("locate_limit_price_routes", ("LOCATE10",))
    quote_routes = das_values.get("locate_quote_routes", ())
    if not set(offer_routes) <= OFFER_LOCATE_ROUTES:
        raise ValueError("Only LOCATE4/LOCATE6 have confirmed explicit-offer behavior for this integration")
    if not set(priced_routes) <= CAPPED_LOCATE_ROUTES:
        raise ValueError("Only LOCATE10 has confirmed locate price-cap support for this integration")
    if not set(quote_routes) <= UNCAPPED_LOCATE_ROUTES:
        raise ValueError("locate_quote_routes supports LOCATE1/LOCATE7/LOCATE8/LOCATE12/LOCATE14")
    all_routes = (*offer_routes, *priced_routes, *quote_routes)
    if len(set(all_routes)) != len(all_routes):
        raise ValueError("Offer, price-capped, and quote locate routes must be distinct")
    das = DasSettings(host=env.get("DAS_HOST", "127.0.0.1"), port=int(env.get("DAS_PORT", "9800")), username=env.get("DAS_USERNAME", ""), password=env.get("DAS_PASSWORD", ""), account=env.get("DAS_ACCOUNT", ""), paper=mode != "das_live", locate_routes=all_routes, **das_values)
    if not 1 <= das.port <= 65535:
        raise ValueError("Invalid DAS port")
    execution = dict(_section(raw.get("execution", {}), {"reentry_enabled", "max_positions", "max_locate_price", "locate_trigger_below_entry_percent", "cover_cushion_percent", "cover_replace_seconds", "poll_seconds", "final_bar_wait_seconds", "shutdown_grace_seconds"}, "execution"))
    reentry_enabled = execution.pop("reentry_enabled", None)
    if reentry_enabled is not None:
        if type(reentry_enabled) is not bool:
            raise ValueError("execution.reentry_enabled must be a boolean or null to inherit the strategy setting")
        strategy = replace(strategy, reentry=replace(strategy.reentry, enabled=reentry_enabled))
    for name, value in execution.items():
        if name == "locate_trigger_below_entry_percent":
            if value is not None:
                _positive(value, name, zero=True)
                if value >= 100:
                    raise ValueError("locate_trigger_below_entry_percent must be less than 100, or null to disable")
            continue
        _positive(value, name, zero=name in {"max_positions", "max_locate_price", "cover_cushion_percent", "final_bar_wait_seconds", "shutdown_grace_seconds"}, integer=name == "max_positions")
    ui = _section(raw.get("ui", {}), {"host", "port"}, "ui")
    if ui.get("host", "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The trading dashboard must bind to loopback")
    port = _positive(ui.get("port", 8003), "UI port", integer=True)
    if port > 65535:
        raise ValueError("Invalid UI port")
    symbols = raw.get("symbols", [])
    if not isinstance(symbols, list) or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]*", s) for s in symbols):
        raise ValueError("symbols must be an array of uppercase stock tickers")
    return LiveSettings(strategy=strategy, data=data, das=das, config_path=path, strategy_config_path=strategy_path, state_dir=local(raw.get("state_dir", "state/4am_short")), mode=mode, host=ui.get("host", "127.0.0.1"), port=port, symbols=tuple(dict.fromkeys(symbols)), **execution)
