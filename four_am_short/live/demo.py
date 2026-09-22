"""Deterministic dashboard preview. No network, broker, subprocess or state I/O."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

from ..models import EASTERN
from .config import LiveSettings


class DemoEngine:
    def __init__(self, settings: LiveSettings):
        self.settings = replace(settings, demo=True, mode="monitor")
        self.running = False
        self.events: list[dict] = []
        self.day = datetime.now(EASTERN).date()
        rules = self.settings.strategy
        reentry_enabled = self.settings.public_rules().get("reentry", {}).get("enabled", False)
        early = datetime.fromisoformat(f"{self.day}T{rules.early_start}").replace(tzinfo=EASTERN)
        end = datetime.fromisoformat(f"{self.day}T{rules.early_end}").replace(tzinfo=EASTERN)
        self.candidates = []
        self.trades = []
        for index, symbol in enumerate(("DEMOA", "DEMOB", "DEMOC")):
            close = 1.2 + index
            high = round(close * (1 + (rules.gap_percent + 16) / 100), 4)
            high_time = early + timedelta(minutes=index + 6)
            active = max(end, high_time + timedelta(minutes=rules.wait_after_high_minutes))
            limit = round(high * (1 - rules.entry_below_high_percent / 100), 4)
            self.candidates.append({"symbol": symbol, "trade_number": 1, "previous_close": close,
                "first_gap_time": (early + timedelta(minutes=index + 2)).isoformat(),
                "first_gap_bar_high": round(close * (1 + (rules.gap_percent + 4) / 100), 4), "early_high": high,
                "early_high_time": high_time.isoformat(), "entry_limit": limit,
                "locate_trigger_price": (None if self.settings.locate_trigger_below_entry_percent is None else
                    limit * (1 - self.settings.locate_trigger_below_entry_percent / 100)),
                "active_at": active.isoformat(), "status": ("open", "closed", "waiting")[index],
                "note": ("Sample open position", "Sample profit-target exit", "Sample bounce entry pending")[index],
                "quote": {"bid": round(limit - .02, 4), "ask": round(limit, 4)},
                "locate": {"status": "demo", "shares": rules.shares, "cost": rules.shares * .015} if index < 2 else {}})
            if index < 2:
                entry = round(limit + .01, 4)
                target = round(entry * (1 - rules.profit_target_percent / 100), 4)
                closed = index == 1
                stop = round(entry * (1 + rules.stop_loss_percent / 100), 4)
                stopped = closed and reentry_enabled
                exit_price = stop if stopped else target
                self.trades.append({"symbol": symbol, "trade_number": 1, "status": "closed" if closed else "open",
                    "requested_qty": rules.shares, "entry_filled_qty": rules.shares,
                    "entry_avg_price": entry, "entry_time": active.isoformat(),
                    "stop_price": stop,
                    "target_price": target, "remaining_qty": 0 if closed else rules.shares,
                    "exit_avg_price": exit_price if closed else None,
                    "exit_time": (active + timedelta(minutes=12)).isoformat() if closed else None,
                    "exit_reason": ("stop_loss" if stopped else "profit_target") if closed else None,
                    "realized_pnl": round((entry - exit_price) * rules.shares, 2) if closed else 0,
                    "locate": self.candidates[-1]["locate"], "note": "Fictional preview fills only"})
                if stopped:
                    reentry_at = active + timedelta(minutes=13)
                    reentry_limit = round(high * (1 + rules.reentry.entry_above_high_percent / 100), 4)
                    reentry_price = max(reentry_limit, stop)
                    reentry_target = round(reentry_price * (1 - rules.reentry.profit_target_percent / 100), 4)
                    reused_locate = {"status": "reused", "shares": rules.shares, "cost": 0}
                    self.trades.append({"symbol": symbol, "trade_number": 2, "status": "closed",
                        "requested_qty": rules.shares, "entry_filled_qty": rules.shares,
                        "entry_avg_price": reentry_price, "entry_time": reentry_at.isoformat(),
                        "stop_price": round(reentry_price * (1 + rules.reentry.stop_loss_percent / 100), 4),
                        "target_price": reentry_target, "remaining_qty": 0,
                        "exit_avg_price": reentry_target, "exit_time": (reentry_at + timedelta(minutes=12)).isoformat(),
                        "exit_reason": "profit_target", "realized_pnl": round((reentry_price - reentry_target) * rules.shares, 2),
                        "locate": reused_locate, "note": "Fictional re-entry after initial stop-loss"})
                    self.candidates[-1].update(trade_number=2, entry_limit=reentry_limit,
                        active_at=reentry_at.isoformat(), locate_trigger_price=None, locate=reused_locate,
                        note="Sample re-entry profit-target exit")
        self.stats = {"gap_triggered": 8, "gap_traded": 5, "gap_not_traded": 3, "trades": 5,
            "wins": 3, "losses": 2, "win_percent": 60, "loss_percent": 40, "net_pnl": 437.5,
            "average_net_pnl": 87.5, "profit_factor": 1.7, "max_closed_trade_drawdown": 310,
            "winning_days": 3, "losing_days": 2, "stops": 2,
            "initial_trades": 4 if reentry_enabled else 5,
            "reentry_attempts": int(reentry_enabled), "reentry_trades": int(reentry_enabled), "reentry_skipped": 0}
        self.backtest = {"running": False, "error": None, "latest_summary": dict(self.stats),
            "monthly": [{"period": self.day.strftime("%Y-%m"), **self.stats}],
            "last_output": "DEMO: fictional sample results. No historical requests were made.",
            "reports_dir": "Demo preview — no reports written"}

    def _event(self, message: str) -> None:
        self.events.append({"time": datetime.now(EASTERN).isoformat(), "level": "info", "message": message, "symbol": ""})

    async def open(self) -> None:
        self._event("Demo ready. Every symbol, fill and result is fictional; no connections are opened.")

    async def close(self) -> None:
        self.running = False

    async def start(self) -> None:
        self.running = True
        self._event("Demo monitoring started. No orders or locates are submitted.")

    async def stop_entries(self) -> None:
        self.running = False
        self._event("Demo monitoring paused.")

    async def cover_all(self) -> None:
        self.running = False
        self._event("Demo cover control received. Fictional preview state is unchanged; no order was sent.")

    async def start_backtest(self) -> None:
        self._event("Demo backtest preview refreshed. Results are fictional; no historical data was requested.")

    def snapshot(self) -> dict:
        now = datetime.now(EASTERN).isoformat()
        candidates = deepcopy(self.candidates)
        for candidate in candidates:
            candidate["quote"]["timestamp"] = now
        return {"strategy_name": "4am short", "mode": "monitor", "demo": True,
            "running": self.running, "entries_enabled": False,
            "data": {"connected": self.running, "ready": self.running, "status": "Demo sample data",
                     "last_message": now if self.running else None, "universe_size": 3,
                     "previous_close_date": "DEMO", "backfill_ready": True},
            "broker": {"connected": False, "status": "Demo — no broker connection", "account_masked": ""},
            "rules": self.settings.public_rules(),
            "counts": {"candidates": 3, "qualified": 3, "entered": len(self.trades), "open": 1, "closed": len(self.trades) - 1, "skipped": 0},
            "candidates": candidates, "trades": deepcopy(self.trades), "events": deepcopy(self.events[-100:]),
            "backtest": deepcopy(self.backtest)}
