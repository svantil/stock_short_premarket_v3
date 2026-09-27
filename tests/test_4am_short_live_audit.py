"""Decision evidence is durable, immutable and cannot gate order supervision."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from four_am_short.live.engine import LiveEngine
from four_am_short.models import Bar, EASTERN
import test_4am_short_live_engine as broker_fixtures


class LiveAuditTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = broker_fixtures.EngineBrokerSafetyTests.asyncSetUp
    asyncTearDown = broker_fixtures.EngineBrokerSafetyTests.asyncTearDown
    quote = broker_fixtures.EngineBrokerSafetyTests.quote
    trade = broker_fixtures.EngineBrokerSafetyTests.trade
    covers = broker_fixtures.EngineBrokerSafetyTests.covers
    enter = broker_fixtures.EngineBrokerSafetyTests.enter
    entered_position = broker_fixtures.EngineBrokerSafetyTests.entered_position

    async def test_entry_inputs_are_saved_before_borrow_and_survive_updates(self):
        self.engine._data_status["previous_close_date"] = "2026-09-18"
        observed = []

        def check_persisted():
            saved = json.loads(self.engine.settings.state_path.read_text())
            observed.append(saved["days"]["2026-09-21"]["trades"]["TEST"]["entry_evidence"])
            self.quote(9.3, 9.31)

        self.broker.locate_hook = check_persisted
        trade = await self.enter()
        evidence = trade["entry_evidence"]
        self.assertEqual(observed, [evidence])
        self.assertEqual(evidence["data_feed"], "alpaca_sip")
        self.assertEqual(evidence["previous_close"], 7)
        self.assertEqual(evidence["previous_close_date"], "2026-09-18")
        self.assertEqual(evidence["quote"]["bid"], 9.2)
        self.assertEqual(evidence["setup"]["early_high"], 10)
        self.assertEqual(evidence["setup"]["entry_limit"], 9)
        self.assertEqual(evidence["setup_bars"][0]["high"], 10)
        self.assertEqual(evidence["setup_bars_source"], "frozen_setup")
        self.assertTrue(evidence["window_finalized"])
        self.candidate["early_high"] = 90
        self.engine.settings = replace(self.engine.settings, strategy=replace(self.engine.settings.strategy, shares=42))
        self.assertEqual(evidence["setup"]["early_high"], 10)
        self.assertEqual(evidence["strategy"]["shares"], 1000)
        serialized = json.dumps(evidence)
        for secret in ("SIMUSER", "SIMTEST", "simulation-only"):
            self.assertNotIn(secret, serialized)

    async def test_session_records_new_strategy_without_rewriting_original(self):
        first = deepcopy(self.engine._day_state()["audit"])
        self.clock.advance(1)
        settings = replace(self.engine.settings, strategy=replace(self.engine.settings.strategy, shares=42))
        restarted = LiveEngine(settings, now=self.clock)
        restarted.state = deepcopy(self.engine.state)
        daily = restarted._day_state()["audit"]
        self.assertEqual(daily["strategy"], first["strategy"])
        self.assertEqual(len(daily["sessions"]), 2)
        self.assertEqual(daily["sessions"][0]["strategy"]["shares"], 1000)
        self.assertEqual(daily["sessions"][1]["strategy"]["shares"], 42)
        self.assertEqual(daily["sessions"][1]["execution"]["quote_max_age_seconds"], settings.data.quote_max_age_seconds)
        restarted._day_state()
        self.assertEqual(len(daily["sessions"]), 2)

    async def test_previous_close_date_is_saved_after_discovery(self):
        await self.engine._bootstrap()
        audit = self.engine._day_state()["audit"]
        self.assertEqual(audit["previous_close_date"], "2026-09-18")
        self.assertEqual(audit["sessions"][-1]["previous_close_date"], "2026-09-18")

    async def test_late_setup_bars_remain_frozen_after_bar_correction(self):
        self.clock.value = self.clock().replace(hour=5, minute=20)
        self.engine.settings = replace(self.engine.settings, strategy=replace(self.engine.settings.strategy, late_gap_enabled=True))
        self.engine._closes["LATE"] = 1
        early = Bar(datetime(2026, 9, 21, 4, 0, tzinfo=EASTERN), 1.1, 1.2, 1, 1.1, 200)
        late = Bar(datetime(2026, 9, 21, 5, 0, tzinfo=EASTERN), 1.9, 2, 1.8, 1.9, 500)
        self.engine._accept_bar("LATE", early, historical=True)
        self.engine._accept_bar("LATE", late)
        self.engine._mark_late_windows_finalized(self.clock())
        candidate = self.engine._day_state()["candidates"]["LATE"]
        self.assertTrue(candidate["window_finalized"])
        self.engine._accept_bar("LATE", replace(late, high=3))
        self.assertEqual(self.engine._bars["LATE"][late.timestamp.isoformat()].high, 3)
        self.assertEqual(candidate["early_high"], 2)
        trade = self.engine._new_trade(candidate)
        self.engine._record_entry_evidence(trade, candidate)
        evidence = trade["entry_evidence"]
        self.assertEqual([row["high"] for row in evidence["setup_bars"]], [1.2, 2])

    async def test_first_exit_quote_is_preserved_even_if_price_recovers(self):
        trade = await self.entered_position()
        self.clock.advance(1)
        trigger_time = self.clock().isoformat()
        await self.engine.on_event({"T": "q", "S": "TEST", "bp": 12.4, "ap": 12.41,
                                    "bs": 10, "as": 10, "t": trigger_time})
        evidence = deepcopy(trade["exit_signal_evidence"])
        self.assertEqual(evidence["reason"], "stop_loss")
        self.assertEqual(evidence["quote"]["ask"], 12.41)
        self.assertEqual(evidence["stop_price"], 12.35)
        self.assertEqual(evidence["decision_time"], trigger_time)
        self.clock.advance(1)
        self.quote(10, 10.01)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade["exit_signal_evidence"], evidence)
        self.assertEqual(len(self.covers()), 1)

    async def test_time_exit_records_no_quote_when_current_quote_is_missing(self):
        trade = await self.entered_position()
        trade["time_exit"] = self.clock().isoformat()
        self.engine._quotes.clear()
        self.assertEqual(self.engine._exit_signal(trade), "time_exit")
        self.assertEqual(trade["exit_signal_evidence"]["reason"], "time_exit")
        self.assertIsNone(trade["exit_signal_evidence"]["quote"])

    async def test_capture_failures_do_not_prevent_entries_or_stop_covers(self):
        with patch("four_am_short.live.audit.entry_snapshot", side_effect=ValueError("test evidence failure")):
            with self.assertLogs("four_am_short.live.engine", level="WARNING"):
                trade = await self.entered_position()
        self.assertEqual(trade["entry_filled_qty"], 1000)
        self.assertNotIn("entry_evidence", trade)
        self.quote(12.4, 12.41)
        with patch("four_am_short.live.audit.exit_snapshot", side_effect=ValueError("test evidence failure")):
            with self.assertLogs("four_am_short.live.engine", level="WARNING"):
                await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade["exit_reason"], "stop_loss")
        self.assertEqual(len(self.covers()), 1)

    async def test_retry_records_new_attempt_without_replacing_first_evidence(self):
        trade = self.engine._new_trade(self.candidate)
        self.engine._record_entry_evidence(trade, self.candidate)
        initial = deepcopy(trade["entry_evidence"])
        self.clock.advance(30)
        self.quote(9.4, 9.41)
        self.engine._record_entry_evidence(trade, self.candidate)
        self.assertEqual(trade["entry_evidence"], initial)
        self.assertEqual(len(trade["entry_attempt_evidence"]), 2)
        self.assertEqual(trade["entry_attempt_evidence"][-1]["quote"]["bid"], 9.4)

    async def test_reentry_uses_original_saved_setup_bars_after_restart(self):
        primary = await self.enter()
        candidate = dict(self.candidate, trade_number=2, entry_limit=10.5)
        self.engine._setup_bars.clear()
        self.engine._bars.clear()
        trade = self.engine._new_trade(candidate)
        self.engine._record_entry_evidence(trade, candidate)
        self.assertEqual(trade["entry_evidence"]["setup_bars"], primary["entry_evidence"]["setup_bars"])
        self.assertEqual(trade["entry_evidence"]["setup_bars_source"], "primary_entry_evidence")
        self.assertEqual(trade["entry_evidence"]["setup"]["entry_limit"], 10.5)


if __name__ == "__main__":
    unittest.main()
