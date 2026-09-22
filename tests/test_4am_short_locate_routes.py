"""Additional immediate-quote routes, simulated only; no broker is contacted."""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from four_am_short.live.config import DasSettings, load_live_settings
from four_am_short.live.das import DasClient, DasError
from test_4am_short_live_das import Wire


QUOTE_ROUTES = ("LOCATE7", "LOCATE1", "LOCATE8", "LOCATE12", "LOCATE14")


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class QuoteRouteWire(Wire):
    """Model immediate purchases accurately, including purchases with no cap."""
    def __init__(self, clock):
        super().__init__()
        self.clock, self.timeout = clock, .1
        self.prices.update(dict.fromkeys(QUOTE_ROUTES, "0.005"))
        self.minimums.update(dict.fromkeys(QUOTE_ROUTES, "0"))
        self.quote_rows = {}
        self.inquiries = []

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        if not self.buffer:
            self.clock.sleep(self.timeout)
            raise socket.timeout()
        return super().recv(size)

    def sendall(self, data):
        command = data.decode().strip()
        fields = command.split()
        if fields[0] == "SLPRICEINQUIRE":
            self.inquiries.append((self.clock.now, fields[3]))
            if fields[3] in self.quote_rows:
                self.commands.append(command)
                if self.sent_callback:
                    self.sent_callback(command)
                for row in self.quote_rows[fields[3]]:
                    self.queue(row)
                return
        if fields[0] == "SLNEWORDER" and fields[3] in QUOTE_ROUTES:
            self.commands.append(command)
            if self.sent_callback:
                self.sent_callback(command)
            quantity, route = int(fields[2]), fields[3]
            row = dict(id=str(91 + len(self.locates)), qty=quantity, route=route,
                       token=fields[4], price=self.prices[route], status="Located",
                       open=0, filled=quantity)
            if self.locate_mutator:
                self.locate_mutator(row, True)
            self.locates[row["id"]] = row
            self.available += row["filled"]
            if not self.purchase_no_ack:
                self.queue(self.locate_line(row))
            return
        super().sendall(data)


class QuoteRouteConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.strategy = self.root / "strategy.json"
        self.strategy.write_text(json.dumps({"input_file": "unused.txt", "data": {"env_file": None}}))
        self.path = self.root / "live.json"

    def load(self, das):
        self.path.write_text(json.dumps({
            "strategy_config": str(self.strategy), "mode": "monitor",
            "env_file": "unused-credentials.txt", "das": das,
        }))
        with patch("four_am_short.live.config._environment", return_value={}):
            return load_live_settings(self.path)

    def test_new_routes_and_uncapped_purchases_are_opt_in(self):
        settings = self.load({})
        self.assertEqual(settings.das.locate_quote_routes, ())
        self.assertIs(settings.das.allow_uncapped_locate_purchases, False)
        self.assertEqual(set(settings.das.locate_routes), {"LOCATE4", "LOCATE6", "LOCATE10"})

    def test_requested_routes_are_deduplicated_and_included_in_union(self):
        settings = self.load({"locate_quote_routes": [*QUOTE_ROUTES, "LOCATE7"],
                              "allow_uncapped_locate_purchases": True})
        self.assertEqual(settings.das.locate_quote_routes, QUOTE_ROUTES)
        self.assertTrue(settings.das.allow_uncapped_locate_purchases)
        self.assertEqual(set(settings.das.locate_routes), {"LOCATE4", "LOCATE6", "LOCATE10", *QUOTE_ROUTES})

    def test_invalid_route_assignments_and_non_boolean_opt_in_are_rejected(self):
        invalid = [
            {"locate_quote_routes": "LOCATE7"}, {"locate_quote_routes": [7]},
            {"locate_quote_routes": ["ALLROUTE"]}, {"locate_quote_routes": ["LOCATE99"]},
            {"locate_quote_routes": ["LOCATE4"]}, {"locate_quote_routes": ["LOCATE10"]},
            {"locate_offer_routes": ["LOCATE7"]}, {"locate_limit_price_routes": ["LOCATE7"]},
            {"locate_offer_routes": ["LOCATE1"], "locate_quote_routes": ["LOCATE1"]},
            *({"allow_uncapped_locate_purchases": value} for value in (None, 0, 1, "true", [])),
        ]
        for das in invalid:
            with self.subTest(das=das):
                with self.assertRaises(ValueError):
                    self.load(das)


class QuoteRouteProtocolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "das.json"
        self.clock = Clock()
        for name, replacement in (("monotonic", self.clock.monotonic), ("sleep", self.clock.sleep)):
            mocked = patch(f"four_am_short.live.das_locates.time.{name}", replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.wire = QuoteRouteWire(self.clock)
        self.settings = DasSettings(
            username="TESTUSER", password="test-only-password", account="TESTACCOUNT",
            timeout_seconds=.1, locate_quote_wait_seconds=.02,
            locate_quote_routes=QUOTE_ROUTES,
        )
        self.client = self.make_client()

    def make_client(self, **changes):
        if hasattr(self, "client"):
            self.client.close()
        client = DasClient(replace(self.settings, **changes), journal_path=self.path,
                           socket_factory=lambda *_args, **_kwargs: self.wire)
        self.addCleanup(client.close)
        return client

    def compare(self):
        return self.client.ensure_shortable("TEST", 100, .04)

    def rows(self):
        return {row["route"]: row for row in self.client.locate_comparisons["TEST"]}

    def paid_commands(self):
        return [command for command in self.wire.commands
                if (command.startswith("SLNEWORDER ")
                    and command.split()[3] in {*QUOTE_ROUTES, "LOCATE10"})
                or command.endswith(" Accept")]

    def assert_new_routes_unpaid(self):
        self.assertFalse(any(command.startswith("SLNEWORDER ") and command.split()[3] in QUOTE_ROUTES
                             for command in self.wire.commands))

    def test_without_purchase_opt_in_new_routes_are_quoted_but_cannot_win(self):
        success, _, winner, cost = self.compare()
        self.assertTrue(success)
        self.assertEqual((winner, cost), ("LOCATE10", .01))
        self.assert_new_routes_unpaid()
        self.assertEqual(len(self.paid_commands()), 1)
        for route in QUOTE_ROUTES:
            row = self.rows()[route]
            self.assertEqual(row["route_type"], 0)
            self.assertTrue(row["quote_only"])
            self.assertFalse(row["eligible"] or row["selected"])
            self.assertEqual(row["price"], .005)
            self.assertEqual(row["total_cost"], .5)
            self.assertEqual(row["reason"], "Quote only: route cannot enforce the purchase-price cap")

    def test_all_inquiries_are_targeted_throttled_and_capped_quote_is_last(self):
        self.compare()
        self.assertEqual([route for _, route in self.wire.inquiries], [*QUOTE_ROUTES, "LOCATE10"])
        for earlier, later in zip(self.wire.inquiries, self.wire.inquiries[1:]):
            self.assertGreaterEqual(later[0] - earlier[0], 3)
        for route in QUOTE_ROUTES:
            self.assertEqual(self.wire.commands.count(f"SLPRICEINQUIRE TEST 100 {route}"), 1)
            self.assertEqual(self.wire.commands.count(f"SLRouteMinCharge {route}"), 1)
        self.assertFalse(any("ALLROUTE" in command for command in self.wire.commands))

    def test_authorized_cheapest_route_gets_one_uncapped_purchase(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        success, _, winner, cost = self.compare()
        self.assertEqual((success, winner, cost), (True, "LOCATE7", .005))
        self.assertEqual(len(self.paid_commands()), 1)
        command = self.paid_commands()[0].split()
        self.assertEqual(command[:4], ["SLNEWORDER", "TEST", "100", "LOCATE7"])
        self.assertEqual(len(command), 5, "Uncapped route must not receive an unsupported limit field")
        self.assertTrue(self.rows()["LOCATE7"]["selected"])
        self.assertEqual(self.wire.commands.count("SLPRICEINQUIRE TEST 100 LOCATE7"), 2)
        self.assertEqual(self.wire.inquiries[-1][1], "LOCATE7")

    def test_changed_selected_quote_at_refresh_prevents_purchase(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        inquiries = 0

        def change_on_second_inquiry(command):
            nonlocal inquiries
            if command == "SLPRICEINQUIRE TEST 100 LOCATE7":
                inquiries += 1
                if inquiries == 2:
                    self.wire.prices["LOCATE7"] = "0.006"

        self.wire.sent_callback = change_on_second_inquiry
        with self.assertRaisesRegex(DasError, "changed before purchase"):
            self.compare()
        self.assertEqual(inquiries, 2)
        self.assertFalse(self.paid_commands())

    def test_entry_expiring_during_quote_refresh_prevents_purchase(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        valid, inquiries = True, 0

        def expire_on_second_inquiry(command):
            nonlocal valid, inquiries
            if command == "SLPRICEINQUIRE TEST 100 LOCATE7":
                inquiries += 1
                if inquiries == 2:
                    valid = False

        self.wire.sent_callback = expire_on_second_inquiry
        with self.assertRaisesRegex(DasError, "eligibility expired"):
            self.client.ensure_shortable("TEST", 100, .04, still_valid=lambda: valid)
        self.assertEqual(inquiries, 2)
        self.assertFalse(self.paid_commands())

    def test_uncapped_purchase_intent_is_durable_before_wire(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        inspected = []

        def inspect(command):
            if command.startswith("SLNEWORDER TEST 100 LOCATE7 "):
                record = next(iter(json.loads(self.path.read_text())["locates"].values()))
                request = next(row for row in record["requests"] if row["route"] == "LOCATE7")
                self.assertEqual(record["state"], "purchase_pending")
                self.assertEqual(request["phase"], "purchase_pending")
                self.assertIs(request["price_cap_enforced"], False)
                inspected.append(command)

        self.wire.sent_callback = inspect
        self.compare()
        self.assertEqual(len(inspected), 1)

    def test_preview_with_uncapped_opt_in_still_never_purchases(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        with self.client._lock:
            self.client._claim_account()
            result = self.client._offers().ensure("TEST", 100, .04, preview=True)
        self.assertTrue(result[0])
        self.assertFalse(self.paid_commands())
        self.assert_new_routes_unpaid()
        self.assertFalse(any(row["selected"] for row in self.rows().values()))
        record = next(iter(json.loads(self.path.read_text())["locates"].values()))
        self.assertTrue(record["preview"])
        self.assertTrue(all(row["phase"] in {"quote_only", "never_accept"} for row in record["requests"]))

    def test_minimum_charge_and_quote_fee_cap_still_apply_before_uncapped_purchase(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        self.wire.minimums.update(dict.fromkeys(QUOTE_ROUTES, "5"))
        self.assertEqual(self.compare()[2], "LOCATE10")
        self.assert_new_routes_unpaid()
        for route in QUOTE_ROUTES:
            self.assertFalse(self.rows()[route]["eligible"])
            self.assertIn("exceeds configured ceiling", self.rows()[route]["reason"])

    def test_failed_or_missing_quote_does_not_block_other_eligible_routes(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        self.wire.quote_rows["LOCATE7"] = ["%SLRET 2 TEST 0 0 LOCATE7 route disabled TESTACCOUNT"]
        self.wire.quote_rows["LOCATE1"] = []
        self.assertEqual(self.compare()[2], "LOCATE8")
        self.assertFalse(self.rows()["LOCATE7"]["eligible"])
        self.assertIn("route disabled", self.rows()["LOCATE7"]["reason"])
        self.assertFalse(self.rows()["LOCATE1"]["eligible"])
        self.assertEqual(len(self.paid_commands()), 1)

    def test_wrong_account_quote_cannot_be_selected(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        self.wire.quote_rows["LOCATE7"] = ["%SLRET 1 TEST 0.0001 100 LOCATE7 OTHERACCOUNT"]
        self.assertEqual(self.compare()[2], "LOCATE1")
        self.assertFalse(self.rows()["LOCATE7"]["eligible"])

    def test_insufficient_quote_shares_retains_specific_availability_reason(self):
        self.wire.quote_rows["LOCATE7"] = ["%SLRET 1 TEST 0.0001 50 LOCATE7 TESTACCOUNT"]
        self.assertEqual(self.compare()[2], "LOCATE10")
        row = self.rows()["LOCATE7"]
        self.assertEqual(row["available_qty"], 50)
        self.assertFalse(row["eligible"])
        self.assertIn("Insufficient shares", row["reason"])
        self.assert_new_routes_unpaid()

    def test_final_uncapped_price_above_quote_and_ceiling_is_reported_at_actual_cost(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)

        def changed_price(row, paid):
            if row["route"] == "LOCATE7" and paid:
                row["price"] = "0.08"

        self.wire.locate_mutator = changed_price
        success, note, route, fee = self.compare()
        self.assertEqual((success, route, fee), (True, "LOCATE7", .08))
        self.assertEqual(len(self.paid_commands()), 1)
        self.assertEqual(self.rows()[route]["actual_total_cost"], 8)
        self.assertTrue(self.rows()[route]["warning"])
        self.assertTrue(self.rows()[route]["cost_exceeded_quote"])
        self.assertTrue(self.rows()[route]["cost_exceeded_ceiling"])
        self.assertIn("exceeded", note.lower())
        record = next(iter(json.loads(self.path.read_text())["locates"].values()))
        self.assertEqual(record["state"], "located")
        self.assertFalse(record.get("reconciliation_required", False))
        paid = next(row for row in record["requests"] if row["route"] == route)
        self.assertEqual(float(paid["actual_price"]), .08)
        self.assertEqual(float(paid["total_cost"]), 8)

    def test_uncapped_missing_ack_requires_reconciliation_and_never_second_purchase(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        self.wire.purchase_no_ack = True
        with self.assertRaises(DasError):
            self.compare()
        self.assertEqual(len(self.paid_commands()), 1)
        self.wire.locates = {}
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        with self.assertRaisesRegex(DasError, "unconfirmed"):
            self.compare()
        self.assertEqual(len(self.paid_commands()), 1)

    def test_unknown_uncapped_purchase_reconciles_after_restart_without_rebuy(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        self.wire.purchase_no_ack = True
        with self.assertRaises(DasError):
            self.compare()
        self.client = self.make_client(allow_uncapped_locate_purchases=False)
        self.assertTrue(self.compare()[0])
        self.assertEqual(len(self.paid_commands()), 1)

    def test_uncapped_quantity_mismatch_still_blocks_trading_and_second_purchase(self):
        self.client = self.make_client(allow_uncapped_locate_purchases=True)

        def too_many_shares(row, paid):
            if paid and row["route"] == "LOCATE7":
                row.update(qty=101, filled=101)

        self.wire.locate_mutator = too_many_shares
        with self.assertRaises(DasError):
            self.compare()
        self.client = self.make_client(allow_uncapped_locate_purchases=True)
        with self.assertRaises(DasError):
            self.compare()
        self.assertEqual(len(self.paid_commands()), 1)

    def test_legacy_capped_journal_still_rejects_execution_above_price_limit(self):
        self.client = self.make_client(locate_quote_routes=())
        self.wire.purchase_no_ack = True
        with self.assertRaises(DasError):
            self.compare()
        self.client.close()
        journal = json.loads(self.path.read_text())
        record = next(iter(journal["locates"].values()))
        for request in record["requests"]:
            request.pop("price_cap_enforced", None)
        self.path.write_text(json.dumps(journal))
        for row in self.wire.locates.values():
            if row["route"] == "LOCATE10":
                row["price"] = "0.08"
        self.client = self.make_client(locate_quote_routes=())
        with self.assertRaises(DasError):
            self.compare()
        self.assertEqual(len(self.paid_commands()), 1)

    def test_runtime_rejects_unsupported_route_assignment_before_any_broker_command(self):
        for settings in ({"locate_offer_routes": ("LOCATE7",)},
                         {"locate_limit_price_routes": ("LOCATE7",)},
                         {"locate_quote_routes": ("LOCATE10",)},
                         {"locate_quote_routes": ("LOCATE99",)}):
            with self.subTest(settings=settings):
                self.client = self.make_client(**settings)
                with self.assertRaises(DasError):
                    self.compare()
                self.assertEqual(self.wire.commands, [])


if __name__ == "__main__":
    unittest.main()
