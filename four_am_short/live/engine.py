"""Persisted 4am short supervisor: Alpaca prices, DAS locates and execution.

An entry attempt is durable before borrowing or sending an order. Broker tokens
are durable before submission. Missing acknowledgments are reconciled, never
interpreted as rejection. Quote-based covers cancel any unfilled entry remainder
and reconcile quantities before sending a buy, avoiding accidental long orders.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .. import STRATEGY_NAME
from ..config import load_config
from ..models import Bar, EASTERN
from ..pricing import order_price
from ..setups import select_setup
from . import audit
from .config import PROJECT, LiveSettings
from .das import DasClient, DasError, LocateDeferred, OrderRejected, OrderSubmissionUncertain, TERMINAL_STATUSES
from .data import AlpacaData, SIPFeed
from .store import StateStore

LOGGER = logging.getLogger(__name__)
LOCATE_RETRY_SECONDS = 30


def at(day: date, clock: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), EASTERN)


def stamp(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("Market timestamps must include a timezone")
    return result.astimezone(EASTERN)


def percent(price: float, offset: float) -> float:
    return float(Decimal(str(price)) * (1 + Decimal(str(offset)) / 100))


def _positive(value: Any) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid market price")
    return value


class LiveEngine:
    def __init__(self, settings: LiveSettings, *, data=None, broker=None, feed=None,
                 now: Callable[[], datetime] | None = None):
        self.settings = settings
        self.now = now or (lambda: datetime.now(EASTERN))
        self.data = data
        self.broker = broker
        self.feed = feed
        self.store = StateStore(settings.state_path, settings.identity)
        self.state: dict = {"version": 1, "identity": settings.identity, "days": {}, "events": []}
        self.running = False
        self.entries_enabled = False
        self._opened = False
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._bootstrap_task: asyncio.Task | None = None
        self._finalize_task: asyncio.Task | None = None
        self._late_finalize_task: asyncio.Task | None = None
        self._entry_task: asyncio.Task | None = None
        self._entry_symbol = ""
        self._backtest_task: asyncio.Task | None = None
        self._tick_lock = asyncio.Lock()
        self._quotes: dict[str, dict] = {}
        self._bars: dict[str, dict[str, Bar]] = {}
        self._setup_bars: dict[str, tuple[Bar, ...]] = {}
        self._audit_session_id = uuid4().hex
        self._audit_recorded_days: set[str] = set()
        self._closes: dict[str, float] = {}
        self._symbols: list[str] = []
        self._day = self.now().astimezone(EASTERN).date()
        self._market_day = False
        self._window_finalized = False
        self._dirty = False
        self._entry_block = ""
        self._subscriptions: set[str] = set()
        self._bootstrap_retry_at: datetime | None = None
        self._finalize_retry_at: datetime | None = None
        self._late_finalize_retry_at: datetime | None = None
        self._locate_service_task: asyncio.Task | None = None
        self._locate_service_at: datetime | None = None
        self._broker_health_task: asyncio.Task | None = None
        self._broker_check_at: datetime | None = None
        self._closing = False
        self._data_status = {"connected": False, "ready": False, "status": "Stopped", "last_message": None,
                             "universe_size": 0, "backfill_ready": False, "previous_close_date": None}
        account = settings.das.account
        self._broker_status = {"connected": False, "status": "No broker connection in monitor mode" if not settings.execution_enabled else "Disconnected",
                               "account_masked": ("****" + account[-4:]) if account else "Not configured",
                               "checking": False, "last_checked_at": None, "last_connected_at": None,
                               "last_disconnected_at": None, "last_error": None,
                               "next_check_at": None, "trading_blocked": False}
        self.backtest: dict = {"running": False, "error": None, "last_output": "", "latest_summary": None, "monthly": [], "reports_dir": None}

    def _day_state(self, day: date | None = None) -> dict:
        key = str(day or self._day)
        value = self.state["days"].setdefault(key, {"candidates": {}, "trades": {}})
        if key == str(self._day) and key not in self._audit_recorded_days:
            try:
                execution = {name: getattr(self.settings, name) for name in (
                    "final_bar_wait_seconds", "locate_trigger_below_entry_percent", "max_locate_price",
                    "max_positions", "cover_cushion_percent")}
                execution["quote_max_age_seconds"] = self.settings.data.quote_max_age_seconds
                session = audit.session_snapshot(self._audit_session_id, self.now(), self.settings.strategy,
                                                 self._data_status.get("previous_close_date"), execution)
                daily = value.setdefault("audit", {"schema": audit.SCHEMA, "data_feed": audit.DATA_FEED,
                                                  "started_at": session["started_at"],
                                                  "strategy": session["strategy"],
                                                  "previous_close_date": session["previous_close_date"], "sessions": []})
                daily.setdefault("sessions", []).append(session)
                self._dirty = True
            except Exception:
                # Optional diagnostics must never prevent position supervision.
                LOGGER.warning("Could not capture live session evidence")
            self._audit_recorded_days.add(key)
        return value

    def _record_entry_evidence(self, trade: dict, candidate: dict) -> None:
        try:
            symbol = trade["symbol"]
            end = stamp(candidate["window_end"]) if candidate.get("window_end") else at(self._day, self.settings.strategy.early_end)
            bars = self._setup_bars.get(symbol)
            source = "frozen_setup"
            if bars is None:
                bars = tuple(bar for bar in self._bars.get(symbol, {}).values()
                             if at(self._day, self.settings.strategy.early_start) <= bar.timestamp < end)
                source = "current_memory" if bars else "unavailable"
            frozen = {**candidate, "entry_limit": trade["entry_limit"], "entry_deadline": trade["entry_deadline"],
                      "active_at": trade["active_at"], "time_exit": trade["time_exit"],
                      "stop_percent": trade["stop_percent"], "target_percent": trade["target_percent"]}
            evidence = audit.entry_snapshot(
                session_id=self._audit_session_id, now=self.now(), strategy=self.settings.strategy,
                candidate=frozen, previous_close_date=self._data_status.get("previous_close_date"),
                quote=self._fresh_quote(symbol), bars=bars, bars_source=source,
                window_finalized=bool(candidate.get("window_finalized") if candidate.get("late_gap") else self._window_finalized),
                early_window_finalized=self._window_finalized)
            if trade.get("trade_number", 1) == 2:
                primary = self._day_state()["trades"].get(symbol, {}).get("entry_evidence", {})
                if primary.get("setup_bars"):
                    # Re-entry uses the original frozen high, including after a
                    # restart when a REST refresh may contain revised bars.
                    evidence.update(setup_bars=[dict(row) for row in primary["setup_bars"]],
                                    setup_bars_source="primary_entry_evidence",
                                    previous_close=primary.get("previous_close"),
                                    previous_close_date=primary.get("previous_close_date"))
            trade.setdefault("entry_evidence", evidence)
            trade.setdefault("entry_attempt_evidence", []).append(evidence)
            self._dirty = True
        except Exception:
            LOGGER.warning("Could not capture live entry evidence")

    def _record_exit_evidence(self, trade: dict, reason: str, quote: dict | None = None) -> None:
        if trade.get("exit_signal_evidence"):
            return
        try:
            trade["exit_signal_evidence"] = audit.exit_snapshot(
                session_id=self._audit_session_id, now=self.now(), trade=trade, reason=reason,
                quote=quote if quote is not None else self._fresh_quote(trade["symbol"]))
            self._dirty = True
        except Exception:
            LOGGER.warning("Could not capture live exit evidence")

    def _trades(self, *, active: bool = False) -> list[dict]:
        values = [trade for day in self.state["days"].values() for trade in day["trades"].values()]
        return [trade for trade in values if trade["status"] not in {"closed", "skipped", "locate_deferred"}] if active else values

    def _expire_deferred_locates(self) -> None:
        for trade in self._trades():
            if trade["status"] == "locate_deferred" and self.now() >= stamp(trade["entry_deadline"]):
                note = "Entry deadline passed after unpaid locate deferral; no stock order sent"
                trade.update(status="skipped", note=note,
                             locate={**trade.get("locate", {}), "status": "not_purchased", "note": note})
                self._dirty = True

    def _safe_error(self, error: Exception | str) -> str:
        text = str(error)
        for value in (self.settings.data.api_key, self.settings.data.secret_key, self.settings.das.password,
                      self.settings.das.username, self.settings.das.account):
            if value:
                text = text.replace(value, "[redacted]")
        return text[:700]

    def _event(self, message: str, level: str = "info", symbol: str = "") -> None:
        self.state["events"].append({"time": self.now().isoformat(), "level": level,
                                     "message": self._safe_error(message), "symbol": symbol})
        self.state["events"] = self.state["events"][-500:]
        self._dirty = True

    def _persist(self) -> None:
        try:
            self.store.save(self.state)
            self._dirty = False
        except Exception:
            self.entries_enabled = False
            self._entry_block = "Cannot persist live state; order submissions are blocked"
            raise

    def _sync_broker_status(self) -> None:
        """Read the adapter's atomic snapshot without taking its socket lock.

        A worker may disconnect while its coroutine is still waiting to resume.
        Reading the adapter here prevents an old successful probe/entry from
        leaving the dashboard green after that later disconnect.
        """
        if not self.settings.execution_enabled or self.broker is None:
            return
        connection = getattr(self.broker, "connection_status", None)
        if isinstance(connection, dict):
            self._broker_status["connected"] = bool(connection.get("connected")) and not self._broker_status["trading_blocked"]
            for key in ("last_checked_at", "last_connected_at", "last_disconnected_at"):
                if connection.get(key) is not None:
                    self._broker_status[key] = connection[key]
            if connection.get("last_error"):
                self._broker_status["last_error"] = self._safe_error(connection["last_error"])
            elif self._broker_status["connected"]:
                self._broker_status["last_error"] = None
            self._broker_status["checking"] = bool(connection.get("checking") or connection.get("connecting") or
                                                    (self._broker_health_task and not self._broker_health_task.done()))
        connected, checking = self._broker_status["connected"], self._broker_status["checking"]
        if connected:
            self._broker_status["status"] = "Connected; DAS account verified"
        elif self._broker_status["trading_blocked"]:
            self._broker_status["status"] = "DAS account reports trading blocked"
        elif checking:
            self._broker_status["status"] = "Reconnecting; verifying DAS account"
        else:
            self._broker_status["status"] = self._broker_status["last_error"] or "Disconnected; waiting for DAS connection check"
        self._broker_status["next_check_at"] = self._broker_check_at.isoformat() if self._broker_check_at else None

    def _broker_ready(self) -> bool:
        if not self.settings.execution_enabled:
            return True
        self._sync_broker_status()
        return bool(self._broker_status["connected"])

    def _broker_failed(self, error: Exception | str) -> None:
        if not self.settings.execution_enabled:
            return
        connection = getattr(self.broker, "connection_status", None)
        if isinstance(connection, dict) and connection.get("connected"):
            # A rejected order or reconciliation mismatch is not a transport
            # outage; its trade record retains the separate actionable error.
            self._sync_broker_status()
            return
        self._broker_status.update(connected=False, last_error=self._safe_error(error),
                                   last_disconnected_at=self.now().isoformat())
        retry = self.now() + timedelta(seconds=self.settings.das.reconnect_seconds)
        if self._broker_check_at is None or self._broker_check_at > retry:
            self._broker_check_at = retry
        self._sync_broker_status()

    async def _check_broker_health(self) -> None:
        """Read-only account heartbeat. get_account reconnects if needed.

        This does not replay orders, purchase locates, or change the user's
        entry toggle. It runs off the event loop, including when the book is flat.
        """
        if not self.settings.execution_enabled or self.broker is None:
            return
        was_connected = self._broker_ready()
        previous_error = self._broker_status["last_error"]
        self._broker_status["checking"] = True
        self._broker_check_at = None
        try:
            account = await asyncio.to_thread(self.broker.get_account)
            self._broker_status["trading_blocked"] = bool(account.get("trading_blocked"))
            if self._broker_status["trading_blocked"]:
                raise RuntimeError("DAS account reports trading blocked")
            self._broker_status.update(connected=True, last_connected_at=self.now().isoformat(), last_error=None)
            self._sync_broker_status()
            if self._broker_status["connected"] and not was_connected:
                self._event("DAS connection verified; automatic connection checks active")
        except Exception as exc:
            self._broker_status.update(connected=False, last_error=self._safe_error(exc))
            self._broker_failed(exc)
            if was_connected or previous_error != self._broker_status["last_error"]:
                self._event(f"DAS connection check failed; retrying automatically: {self._safe_error(exc)}", "warning")
        finally:
            self._broker_status.update(checking=False, last_checked_at=self.now().isoformat())
            self._sync_broker_status()
            delay = self.settings.das.health_check_seconds if self._broker_status["connected"] else self.settings.das.reconnect_seconds
            self._broker_check_at = self.now() + timedelta(seconds=delay)
            self._broker_status["next_check_at"] = self._broker_check_at.isoformat()
            # The task is not done yet inside its own finally block.
            self._broker_status["checking"] = False

    def _schedule_broker_health(self) -> None:
        if not (self.running and self.settings.execution_enabled and self.broker is not None):
            return
        if self._broker_health_task and not self._broker_health_task.done():
            return
        self._sync_broker_status()
        # A disconnect observed in another worker should not wait for the
        # remaining healthy interval before starting its retry countdown.
        if not self._broker_status["connected"]:
            retry = self.now() + timedelta(seconds=self.settings.das.reconnect_seconds)
            if self._broker_check_at is not None and self._broker_check_at > retry:
                self._broker_check_at = retry
        if self._broker_check_at is None or self.now() >= self._broker_check_at:
            self._broker_health_task = asyncio.create_task(self._check_broker_health(), name="4am-das-health")

    async def open(self) -> None:
        if self._opened:
            return
        self.state = self.store.open()
        self._opened = True
        self._day_state()
        self._load_latest_backtest()
        self._expire_deferred_locates()
        for trade in self._trades(active=True):
            # A crash while arranging borrow must never automatically buy it again.
            if trade["status"] == "locating" and not trade.get("entry_token"):
                note = ("Interrupted re-entry borrow check; no replacement attempt submitted"
                        if trade.get("trade_number", 1) == 2 else
                        "Interrupted locate attempt; inspect DAS locate journal before reusing shares")
                trade.update(status="skipped", note=note)
        self._persist()
        if self._trades(active=True):
            await self._start(allow_entries=False)
            self._event("Restored managed orders/positions; supervising exits with new entries paused", "warning")

    async def start(self) -> None:
        try:
            await self._start(allow_entries=True)
        except Exception as exc:
            self._event(self._safe_error(exc), "error")
            if self._opened:
                self._persist()
            raise

    async def _start(self, *, allow_entries: bool) -> None:
        if self._closing:
            raise RuntimeError("The supervisor is shutting down")
        if not self._opened:
            await self.open()
        if self.running:
            if self._entry_block:
                raise RuntimeError(self._entry_block)
            self.entries_enabled = allow_entries
            self._event("New entries enabled" if allow_entries else "New entries paused")
            return
        if self.data is None and (not self.settings.data.api_key or not self.settings.data.secret_key):
            raise RuntimeError("Configure the Alpaca API key and secret in the v3 .env file")
        if self.settings.execution_enabled and not all((self.settings.das.username, self.settings.das.password, self.settings.das.account)) and self.broker is None:
            raise RuntimeError("Configure the DAS host, username, password and account before enabling orders")
        self.data = self.data or AlpacaData(self.settings.data)
        self.feed = self.feed or SIPFeed(self.settings.data, self.on_event, self.on_status)
        if self.settings.execution_enabled:
            self.broker = self.broker or DasClient(self.settings.das, journal_path=self.settings.state_dir / f"das_{self.settings.identity}.json")
            await self._check_broker_health()
        self.running, self.entries_enabled = True, allow_entries
        self._stop = asyncio.Event()
        self._tasks = [asyncio.create_task(self.feed.run(self._stop), name="4am-sip"),
                       asyncio.create_task(self._supervise(), name="4am-supervisor")]
        self._launch_bootstrap()
        self._event(f"Started {self.settings.mode}; strategy file {self.settings.strategy_config_path.name}")

    def _launch_bootstrap(self) -> None:
        if self._bootstrap_task and not self._bootstrap_task.done():
            return
        self._bootstrap_task = asyncio.create_task(self._bootstrap(), name="4am-discovery")

    def _replace_window(self, history: dict[str, list[Bar]], *, preserve_from: datetime | None = None) -> None:
        # A successful complete REST snapshot is authoritative. Old persisted
        # candidates must not survive missing bars or a missing prior close.
        trades = self._day_state()["trades"]
        # Rebuild every unconsumed setup, including a later setup which may
        # become an original-window qualifier after corrected history arrives.
        self._day_state()["candidates"] = {symbol: row for symbol, row in self._day_state()["candidates"].items() if symbol in trades}
        self._setup_bars = {symbol: bars for symbol, bars in self._setup_bars.items() if symbol in trades}
        # The REST endpoint is exclusive. Preserve later websocket minutes
        # received while the request was in flight, but replace its covered
        # interval so stale bars cannot keep a removed qualification alive.
        retained = {symbol: [bar for bar in bars.values() if preserve_from is not None and bar.timestamp >= preserve_from]
                    for symbol, bars in self._bars.items()}
        self._bars = {}
        for symbol, bars in history.items():
            if symbol in self._closes:
                for bar in bars:
                    self._accept_bar(symbol, bar, historical=True)
        for symbol, bars in retained.items():
            if symbol in self._closes:
                for bar in bars:
                    self._accept_bar(symbol, bar, historical=True)
        self._dirty = True

    async def _bootstrap(self) -> None:
        if self._late_finalize_task and not self._late_finalize_task.done():
            self._late_finalize_task.cancel()
        self._data_status.update(backfill_ready=False, status="Loading Alpaca universe and prior regular closes")
        self._window_finalized = False
        try:
            day = self._day
            result = await self.data.discover(day)
            if self._day != day:
                return
            self._market_day = result.market_day
            self._symbols = [symbol for symbol in result.symbols if not self.settings.symbols or symbol in self.settings.symbols]
            self._closes = {symbol: result.previous_closes[symbol] for symbol in self._symbols if symbol in result.previous_closes}
            self._data_status.update(universe_size=len(self._symbols), previous_close_date=str(result.previous_close_date) if result.previous_close_date else None)
            try:
                daily_audit = self._day_state()["audit"]
                previous_date = self._data_status["previous_close_date"]
                if daily_audit.get("previous_close_date") is None:
                    daily_audit["previous_close_date"] = previous_date
                for session in daily_audit.get("sessions", []):
                    if session.get("session_id") == self._audit_session_id:
                        session["previous_close_date"] = previous_date
                self._dirty = True
            except Exception:
                LOGGER.warning("Could not capture previous-close date evidence")
            for warning in result.warnings[:10]:
                self._event(warning, "warning")
            if not self._market_day:
                self._replace_window({})
                self._data_status.update(status="Exchange closed for this Eastern date", backfill_ready=True)
                return
            rules = self.settings.strategy
            current = self.now().astimezone(EASTERN)
            discovery_end = rules.entry_deadline if rules.late_gap_enabled else rules.early_end
            end = min(at(day, discovery_end), current.replace(second=0, microsecond=0))
            if end > at(day, rules.early_start):
                history = await self.data.backfill(self._symbols, day, rules.early_start, end.strftime("%H:%M"))
                if self._day != day:
                    return
                if rules.late_gap_enabled:
                    self._replace_window(history, preserve_from=end)
                elif end == at(day, rules.early_end):
                    self._replace_window(history)
                else:
                    # Preserve websocket minutes arriving while the partial
                    # startup request was in flight, and revalidate candidates.
                    self._day_state()["candidates"] = {s: row for s, row in self._day_state()["candidates"].items() if s in self._day_state()["trades"]}
                    for symbol, bars in history.items():
                        for bar in bars:
                            self._accept_bar(symbol, bar, historical=True)
            for symbol in list(self._bars):
                self._rebuild_candidate(symbol)
            if rules.late_gap_enabled:
                self._mark_late_windows_finalized(end, verified_at=current)
            self._data_status.update(backfill_ready=True, status="SIP discovery ready; collecting premarket setups" if rules.late_gap_enabled else "SIP discovery ready; collecting the early window")
            self._bootstrap_retry_at = None
            if current >= at(day, rules.early_end) + timedelta(seconds=self.settings.final_bar_wait_seconds):
                self._window_finalized = True
                self._data_status["status"] = "Early window loaded; scanning later gaps and watching entries and exits" if rules.late_gap_enabled else "Early window loaded; watching entries and exits"
            await self._sync_subscriptions()
            self._persist()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._data_status.update(backfill_ready=False, status=self._safe_error(exc))
            self._event(f"Discovery/backfill failed: {self._safe_error(exc)}", "error")
            self._bootstrap_retry_at = self.now() + timedelta(seconds=max(5, self.settings.data.reconnect_seconds))

    async def _finalize_window(self) -> None:
        try:
            rules = self.settings.strategy
            day = self._day
            history = await self.data.backfill(self._symbols, day, rules.early_start, rules.early_end)
            if self._day != day:
                return
            self._replace_window(history, preserve_from=at(day, rules.early_end) if rules.late_gap_enabled else None)
            self._window_finalized = True
            self._finalize_retry_at = None
            self._data_status["status"] = "Early window finalized; scanning later gaps and watching entries and exits" if rules.late_gap_enabled else "Early window finalized; watching entries and exits"
            self._event("Early window finalized from Alpaca SIP bars")
            await self._sync_subscriptions()
            self._persist()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._data_status["status"] = "Final early-window backfill failed; entries paused"
            self._event(self._safe_error(exc), "error")
            self._finalize_retry_at = self.now() + timedelta(seconds=max(5, self.settings.data.reconnect_seconds))

    def _mark_late_windows_finalized(self, covered_until: datetime, *, symbol: str | None = None,
                                     covered_from: datetime | None = None, verified_at: datetime | None = None) -> None:
        """Authorize only complete observation windows verified by REST."""
        for candidate in self._day_state()["candidates"].values():
            if not candidate.get("late_gap") or (symbol is not None and candidate["symbol"] != symbol):
                continue
            end = stamp(candidate["window_end"])
            if (end <= covered_until and (covered_from is None or stamp(candidate["first_gap_time"]) >= covered_from)
                    and (verified_at or self.now()) >= end + timedelta(seconds=self.settings.final_bar_wait_seconds)):
                candidate["window_finalized"] = True
                self._dirty = True

    async def _finalize_late_windows(self) -> None:
        """Refresh each mature later setup before permitting its first entry."""
        day = self._day
        try:
            for pending in list(self._day_state()["candidates"].values()):
                if not pending.get("late_gap") or pending.get("window_finalized"):
                    continue
                symbol = pending["symbol"]
                start, end = stamp(pending["first_gap_time"]), stamp(pending["window_end"])
                if (symbol in self._day_state()["trades"] or self.now() >= self._candidate_deadline(pending)
                        or self.now() < end + timedelta(seconds=self.settings.final_bar_wait_seconds)):
                    continue
                history = await self.data.backfill([symbol], day, start.strftime("%H:%M"), end.strftime("%H:%M"))
                if self._day != day:
                    return
                if symbol in self._day_state()["trades"]:
                    continue
                self._day_state()["candidates"].pop(symbol, None)
                self._bars[symbol] = {key: bar for key, bar in self._bars.get(symbol, {}).items()
                                      if not start <= bar.timestamp < end}
                for bar in history.get(symbol, []):
                    self._accept_bar(symbol, bar, historical=True)
                self._rebuild_candidate(symbol)
                self._mark_late_windows_finalized(end, symbol=symbol, covered_from=start)
            self._late_finalize_retry_at = None
            await self._sync_subscriptions()
            self._persist()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._event(f"Later setup backfill failed; its entries remain paused: {self._safe_error(exc)}", "error")
            self._late_finalize_retry_at = self.now() + timedelta(seconds=max(5, self.settings.data.reconnect_seconds))

    async def on_status(self, status: dict) -> None:
        was_ready = bool(self._data_status.get("ready"))
        self._data_status.update(status)
        ready = bool(self._data_status.get("ready"))
        if status.get("error"):
            self._data_status["status"] = self._safe_error(status["error"])
        elif not ready:
            self._data_status["status"] = "SIP connecting/authenticating" if status.get("connected") else "SIP disconnected; reconnecting" if self.running else "Stopped"
        if was_ready and not ready:
            self._data_status["backfill_ready"] = False
            self._event("SIP disconnected; pending entries will be canceled while existing positions remain supervised", "warning")
            for trade in self._trades(active=True):
                if not trade.get("entry_terminal"):
                    trade["cancel_entry"] = True
        if ready and not was_ready and self.running:
            self._launch_bootstrap()

    async def on_event(self, event: dict) -> None:
        try:
            kind, symbol = event.get("T"), event.get("S", "")
            if not symbol:
                return
            timestamp = stamp(event["t"])
            if timestamp > self.now() + timedelta(seconds=self.settings.data.future_tolerance_seconds):
                return
            if kind in {"b", "u"}:
                prices = [_positive(event[key]) for key in ("o", "h", "l", "c")]
                if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
                    return
                self._accept_bar(symbol, Bar(timestamp, *prices, float(event.get("v", 0))))
            elif kind == "q":
                bid, ask = _positive(event["bp"]), _positive(event["ap"])
                if ask < bid or float(event.get("bs", 0)) <= 0 or float(event.get("as", 0)) <= 0:
                    return
                prior = self._quotes.get(symbol)
                if prior and timestamp < stamp(prior["timestamp"]):
                    return
                self._quotes[symbol] = {"bid": bid, "ask": ask, "timestamp": timestamp.isoformat()}
                # Remember a stop/target touch even if the quote changes while a
                # serialized DAS request is running in the supervisor.
                for trade in self._trades(active=True):
                    if trade["symbol"] == symbol:
                        self._cancel_entry_outside_price_range(trade, self._fresh_quote(symbol))
                    if trade["symbol"] == symbol and trade["entry_filled_qty"] and not trade.get("exit_reason"):
                        reason = self._exit_signal(trade)
                        if reason:
                            trade.update(exit_reason=reason, cancel_entry=True)
                            self._dirty = True
            elif kind == "t":
                candidate = self._day_state()["candidates"].get(symbol)
                if candidate:
                    candidate["last_trade"] = _positive(event["p"])
        except (KeyError, ValueError, TypeError, OverflowError):
            return

    def _accept_bar(self, symbol: str, bar: Bar, *, historical: bool = False) -> None:
        timestamp = bar.timestamp.astimezone(EASTERN)
        rules = self.settings.strategy
        if timestamp.date() != self._day or timestamp.second or timestamp.microsecond:
            return
        discovery_end = rules.entry_deadline if rules.late_gap_enabled else rules.early_end
        if not at(self._day, rules.early_start) <= timestamp < at(self._day, discovery_end):
            return
        if timestamp + timedelta(minutes=1) > self.now():
            return
        if self._window_finalized and not historical and (not rules.late_gap_enabled or timestamp < at(self._day, rules.early_end)):
            return
        self._bars.setdefault(symbol, {})[timestamp.isoformat()] = bar
        self._rebuild_candidate(symbol)

    def _rebuild_candidate(self, symbol: str) -> None:
        previous = self._closes.get(symbol)
        if previous is None or symbol in self._day_state()["trades"]:
            return
        existing = self._day_state()["candidates"].get(symbol, {})
        if existing.get("late_gap") and existing.get("window_finalized"):
            return
        rules = self.settings.strategy
        bars = sorted(self._bars.get(symbol, {}).values(), key=lambda bar: bar.timestamp)
        threshold = percent(previous, rules.gap_percent)
        setup = select_setup(self._day, bars, previous, rules)
        if setup is None:
            self._day_state()["candidates"].pop(symbol, None)
            self._setup_bars.pop(symbol, None)
            return
        high_bar, high_time, active = setup.high_bar, setup.high_time, setup.active_at
        high = high_bar.high
        candidate = {**existing, "symbol": symbol, "date": str(self._day), "previous_close": previous,
                     "gap_threshold": threshold, "first_gap_time": setup.first_gap_bar.timestamp.isoformat(),
                     "first_gap_bar_high": setup.first_gap_bar.high, "early_high": high, "late_gap": setup.late_gap,
                     "early_high_bar_time": high_bar.timestamp.isoformat(), "early_high_time": high_time.isoformat(),
                     "window_end": setup.window_end.isoformat(), "window_finalized": False,
                     "active_at": active.isoformat(), "entry_limit": order_price(percent(high, -rules.entry_below_high_percent)),
                     "status": "waiting", "note": "Waiting for the high delay" if setup.late_gap else "Waiting for the early window and high delay", "locate": {}}
        self._day_state()["candidates"][symbol] = candidate
        self._setup_bars[symbol] = tuple(bar for bar in bars
                                         if at(self._day, rules.early_start) <= bar.timestamp < setup.window_end)
        self._dirty = True

    def _fresh_quote(self, symbol: str) -> dict | None:
        quote = self._quotes.get(symbol)
        if not quote:
            return None
        age = (self.now() - stamp(quote["timestamp"])).total_seconds()
        if age < -self.settings.data.future_tolerance_seconds or age > self.settings.data.quote_max_age_seconds:
            return None
        return quote

    def _entry_price_block_reason(self, price: float, label: str) -> str | None:
        rules = self.settings.strategy
        if rules.entry_price_allowed(price):
            return None
        if rules.min_entry_price is not None and price < rules.min_entry_price:
            return f"{label} ${price:g} below minimum entry price ${rules.min_entry_price:g}"
        if rules.max_entry_price is not None and price > rules.max_entry_price:
            return f"{label} ${price:g} above maximum entry price ${rules.max_entry_price:g}"
        return f"{label} is not a valid entry price"

    def _cancel_entry_outside_price_range(self, trade: dict, quote: dict | None = None) -> None:
        """Latch cancellation of the unfilled remainder; never discard fills."""
        if trade.get("entry_terminal") or trade["status"] == "locating":
            return
        reason = self._entry_price_block_reason(trade["entry_limit"], "Entry limit")
        if not reason and quote:
            reason = self._entry_price_block_reason(quote["bid"], "Bid")
        if reason and not trade.get("entry_cancel_reason"):
            trade.update(cancel_entry=True, entry_cancel_reason=reason)
            self._dirty = True

    async def _sync_subscriptions(self) -> None:
        symbols = {trade["symbol"] for trade in self._trades(active=True)}
        if self.now() < at(self._day, self.settings.strategy.entry_deadline):
            symbols.update(self._day_state()["candidates"])
        for primary in list(self._day_state()["trades"].values()):
            candidate = self._reentry_candidate(primary)
            if candidate and self.now() < stamp(candidate["entry_deadline"]):
                symbols.add(primary["symbol"])
        if self.feed and symbols != self._subscriptions:
            await self.feed.set_symbols(symbols)
            self._subscriptions = symbols

    def _eligibility(self, candidate: dict, *, locate: bool = False) -> dict:
        """Capture the exact gate and quote used, including during a DAS wait."""
        now = self.now()
        quote = self._quotes.get(candidate["symbol"])
        age = (now - stamp(quote["timestamp"])).total_seconds() if quote else None
        trigger = self._locate_trigger_price(candidate) if locate and candidate.get("trade_number", 1) == 1 else None
        details = {"checked_at": now.isoformat(), "bid": quote["bid"] if quote else None,
                   "ask": quote["ask"] if quote else None,
                   "quote_timestamp": quote["timestamp"] if quote else None,
                   "quote_age_seconds": round(age, 3) if age is not None else None,
                   "trigger_price": trigger, "min_entry_price": self.settings.strategy.min_entry_price,
                   "max_entry_price": self.settings.strategy.max_entry_price, "reason": None}

        def blocked(reason: str) -> dict:
            return {**details, "reason": reason}

        if not self.running or self._closing:
            return blocked("Supervisor stopped or shutting down")
        if not self.entries_enabled:
            return blocked("New entries paused")
        if self._entry_block:
            return blocked(self._entry_block)
        if not self._market_day:
            return blocked("Market session not ready")
        if not self._window_finalized or (candidate.get("late_gap") and not candidate.get("window_finalized", False)):
            return blocked("Setup window not finalized")
        if not self._data_status.get("ready") or not self._data_status.get("backfill_ready"):
            return blocked("SIP feed or historical backfill not ready")
        if not self._broker_ready():
            return blocked("DAS connection not verified")
        if candidate["date"] != str(now.astimezone(EASTERN).date()):
            return blocked("Setup belongs to a different trading date")
        if candidate["symbol"] not in self._closes or candidate["symbol"] not in self._symbols:
            return blocked("Symbol or previous close unavailable")
        if candidate.get("trade_number", 1) == 1 and candidate["symbol"] not in self._day_state()["trades"]:
            current = self._day_state()["candidates"].get(candidate["symbol"])
            if current is None or any(current.get(key) != candidate.get(key) for key in (
                    "early_high", "active_at", "entry_limit", "first_gap_time", "window_end", "window_finalized", "late_gap")):
                return blocked("Setup changed before entry")
        if now >= self._candidate_deadline(candidate):
            return blocked("Entry deadline passed")
        if now < stamp(candidate["active_at"]):
            return blocked("Waiting for the setup window and high delay")
        price_reason = self._entry_price_block_reason(candidate["entry_limit"], "Entry limit")
        if price_reason:
            return blocked(price_reason)
        if not quote:
            return blocked("No SIP quote available")
        if age < -self.settings.data.future_tolerance_seconds:
            return blocked(f"SIP quote is {-age:.2f}s in the future")
        if age > self.settings.data.quote_max_age_seconds:
            return blocked(f"SIP quote is {age:.2f}s old; maximum {self.settings.data.quote_max_age_seconds:g}s")
        price_reason = self._entry_price_block_reason(quote["bid"], "Bid")
        if price_reason:
            return blocked(price_reason)
        if candidate.get("trade_number", 1) == 2:
            primary = self._day_state()["trades"].get(candidate["symbol"])
            if not self.settings.strategy.reentry.enabled or not primary or not self._primary_stopped_flat(primary):
                return blocked("Initial stop-loss position is not confirmed flat or re-entry is disabled")
            flat_at = primary.get("flat_confirmed_at") or primary.get("exit_time")
            if not flat_at or stamp(quote["timestamp"]) < stamp(flat_at):
                return blocked("Waiting for a quote after the initial position closed")
        if trigger is not None and Decimal(str(quote["bid"])) < Decimal(str(trigger)):
            return blocked(f"Bid ${quote['bid']:g} below locate trigger ${trigger:g}")
        return details

    def _can_enter(self, candidate: dict) -> bool:
        return self._eligibility(candidate)["reason"] is None

    def _locate_trigger_price(self, candidate: dict) -> float | None:
        distance = self.settings.locate_trigger_below_entry_percent
        return None if distance is None else percent(candidate["entry_limit"], -distance)

    def _can_locate(self, candidate: dict) -> bool:
        """Delay the borrow attempt until the bid approaches the sell limit.

        This gate also runs immediately before a paid acceptance/purchase. Once
        borrow is confirmed, the ordinary entry rules control the resting order;
        a later price fade must not discard borrow we have already paid for.
        """
        return self._eligibility(candidate, locate=True)["reason"] is None

    async def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event(f"Supervisor: {self._safe_error(exc)}", "error")
                self._broker_status["status"] = self._safe_error(exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.poll_seconds)
            except TimeoutError:
                pass

    async def tick(self) -> None:
        async with self._tick_lock:
            self._schedule_broker_health()
            today = self.now().astimezone(EASTERN).date()
            if today != self._day:
                for task in (self._bootstrap_task, self._finalize_task, self._late_finalize_task):
                    if task and not task.done():
                        task.cancel()
                self._bootstrap_task = self._finalize_task = self._late_finalize_task = None
                self._bootstrap_retry_at = self._finalize_retry_at = self._late_finalize_retry_at = None
                self._day = today
                self._bars, self._closes, self._symbols = {}, {}, []
                self._setup_bars = {}
                self._window_finalized = False
                self._market_day = False
                self._data_status["backfill_ready"] = False
                self._data_status["previous_close_date"] = None
                self._day_state()
                self._launch_bootstrap()
            if self.running and not self._closing and not self._data_status.get("backfill_ready") and (self._bootstrap_retry_at is None or self.now() >= self._bootstrap_retry_at):
                self._launch_bootstrap()
            self._expire_deferred_locates()
            # Exit supervision always runs, including after Stop Entries.
            for trade in self._trades(active=True):
                if trade["status"] == "locating":
                    continue
                try:
                    if self.settings.execution_enabled:
                        if not self._broker_ready():
                            continue
                        await self._manage_broker_trade(trade)
                    else:
                        self._manage_monitor_trade(trade)
                except Exception as exc:
                    trade["note"] = self._safe_error(exc)
                    trade["reconciliation_required"] = True
                    trade["status"] = "uncertain"
                    self._event(f"Reconciliation required: {self._safe_error(exc)}", "error", trade["symbol"])
                    self._broker_failed(exc)
            if self.settings.execution_enabled and self.broker and self._broker_ready() and not self._closing and (self._entry_task is None or self._entry_task.done()):
                if (self._locate_service_task is None or self._locate_service_task.done()) and (self._locate_service_at is None or self.now() >= self._locate_service_at):
                    self._locate_service_at = self.now() + timedelta(seconds=10)
                    self._locate_service_task = asyncio.create_task(self._service_locates(), name="4am-locate-offers")
            if self._market_day and self._data_status.get("backfill_ready") and not self._window_finalized:
                finish = at(self._day, self.settings.strategy.early_end) + timedelta(seconds=self.settings.final_bar_wait_seconds)
                if self.now() >= finish and (self._finalize_retry_at is None or self.now() >= self._finalize_retry_at) and (self._finalize_task is None or self._finalize_task.done()) and (self._bootstrap_task is None or self._bootstrap_task.done()):
                    self._finalize_task = asyncio.create_task(self._finalize_window(), name="4am-final-bars")
            if (self.settings.strategy.late_gap_enabled and self._market_day and self._data_status.get("backfill_ready")
                    and not self._closing and self.now() < at(self._day, self.settings.strategy.entry_deadline)
                    and (self._late_finalize_retry_at is None or self.now() >= self._late_finalize_retry_at)
                    and all(task is None or task.done() for task in (self._bootstrap_task, self._finalize_task, self._late_finalize_task))):
                mature = any(candidate.get("late_gap") and not candidate.get("window_finalized")
                             and candidate["symbol"] not in self._day_state()["trades"]
                             and self.now() >= stamp(candidate["window_end"]) + timedelta(seconds=self.settings.final_bar_wait_seconds)
                             for candidate in self._day_state()["candidates"].values())
                if mature:
                    self._late_finalize_task = asyncio.create_task(self._finalize_late_windows(), name="4am-later-final-bars")
            await self._sync_subscriptions()
            candidates = list(self._day_state()["candidates"].values())
            candidates += [candidate for primary in list(self._day_state()["trades"].values())
                           if (candidate := self._reentry_candidate(primary)) is not None]
            for candidate in candidates:
                previous = self._day_state()["trades"].get(self._trade_key(candidate))
                if previous and previous["status"] != "locate_deferred":
                    continue
                if self.now() >= self._candidate_deadline(candidate):
                    candidate.update(status="expired", note="Entry deadline passed")
                    continue
                if previous and self.now() < stamp(previous["locate_retry_after"]):
                    continue
                price_reason = self._entry_price_block_reason(candidate["entry_limit"], "Entry limit")
                if price_reason:
                    candidate.update(status="price_filtered", note=price_reason)
                    continue
                quote = self._fresh_quote(candidate["symbol"])
                price_reason = self._entry_price_block_reason(quote["bid"], "Bid") if quote else None
                if price_reason:
                    candidate.update(status="waiting_for_price", note=price_reason)
                    continue
                if not self._can_enter(candidate):
                    candidate["status"] = "waiting"
                    candidate["note"] = self._entry_block or ("Entries paused" if not self.entries_enabled else
                        "Waiting for verified DAS connection; automatic reconnect active" if not self._broker_ready() else
                        "Waiting for window, high delay, SIP readiness or fresh quote")
                    continue
                if not self._can_locate(candidate):
                    trigger = self._locate_trigger_price(candidate)
                    note = f"Waiting for bid >= ${trigger:g} before checking borrow or buying locates" if trigger is not None else "Waiting for a fresh quote before checking borrow"
                    candidate.update(status="waiting_for_price", note=note)
                    continue
                if self._entry_task and not self._entry_task.done():
                    continue
                if any(trade.get("reconciliation_required") for trade in self._trades(active=True)):
                    candidate["note"] = "New entries paused while a managed order requires reconciliation"
                    continue
                if self.settings.max_positions and len(self._trades(active=True)) >= self.settings.max_positions:
                    candidate["note"] = "Position limit reached"
                    continue
                self._entry_symbol = candidate["symbol"]
                self._entry_task = asyncio.create_task(self._enter(candidate), name=f"4am-enter-{candidate['symbol']}")
                break
            if self._dirty:
                self._persist()

    async def _service_locates(self) -> None:
        try:
            await asyncio.to_thread(self.broker.service_locate_offers)
        except Exception as exc:
            self._broker_failed(exc)
            self._event(f"Locate offer reconciliation: {self._safe_error(exc)}", "warning")

    @staticmethod
    def _trade_key(candidate: dict) -> str:
        return candidate["symbol"] + (":reentry" if candidate.get("trade_number", 1) == 2 else "")

    def _candidate_deadline(self, candidate: dict) -> datetime:
        return stamp(candidate["entry_deadline"]) if candidate.get("entry_deadline") else at(self._day, self.settings.strategy.entry_deadline)

    def _primary_stopped_flat(self, trade: dict) -> bool:
        if (trade.get("trade_number", 1) != 1 or trade.get("date") != str(self._day)
                or trade.get("status") != "closed" or trade.get("exit_reason") != "stop_loss"
                or not trade.get("entry_filled_qty") or trade.get("remaining_qty") != 0
                or not trade.get("entry_terminal") or trade.get("reconciliation_required")):
            return False
        covers = trade.get("cover_orders", [])
        return (not self.settings.execution_enabled or
                (sum(row.get("filled_qty", 0) for row in covers) == trade["entry_filled_qty"]
                 and all(row.get("status") in TERMINAL_STATUSES for row in covers)))

    def _reentry_candidate(self, primary: dict) -> dict | None:
        rules = self.settings.strategy.reentry
        if not rules.enabled or not self._primary_stopped_flat(primary):
            return None
        symbol = primary["symbol"]
        if symbol + ":reentry" in self._day_state()["trades"]:
            return None
        original = self._day_state()["candidates"].get(symbol, {})
        # Legacy primary records stored the early high only on the candidate.
        # Never derive a new high from prices observed after the early window.
        high = primary.get("early_high", original.get("early_high"))
        if not high or not primary.get("exit_time"):
            return None
        return {**original, "symbol": symbol, "date": primary["date"], "trade_number": 2,
                "early_high": high, "active_at": primary["exit_time"],
                "entry_limit": order_price(percent(high, rules.entry_above_high_percent)),
                "entry_deadline": at(self._day, rules.entry_deadline).isoformat(),
                "time_exit": at(self._day, rules.time_exit).isoformat(),
                "stop_percent": rules.stop_loss_percent, "target_percent": rules.profit_target_percent,
                "requested_qty": primary["requested_qty"], "status": "waiting",
                "note": "Stopped out; waiting to submit re-entry with existing borrow", "locate": {}}

    async def _verify_primary_closed(self, primary: dict) -> None:
        """Reconfirm all original orders after restart and before a second short."""
        if not self._primary_stopped_flat(primary):
            raise OrderSubmissionUncertain("Primary stop-loss is not fully closed and reconciled")
        intents = [(primary["entry_token"], "sell", primary["requested_qty"], primary["entry_filled_qty"])]
        intents += [(row["token"], "buy", row["qty"], row["filled_qty"])
                    for row in primary["cover_orders"] if not row.get("definitive_rejection")]
        for token, side, qty, filled in intents:
            order = await asyncio.to_thread(self.broker.get_order_by_client_id, token)
            if order is None:
                raise OrderSubmissionUncertain("Primary order could not be reconfirmed before re-entry")
            self._validate_owned_order(order, token, primary["symbol"], side, qty)
            if order["status"] not in TERMINAL_STATUSES or int(order.get("filled_qty", 0)) != filled:
                raise OrderSubmissionUncertain("Primary order is not terminal or its fills changed; re-entry blocked")

    def _new_trade(self, candidate: dict) -> dict:
        rules = self.settings.strategy
        return {"symbol": candidate["symbol"], "date": candidate["date"], "status": "locating",
                "trade_number": candidate.get("trade_number", 1), "early_high": candidate["early_high"],
                "active_at": candidate["active_at"],
                "requested_qty": candidate.get("requested_qty", rules.shares), "entry_limit": candidate["entry_limit"], "entry_token": "", "entry_order_id": "",
                "entry_filled_qty": 0, "entry_avg_price": None, "entry_time": "", "entry_terminal": False,
                "entry_deadline": self._candidate_deadline(candidate).isoformat(),
                "time_exit": candidate.get("time_exit", at(self._day, rules.time_exit).isoformat()),
                "stop_percent": candidate.get("stop_percent", rules.stop_loss_percent),
                "target_percent": candidate.get("target_percent", rules.profit_target_percent),
                "stop_price": None, "target_price": None, "remaining_qty": 0, "cover_orders": [],
                "exit_reason": "", "exit_time": "", "exit_avg_price": None, "realized_pnl": None,
                "locate": {}, "cancel_entry": False, "note": "Checking account and borrow", "reconciliation_required": False}

    async def _enter(self, candidate: dict) -> None:
        symbol = candidate["symbol"]
        key = self._trade_key(candidate)
        reentry = candidate.get("trade_number", 1) == 2
        previous = self._day_state()["trades"].get(key)
        if not self._can_locate(candidate):
            return
        if previous and (previous["status"] != "locate_deferred" or reentry
                         or previous.get("entry_token") or previous.get("entry_filled_qty")
                         or previous.get("reconciliation_required")
                         or self.now() >= stamp(previous["entry_deadline"])
                         or self.now() < stamp(previous["locate_retry_after"])):
            return
        trade = self._new_trade(candidate)
        if previous:
            # A retry continues the same frozen setup and original deadline.
            trade = {**previous, "status": "locating", "note": "Rechecking account and unpaid locate", "cancel_entry": False}
        self._day_state()["trades"][key] = trade
        candidate["status"] = "locating"
        self._record_entry_evidence(trade, candidate)
        self._persist()  # Reserve the stock/day before any possible locate charge.
        locate_started = False
        eligibility: dict = {}
        entry_candidate = {**candidate, "entry_deadline": trade["entry_deadline"],
                           "active_at": trade["active_at"], "entry_limit": trade["entry_limit"]}

        def still_valid() -> bool:
            nonlocal eligibility
            eligibility = self._eligibility(entry_candidate, locate=True)
            return eligibility["reason"] is None

        try:
            if not self.settings.execution_enabled:
                trade.update(status="entry_pending", note="Monitor mode: simulated resting sell limit", locate={"status": "not_requested", "note": "Monitor mode never connects to DAS"})
                self._persist()
                self._manage_monitor_trade(trade)
                return
            account = await asyncio.to_thread(self.broker.get_account)
            position = await asyncio.to_thread(self.broker.position_qty, symbol)
            orders = await asyncio.to_thread(self.broker.list_open_orders, symbol)
            if account.get("trading_blocked") or position != 0 or orders:
                trade.update(status="skipped", note="Existing broker position/order or blocked account; no locate requested")
                return
            if reentry:
                await self._verify_primary_closed(self._day_state()["trades"][symbol])
            if not still_valid():
                # Account checks are read-only; no borrow request has begun.
                raise LocateDeferred("Entry conditions changed before borrowing; no locate requested")
            if reentry:
                success, note = await asyncio.to_thread(self.broker.validate_shortable, symbol, trade["requested_qty"])
                route, fee = "existing", 0.0
                note = f"Re-entry: {note}; no new locate purchase"
            else:
                locate_started = True
                success, note, route, fee = await asyncio.to_thread(
                    self.broker.ensure_shortable, symbol, trade["requested_qty"], self.settings.max_locate_price,
                    still_valid=still_valid)
            trade["locate"] = {"status": ("reused" if reentry else "available") if success else "unavailable", "note": note,
                               "route": route, "fee_per_share": fee,
                               "comparisons": getattr(self.broker, "locate_comparisons", {}).get(symbol, [])}
            candidate["locate"] = trade["locate"]
            self._persist()
            if not success:
                trade.update(status="skipped", note=note)
                return
            # Locate latency must not extend the entry deadline or a pause.
            if not self._can_enter(entry_candidate):
                trade.update(status="skipped", note=f"{self._eligibility(entry_candidate)['reason']}; entry conditions changed after borrowing; locate may remain unused")
                return
            position = await asyncio.to_thread(self.broker.position_qty, symbol)
            orders = await asyncio.to_thread(self.broker.list_open_orders, symbol)
            if position != 0 or orders or not self._can_enter(entry_candidate):
                trade.update(status="skipped", note="Account or quote changed before submission; no stock order sent")
                return
            routes = [route for route in (self.settings.das.route, self.settings.das.backup_route) if route]
            for route_index, route in enumerate(routes):
                if trade["cancel_entry"] or not self._can_enter(entry_candidate):
                    note = trade.get("entry_cancel_reason") or self._eligibility(entry_candidate)["reason"] or "Entry canceled"
                    trade.update(status="skipped", note=f"{note}; no stock order sent")
                    return
                token = self.broker.new_client_order_id()
                trade.update(entry_token=token, entry_route=route, status="entry_pending", note=f"Submitting resting sell limit via {route}")
                self._persist()
                try:
                    order = await asyncio.to_thread(self.broker.submit_limit_order, symbol=symbol, qty=trade["requested_qty"],
                                                    side="sell", limit_price=trade["entry_limit"], client_order_id=token, route=route,
                                                    still_valid=lambda: not trade["cancel_entry"] and self._can_enter(entry_candidate))
                    self._apply_entry_order(trade, order)
                    label = "Re-entry short" if reentry else "Short"
                    self._event(f"{label} limit submitted at ${trade['entry_limit']:g}", symbol=symbol)
                    break
                except OrderRejected as exc:
                    trade.setdefault("rejected_entry_tokens", []).append(token)
                    trade.update(entry_token="", entry_terminal=True, note=self._safe_error(exc))
                    self._persist()
                    if route_index == len(routes) - 1:
                        trade["status"] = "skipped"
                        return
                    if await asyncio.to_thread(self.broker.position_qty, symbol) != 0 or await asyncio.to_thread(self.broker.list_open_orders, symbol):
                        raise OrderSubmissionUncertain("Cannot verify a flat book before entry route fallback")
                    trade["entry_terminal"] = False
                    self._event(f"Definitive unfilled rejection on {route}; trying the configured backup route", "warning", symbol)
        except LocateDeferred as exc:
            # This typed result proves no paid locate command was sent. Other
            # failures, especially missing acknowledgments, never reach here.
            reason = self._safe_error(eligibility.get("reason") or exc)
            retry_at = self.now() + timedelta(seconds=LOCATE_RETRY_SECONDS)
            retry = (not reentry and not trade.get("entry_token") and not trade["entry_filled_qty"]
                     and not trade.get("reconciliation_required")
                     and retry_at < stamp(trade["entry_deadline"]))
            note = f"{reason}; no locate purchase or stock order sent. " + (
                f"Retry after {retry_at:%H:%M:%S} ET when entry conditions recover" if retry else "No automatic retry available")
            locate = {"status": "deferred" if retry else "not_purchased", "note": note,
                      "requested": locate_started, "eligibility": dict(eligibility),
                      "comparisons": list(getattr(self.broker, "locate_comparisons", {}).get(symbol, [])) if locate_started else []}
            trade.setdefault("locate_deferrals", []).append({"time": self.now().isoformat(), **locate})
            trade.update(status="locate_deferred" if retry else "skipped", note=note, locate=locate,
                         locate_retry_after=retry_at.isoformat())
            self._event(note, "warning", symbol)
        except Exception as exc:
            trade["note"] = self._safe_error(exc)
            if locate_started and trade.get("locate", {}).get("status") not in {"available", "reused", "unavailable"}:
                trade["locate"] = {"status": "failed", "note": trade["note"], "requested": True,
                                   "eligibility": dict(eligibility),
                                   "comparisons": list(getattr(self.broker, "locate_comparisons", {}).get(symbol, []))}
            self._broker_failed(exc)
            if trade.get("entry_token"):
                trade.update(status="uncertain", reconciliation_required=True)
            else:
                trade["status"] = "skipped"
            self._event(trade["note"], "error", symbol)
        finally:
            candidate.update(status=trade["status"], note=trade["note"], locate=trade["locate"])
            self._dirty = True
            self._persist()

    def _apply_entry_order(self, trade: dict, order: dict) -> None:
        self._validate_owned_order(order, trade["entry_token"], trade["symbol"], "sell", trade["requested_qty"])
        filled = int(order.get("filled_qty", 0))
        if filled < trade["entry_filled_qty"] or filled > trade["requested_qty"]:
            raise OrderSubmissionUncertain("Entry fill quantity changed inconsistently")
        trade["entry_order_id"] = str(order["id"])
        trade["entry_terminal"] = order["status"] in TERMINAL_STATUSES
        if filled:
            average = _positive(order["filled_avg_price"])
            if not trade["entry_time"]:
                trade["entry_time"] = self.now().isoformat()
            if order.get("first_fill_time"):
                trade["entry_time"] = stamp(order["first_fill_time"]).isoformat()
            trade["entry_last_fill_time"] = order.get("last_fill_time") or trade.get("entry_last_fill_time") or trade["entry_time"]
            trade.update(entry_filled_qty=filled, entry_avg_price=average,
                         stop_price=percent(average, trade["stop_percent"]), target_price=percent(average, -trade["target_percent"]))
            if order.get("last_fill_time") and stamp(order["last_fill_time"]) >= stamp(trade["entry_deadline"]):
                if not trade.get("exit_reason"):
                    self._record_exit_evidence(trade, "late_entry_fill")
                trade.update(late_entry_fill=True, cancel_entry=True, exit_reason=trade.get("exit_reason") or "late_entry_fill")
        if trade["entry_terminal"] and not filled:
            reason = f"; {trade['entry_cancel_reason']}" if trade.get("entry_cancel_reason") else ""
            trade.update(status="skipped", note=f"Entry {order['status']} with no fills{reason}")
        elif filled:
            trade["status"] = "open"
        trade["remaining_qty"] = filled - sum(item.get("filled_qty", 0) for item in trade["cover_orders"])

    @staticmethod
    def _validate_owned_order(order: dict, token: str, symbol: str, side: str, qty: int) -> None:
        if str(order.get("client_order_id")) != str(token) or order.get("symbol") != symbol or order.get("side") != side or int(order.get("qty", 0)) != qty:
            raise OrderSubmissionUncertain("DAS order does not match its durable strategy intent")

    def _exit_signal(self, trade: dict) -> str:
        if trade.get("exit_reason"):
            return trade["exit_reason"]
        if self.now() >= stamp(trade["time_exit"]):
            self._record_exit_evidence(trade, "time_exit")
            return "time_exit"
        quote = self._fresh_quote(trade["symbol"])
        if quote and trade["entry_filled_qty"]:
            filled_at = trade.get("entry_last_fill_time") or trade.get("entry_time")
            if filled_at and stamp(quote["timestamp"]) < stamp(filled_at):
                return ""  # A cached price before this entry cannot trigger its stop/target.
            if quote["ask"] >= trade["stop_price"]:
                self._record_exit_evidence(trade, "stop_loss", quote)
                return "stop_loss"
            if quote["ask"] <= trade["target_price"]:
                self._record_exit_evidence(trade, "profit_target", quote)
                return "profit_target"
        return ""

    async def _manage_broker_trade(self, trade: dict) -> None:
        if not trade.get("entry_token"):
            return
        try:
            order = await asyncio.to_thread(self.broker.get_order_by_client_id, trade["entry_token"])
        except OrderRejected as exc:
            if trade["entry_filled_qty"] or trade["cover_orders"] or await asyncio.to_thread(self.broker.position_qty, trade["symbol"]) != 0:
                raise OrderSubmissionUncertain("Entry rejection conflicts with fills or broker position") from None
            trade.update(status="skipped", entry_terminal=True, reconciliation_required=False, note=self._safe_error(exc))
            self._dirty = True
            return
        if order is None:
            raise OrderSubmissionUncertain("Saved entry intent has no confirmed broker response; no replacement submitted")
        self._apply_entry_order(trade, order)
        for cover in trade["cover_orders"]:
            if cover.get("definitive_rejection"):
                continue
            try:
                row = await asyncio.to_thread(self.broker.get_order_by_client_id, cover["token"])
            except OrderRejected as exc:
                if cover["filled_qty"]:
                    raise OrderSubmissionUncertain("Cover rejection conflicts with known fills") from None
                cover.update(status="rejected", definitive_rejection=True, note=self._safe_error(exc))
                continue
            if row is None:
                raise OrderSubmissionUncertain("Saved cover intent has no confirmed broker response; no replacement submitted")
            self._validate_owned_order(row, cover["token"], trade["symbol"], "buy", cover["qty"])
            filled = int(row.get("filled_qty", 0))
            if filled < cover["filled_qty"] or filled > cover["qty"]:
                raise OrderSubmissionUncertain("Cover fills changed inconsistently")
            cover.update(id=str(row["id"]), status=row["status"], filled_qty=filled,
                         avg_price=_positive(row["filled_avg_price"]) if filled else None,
                         first_fill_time=row.get("first_fill_time"), last_fill_time=row.get("last_fill_time"))
        covered = sum(row["filled_qty"] for row in trade["cover_orders"])
        remaining = trade["entry_filled_qty"] - covered
        trade["remaining_qty"] = remaining
        if remaining < 0:
            raise OrderSubmissionUncertain("Reported covers exceed the managed short quantity")
        position = await asyncio.to_thread(self.broker.position_qty, trade["symbol"])
        if position != -remaining:
            raise OrderSubmissionUncertain("Broker position differs from managed fills; awaiting reconciliation without sending a cover")
        trade["reconciliation_required"] = False
        self._sync_broker_status()
        if trade["status"] == "skipped":
            self._dirty = True
            return
        self._cancel_entry_outside_price_range(trade, self._fresh_quote(trade["symbol"]))
        reason = self._exit_signal(trade) if remaining else trade.get("exit_reason", "")
        if reason:
            trade["exit_reason"] = reason
            trade["cancel_entry"] = True
        if self.now() >= stamp(trade["entry_deadline"]) or not self.entries_enabled or not self._data_status.get("ready"):
            trade["cancel_entry"] = True
        if trade["cancel_entry"] and not trade["entry_terminal"]:
            prefix = f"{trade['entry_cancel_reason']}; " if trade.get("entry_cancel_reason") else ""
            trade["note"] = prefix + "Canceling unfilled entry remainder; awaiting DAS confirmation"
            self._persist()
            await asyncio.to_thread(self.broker.cancel_order, trade["entry_order_id"])
            return
        live_covers = [row for row in trade["cover_orders"] if row["status"] not in TERMINAL_STATUSES]
        if remaining == 0 and trade["entry_filled_qty"] and trade["entry_terminal"] and not live_covers:
            cost = sum(row["filled_qty"] * row["avg_price"] for row in trade["cover_orders"] if row["filled_qty"])
            self._finish_trade(trade, cost / covered)
            return
        if remaining == 0:
            self._dirty = True
            return
        if not reason:
            trade.update(status="open", note="Supervising stop, target and scheduled exit")
            self._dirty = True
            return
        if live_covers:
            cover = live_covers[0]
            elapsed = (self.now() - stamp(cover["submitted_at"])).total_seconds()
            last_cancel = cover.get("cancel_requested_at")
            retry_due = not last_cancel or (self.now() - stamp(last_cancel)).total_seconds() >= self.settings.cover_replace_seconds
            if elapsed >= self.settings.cover_replace_seconds and retry_due:
                cover["cancel_requested"] = True
                cover["cancel_requested_at"] = self.now().isoformat()
                self._persist()
                await asyncio.to_thread(self.broker.cancel_order, cover["id"])
            trade.update(status="cover_pending", note="Waiting for cover fills/cancellation; no overlapping buy order")
            self._dirty = True
            return
        if not trade["entry_terminal"]:
            return
        quote = self._fresh_quote(trade["symbol"])
        if not quote:
            trade["note"] = f"{reason}: waiting for a fresh SIP ask to cover"
            self._dirty = True
            return
        # Manual orders in the same symbol can otherwise make two independent
        # buyers cover the same short. Ownership comes from the durable tokens.
        owned = {str(trade["entry_token"]), *(str(row["token"]) for row in trade["cover_orders"])}
        open_orders = await asyncio.to_thread(self.broker.list_open_orders, trade["symbol"])
        if any(str(row.get("client_order_id")) not in owned for row in open_orders):
            raise OrderSubmissionUncertain("An unmanaged order exists in this symbol; reconcile it in DAS before covering")
        # REST/broker roundtrips can make the earlier quote stale.
        quote = self._fresh_quote(trade["symbol"])
        if not quote:
            trade["note"] = f"{reason}: quote expired during broker reconciliation"
            self._dirty = True
            return
        previous_cover = trade["cover_orders"][-1] if trade["cover_orders"] else None
        route = self.settings.das.route
        if previous_cover and previous_cover["status"] == "rejected":
            if (self.now() - stamp(previous_cover["submitted_at"])).total_seconds() < self.settings.cover_replace_seconds:
                return
            if previous_cover.get("route", route) == route and self.settings.das.backup_route:
                route = self.settings.das.backup_route
        cover_price = order_price(percent(quote["ask"], self.settings.cover_cushion_percent))
        token = self.broker.new_client_order_id()
        cover = {"token": token, "id": "", "qty": remaining, "filled_qty": 0, "avg_price": None,
                 "status": "pending_submit", "limit_price": cover_price, "route": route,
                 "submitted_at": self.now().isoformat(), "cancel_requested": False}
        trade["cover_orders"].append(cover)
        trade.update(status="cover_pending", note=f"Cover requested: {reason}")
        self._persist()
        try:
            result = await asyncio.to_thread(self.broker.submit_limit_order, symbol=trade["symbol"], qty=remaining,
                                             side="buy", limit_price=cover_price, client_order_id=token, route=route)
            self._validate_owned_order(result, token, trade["symbol"], "buy", remaining)
            cover.update(id=str(result["id"]), status=result["status"])
        except OrderRejected as exc:
            cover.update(status="rejected", definitive_rejection=True, note=self._safe_error(exc))
            trade["note"] = "Cover explicitly rejected; will reconcile position before another cover"
        except Exception:
            trade["reconciliation_required"] = True
            raise
        finally:
            self._dirty = True
            self._persist()

    def _manage_monitor_trade(self, trade: dict) -> None:
        quote = self._fresh_quote(trade["symbol"])
        self._cancel_entry_outside_price_range(trade, quote)
        if not trade["entry_filled_qty"]:
            if self.now() >= stamp(trade["entry_deadline"]) or trade["cancel_entry"] or not self.entries_enabled:
                reason = f"; {trade['entry_cancel_reason']}" if trade.get("entry_cancel_reason") else ""
                trade.update(status="skipped", entry_terminal=True, note=f"Simulated entry canceled without a fill{reason}")
            elif self._data_status.get("ready") and quote and quote["bid"] >= trade["entry_limit"]:
                trade.update(entry_filled_qty=trade["requested_qty"], entry_avg_price=quote["bid"], entry_time=self.now().isoformat(),
                             entry_terminal=True, remaining_qty=trade["requested_qty"], status="open", note="Simulated full fill at SIP bid",
                             stop_price=percent(quote["bid"], trade["stop_percent"]), target_price=percent(quote["bid"], -trade["target_percent"]))
            self._dirty = True
        if trade["entry_filled_qty"]:
            reason = self._exit_signal(trade)
            if reason:
                trade["exit_reason"] = reason
                if quote:
                    self._finish_trade(trade, quote["ask"])
                else:
                    trade["note"] = f"{reason}: waiting for a fresh SIP ask for simulated cover"
                    self._dirty = True

    def _finish_trade(self, trade: dict, price: float) -> None:
        fill_times = [stamp(row["last_fill_time"]) for row in trade["cover_orders"] if row.get("last_fill_time") and row["filled_qty"]]
        exit_time = max(fill_times).isoformat() if fill_times else self.now().isoformat()
        trade.update(status="closed", remaining_qty=0, exit_avg_price=price, exit_time=exit_time,
                     flat_confirmed_at=self.now().isoformat(),
                     realized_pnl=(trade["entry_avg_price"] - price) * trade["entry_filled_qty"],
                     note="Position confirmed flat" if self.settings.execution_enabled else "Simulated position closed",
                     reconciliation_required=False)
        self._event(f"Closed {trade['exit_reason']} at ${price:g}; gross P/L ${trade['realized_pnl']:,.2f}", symbol=trade["symbol"])
        self._dirty = True

    async def stop_entries(self) -> None:
        self.entries_enabled = False
        for trade in self._trades(active=True):
            trade["cancel_entry"] = True
        self._event("New entries stopped; pending entries cancel and open positions remain supervised", "warning")
        self._persist()
        if self.running:
            await self.tick()

    async def cover_all(self) -> None:
        self.entries_enabled = False
        for trade in self._trades(active=True):
            self._record_exit_evidence(trade, "manual_cover")
            trade.update(cancel_entry=True, exit_reason="manual_cover")
        self._event("Manual cover requested for this strategy's managed positions", "warning")
        self._persist()
        if self.running:
            await self.tick()

    async def close(self) -> None:
        if not self._opened:
            return
        self._closing = True
        await self.stop_entries()
        if self._entry_task and not self._entry_task.done():
            # A paid locate or stock send already in flight must finish its journal.
            await self._entry_task
        if self.settings.execution_enabled and self._trades(active=True):
            for trade in self._trades(active=True):
                if not trade.get("exit_reason"):
                    self._record_exit_evidence(trade, "shutdown")
                trade.update(cancel_entry=True, exit_reason=trade.get("exit_reason") or "shutdown")
            self._event("Shutdown requested: canceling entries and covering managed positions", "warning")
            self._persist()
            deadline = asyncio.get_running_loop().time() + self.settings.shutdown_grace_seconds
            while self._trades(active=True) and asyncio.get_running_loop().time() < deadline:
                await self.tick()
                if self._trades(active=True):
                    await asyncio.sleep(min(self.settings.poll_seconds, max(0, deadline - asyncio.get_running_loop().time())))
            if self._trades(active=True):
                message = "SHUTDOWN WITH UNRESOLVED MANAGED EXPOSURE. Verify/cancel/cover in DAS immediately. App-managed stops stop with this process; state is retained for restart."
                self._event(message, "error")
                LOGGER.critical(message)
        self.running = False
        self._stop.set()
        # Let an in-flight supervisor broker call finish before releasing the
        # account lock. Canceling to_thread does not stop its socket worker.
        await asyncio.gather(*(task for task in self._tasks if task.get_name() == "4am-supervisor"), return_exceptions=True)
        tasks = [*self._tasks, self._bootstrap_task, self._finalize_task, self._late_finalize_task, self._backtest_task]
        for task in tasks:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
        if self._locate_service_task:
            # Do not abandon an already-running broker worker when releasing its lock.
            await self._locate_service_task
        if self._broker_health_task:
            # Canceling to_thread does not stop the socket worker. Await it
            # before closing the client or releasing the account ownership lock.
            await self._broker_health_task
        try:
            self._persist()
        finally:
            try:
                if self.data:
                    await self.data.close()
                if self.broker:
                    await asyncio.to_thread(self.broker.close)
                    self._broker_status.update(connected=False, checking=False, status="Disconnected; supervisor stopped")
                    self._broker_check_at = None
            finally:
                self.store.close()
                self._opened = False

    def snapshot(self) -> dict:
        self._sync_broker_status()
        today = self._day_state()
        candidates = []
        for source in today["candidates"].values():
            row = dict(source)
            row["locate_trigger_price"] = self._locate_trigger_price(row)
            primary = today["trades"].get(row["symbol"])
            trade = today["trades"].get(row["symbol"] + ":reentry", primary)
            if trade:
                pending = self._reentry_candidate(primary) if primary else None
                if pending:
                    row.update(pending)
                    if self.now() >= self._candidate_deadline(pending):
                        row.update(status="expired", note="Re-entry deadline passed")
                    else:
                        price_reason = self._entry_price_block_reason(row["entry_limit"], "Entry limit")
                        quote = self._fresh_quote(row["symbol"])
                        if price_reason:
                            row.update(status="price_filtered", note=price_reason)
                        elif quote and (price_reason := self._entry_price_block_reason(quote["bid"], "Bid")):
                            row.update(status="waiting_for_price", note=price_reason)
                else:
                    row.update(status=trade["status"], note=trade["note"], locate=trade["locate"],
                               trade_number=trade.get("trade_number", 1), entry_limit=trade["entry_limit"])
                    row["active_at"] = trade.get("active_at") or (
                        primary.get("exit_time", row["active_at"])
                        if trade.get("trade_number", 1) == 2 and primary else row["active_at"])
                if row.get("trade_number") == 2:
                    row["locate_trigger_price"] = None
            elif self.now() >= at(self._day, self.settings.strategy.entry_deadline):
                row.update(status="expired", note="Entry deadline passed")
            row["quote"] = self._quotes.get(row["symbol"])
            if row["quote"]:
                row["quote_age_seconds"] = round((self.now() - stamp(row["quote"]["timestamp"])).total_seconds(), 2)
            candidates.append(row)
        visible_trades = list(today["trades"].values()) + [trade for trade in self._trades(active=True) if trade["date"] != str(self._day)]
        return {"strategy_name": STRATEGY_NAME, "mode": self.settings.mode, "demo": self.settings.demo,
                "running": self.running, "entries_enabled": self.entries_enabled, "time": self.now().isoformat(),
                "data": dict(self._data_status), "broker": dict(self._broker_status), "rules": self.settings.public_rules(),
                "entry_block": self._entry_block,
                "counts": {"candidates": len(candidates), "qualified": len(candidates),
                           "entered": sum(t["entry_filled_qty"] > 0 for t in visible_trades),
                           "open": sum(t["remaining_qty"] > 0 for t in visible_trades),
                           "closed": sum(t["status"] == "closed" for t in visible_trades),
                           "skipped": sum(t["status"] == "skipped" for t in visible_trades)},
                "candidates": sorted(candidates, key=lambda row: row["symbol"]),
                "trades": json.loads(json.dumps(visible_trades)), "events": self.state["events"][-100:],
                "backtest": dict(self.backtest)}

    def _load_latest_backtest(self) -> None:
        try:
            config = load_config(self.settings.strategy_config_path)
            reports = sorted(config.output_dir.glob("*/4am_short_summary.json"), key=lambda path: path.stat().st_mtime)
            if not reports:
                return
            path = reports[-1]
            summary = json.loads(path.read_text())
            with path.with_name("4am_short_monthly.csv").open(newline="") as handle:
                monthly = list(csv.DictReader(handle))
            self.backtest.update(latest_summary=summary, monthly=monthly, reports_dir=str(path.parent))
        except (OSError, ValueError) as exc:
            self.backtest["error"] = self._safe_error(exc)

    async def start_backtest(self) -> None:
        if self._backtest_task and not self._backtest_task.done():
            raise RuntimeError("A 4am short backtest is already running")
        self.backtest.update(running=True, error=None, last_output="")
        self._backtest_task = asyncio.create_task(self._run_backtest(), name="4am-backtest")

    async def _run_backtest(self) -> None:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, str(PROJECT / "backtest_4am_short.py"),
                    "--config", str(self.settings.strategy_config_path), cwd=PROJECT,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            assert process.stdout is not None
            async for line in process.stdout:
                self.backtest["last_output"] = (self.backtest["last_output"] + self._safe_error(line.decode(errors="replace")))[-7000:]
            code = await process.wait()
            if code:
                self.backtest["error"] = f"Backtest returned {code}; inspect candidate errors/incomplete results"
            self._load_latest_backtest()
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
            raise
        except Exception as exc:
            self.backtest["error"] = self._safe_error(exc)
        finally:
            self.backtest["running"] = False
