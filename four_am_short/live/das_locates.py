"""Compare explicit offers, price-capped locates, and opted-in quoted purchases.

Requesting an offer and accepting an offer are different durable phases. A slow
request that has never been accepted cannot block a ready, affordable offer.
Once acceptance might have been sent, only execution reconciliation can resolve
it. A type-0 purchase is sent only for the winning quote after its purchase
intent has been persisted. Uncapped routes require explicit configuration.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from .das import DasError, LocateDeferred, _atom, _locate_size_skip, _number
from .config import OFFER_LOCATE_ROUTES, CAPPED_LOCATE_ROUTES, UNCAPPED_LOCATE_ROUTES
from zoneinfo import ZoneInfo

MARKET_TIME_ZONE = ZoneInfo("America/New_York")


_ENDED = {"canceled", "rejected", "closed", "declined"}
_MODES = {"offer_only", "priced_only"}
_PAID_PENDING = {"accept_pending", "purchase_pending"}
_UNPAID = {"never_accept", "quote_only"}


class OfferManager:
    def __init__(self, client: Any):
        # Construction and observation do not read/write files or send commands.
        self.client = client
        self.records: dict[str, Any] = {}
        self.known: dict[str, tuple[str, dict[str, Any]]] = {}
        self.rows: dict[str, dict[str, Any]] = {}
        self.execution_seen: dict[str, dict[str, Any]] = {}
        self.issues: dict[str, str] = {}
        self.fees: dict[str, str] = {}
        self.replies: dict[tuple[str, str], dict[str, Any]] = {}
        self.started: dict[str, float] = {}
        self.response_at: dict[str, float] = {}
        self.offered_at: dict[str, float] = {}
        self.quoted_at: dict[tuple[str, str], float] = {}
        self.last_service = -float("inf")
        self.still_valid: Callable[[], bool] | None = None

    def _eligible(self) -> bool:
        try:
            return self.still_valid is None or bool(self.still_valid())
        except Exception as exc:
            raise DasError("Cannot verify entry eligibility; no paid locate command sent") from exc

    @staticmethod
    def _never_paid(record: dict[str, Any]) -> bool:
        return bool(record.get("requests")) and all(
            request.get("phase") in _UNPAID and not request.get("execution_evidence")
            for request in record["requests"])

    def _defer_unpaid(self, key: str, reason: str) -> None:
        """Only positively never-sent purchases may release the daily attempt."""
        self._check_evidence()
        record = self.records[key]
        if not self._never_paid(record):
            raise DasError("Entry eligibility failed with a prior paid locate intent; reconcile before retrying")
        # Preserve ownership, including slow offers which have not arrived yet.
        # A cleanup or journal failure remains an ordinary DasError.
        self._reject_ready()
        record.update(state="entry_deferred", deferred_before_paid_send=True,
                      deferred_reason=reason)
        self._save()
        for result in self.client.locate_comparisons.get(record["symbol"], []):
            if result.get("eligible") or result.get("selected"):
                result.update(eligible=False, selected=False, reason=reason)
        raise LocateDeferred(reason)

    def _before_purchase(self, symbol: str, key: str) -> None:
        self._check_evidence()
        if any(row["symbol"] == symbol and row["token"] not in self.known
               and row["status"] not in _ENDED | {"located"}
               for row in self.client._locates.values()):
            self._reject_ready()
            raise DasError("An unowned locate appeared during comparison; no purchase sent")
        if not self._eligible():
            self._defer_unpaid(key, "Entry eligibility expired or trading paused before paid locate; no purchase sent")

    def _paid_send(self, key: str, request: dict[str, Any], command: str) -> None:
        # Recheck after the journal fsync, immediately before the command.
        if not self._eligible():
            request["phase"] = "quote_only" if request["route_type"] == 0 else "never_accept"
            # This function has not called _send. Undo the durable paid intent
            # before recording that a fresh comparison may safely be attempted.
            self.records[key]["state"] = "offers_requested"
            self._save()
            self._defer_unpaid(key, "Entry eligibility expired before paid locate command; no purchase sent")
        self.client._send(command)

    def _load(self) -> None:
        try:
            self.records = self.client._load_journal()["locates"]
            self._index()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise DasError("Cannot read DAS offer journal; no offer request was sent") from exc

    def _index(self) -> None:
        known: dict[str, tuple[str, dict[str, Any]]] = {}
        for key, record in self.records.items():
            if not isinstance(record, dict):
                raise ValueError("invalid journal record")
            if record.get("mode") not in _MODES:
                continue  # Preserve legacy journal records without adopting them.
            if (not isinstance(record.get("requests"), list) or not record.get("symbol")
                    or not isinstance(record.get("shares"), int) or record["shares"] <= 0):
                raise ValueError("invalid requests")
            for request in record["requests"]:
                token = str(request["token"])
                route_type = request.get("route_type", 1)
                if (token in known or route_type not in {0, 1}
                        or type(request.get("price_cap_enforced", True)) is not bool
                        or (not request.get("price_cap_enforced", True)
                            and (route_type != 0 or request["route"] not in UNCAPPED_LOCATE_ROUTES))
                        or request["phase"] not in _UNPAID | _PAID_PENDING | {"located", "failed"}
                        or (route_type == 0 and request["phase"] in {"never_accept", "accept_pending"})
                        or (route_type == 1 and request["phase"] in {"quote_only", "purchase_pending"})):
                    raise ValueError("invalid offer ownership")
                known[token] = (key, request)
                evidence = request.get("execution_evidence")
                if evidence and (token not in self.execution_seen
                                 or evidence["filled"] >= self.execution_seen[token]["filled"]):
                    self.execution_seen[token] = dict(evidence)
        self.known = known

    def _save(self) -> None:
        try:
            self._index()
            journal = self.client._load_journal()
            journal["locates"] = self.records
            self.client._write_journal(journal)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise DasError("Cannot persist DAS offer intent; no further locate command was sent") from exc

    def observe(self, parts: list[str]) -> None:
        """Retain owned execution evidence across snapshot-cache replacement."""
        kind = parts[0].lower()
        if kind.lstrip("$") == "slrouteminchargeret" and len(parts) >= 3:
            _number(parts[2], "minimum locate fee")
            self.fees[parts[1]] = parts[2]
        elif kind == "%slret":
            reply = self.client._locate_return(parts)
            if reply:
                self.replies[(reply["symbol"], reply["route"])] = reply
                self.quoted_at[(reply["symbol"], reply["route"])] = time.monotonic()
        elif kind == "%slorder":
            row = self.client._locates[parts[1]]
            token = row["token"]
            if token not in self.known:
                return
            key, request = self.known[token]
            record = self.records[key]
            if request["phase"] == "quote_only":
                self.issues[token] = "locate order appeared before a purchase was authorized"
            if (row["symbol"] != record["symbol"] or row["shares"] != record["shares"]
                    or row["route"] != request["route"]
                    or (request.get("id") and row["id"] != request["id"])):
                self.issues[token] = "owned offer identity changed"
            previous = self.rows.get(token)
            if previous and previous["id"] != row["id"]:
                self.issues[token] = "one offer token matched multiple locate IDs"
            self.rows[token] = dict(row)
            self.response_at[token] = time.monotonic()
            if row["status"] == "offered":
                self.offered_at.setdefault(token, time.monotonic())
            previous_fill = self.execution_seen.get(token)
            if previous_fill and (row["filled"] < previous_fill["filled"]
                                  or (not row["filled"] and row["status"] != "located")):
                self.issues[token] = "owned locate execution quantity decreased"
            if row["filled"] or row["status"] == "located":
                if request["phase"] not in _UNPAID:
                    price = Decimal(row["price_text"])
                    if (row["filled"] > record["shares"] or (request.get("price_cap_enforced", True) and (
                            price > Decimal(request["quoted_price"])
                            or max(price * record["shares"], Decimal(request["minimum_fee"]))
                            > Decimal(record["max_total_cost"])))):
                        self.issues[token] = "accepted locate execution exceeded its approved quantity or price"
                if not previous_fill or row["filled"] >= previous_fill["filled"]:
                    self.execution_seen[token] = dict(row)

    def _check_evidence(self) -> None:
        for record in self.records.values():
            if isinstance(record, dict) and record.get("reconciliation_required"):
                raise DasError(record.get("reconciliation_reason", "DAS locate journal requires reconciliation"))
        for token, issue in self.issues.items():
            if token in self.known:
                key, request = self.known[token]
                reason = f"DAS {request['route']}: {issue}; no offer will be accepted"
                self.records[key].update(reconciliation_required=True, reconciliation_reason=reason)
                self._save()
                raise DasError(reason)
        for token, row in self.execution_seen.items():
            if token in self.known and self.known[token][1]["phase"] in _UNPAID:
                reason = (f"DAS offer executed without this client's acceptance: "
                          f"{self.client._locate_context(row)}; reconcile before trading")
                self.records[self.known[token][0]].update(
                    reconciliation_required=True, reconciliation_reason=reason)
                self._save()
                raise DasError(reason)
        changed = False
        for token, (_, request) in self.known.items():
            row = self.rows.get(token)
            if row and request["phase"] in _PAID_PENDING and not request.get("id"):
                request["id"] = row["id"]
                changed = True
            evidence = self.execution_seen.get(token)
            if evidence and request["phase"] not in _UNPAID and request.get("execution_evidence") != evidence:
                request["execution_evidence"] = dict(evidence)
                changed = True
        if changed:
            self._save()

    def _current(self, token: str) -> dict[str, Any] | None:
        matches = [row for row in self.client._locates.values() if row["token"] == token]
        if len(matches) > 1:
            self.issues[token] = "offer token matches multiple locate IDs"
            self._check_evidence()
        row = matches[0] if matches else None
        if row is not None:
            key, request = self.known[token]
            record = self.records[key]
            if (row["symbol"] != record["symbol"] or row["route"] != request["route"]
                    or row["shares"] != record["shares"]
                    or (request.get("id") and row["id"] != request["id"])):
                self.issues[token] = "owned offer identity does not match its journal"
                self._check_evidence()
        return row

    def _snapshot(self) -> None:
        try:
            self.client._snapshot("LOCATES", observe=lambda _parts: self._check_evidence())
        finally:
            # A broken snapshot or login burst must not discard execution or
            # identity evidence merely because its end marker never arrived.
            self._check_evidence()

    def _reconcile_paid(self) -> None:
        """No new symbol can hide an unresolved purchase from an earlier scan."""
        for key, record in self.records.items():
            for request in record.get("requests", []):
                if request["phase"] in _PAID_PENDING:
                    self._resolve_acceptance(key, request)

    def _resolve_acceptance(self, key: str, request: dict[str, Any]) -> None:
        """Never turn a possibly paid request into a new purchase attempt."""
        token = request["token"]
        record = self.records[key]
        row = self._current(token)
        operation = "purchase" if request.get("route_type", 1) == 0 else "acceptance"
        if row is None:
            raise DasError(f"DAS {request['route']} {operation} outcome is unconfirmed; "
                           "owned locate missing from snapshot, no alternative purchase will be sent")
        seen = self.execution_seen.get(token)
        if seen and (row["filled"] < seen["filled"]
                     or (not row["filled"] and row["status"] != "located")):
            raise DasError("DAS accepted locate lost execution evidence; reconcile before trading")
        if row["status"] == "located" and row["filled"] == record["shares"]:
            price = Decimal(row["price_text"])
            cost = max(price * record["shares"], Decimal(request["minimum_fee"]))
            if request.get("price_cap_enforced", True) and (
                    price > Decimal(request["quoted_price"]) or cost > Decimal(record["max_total_cost"])):
                raise DasError(f"DAS accepted locate exceeded its approved price: {self.client._locate_context(row)}")
            request.update(phase="located", id=row["id"], actual_price=row["price_text"], total_cost=str(cost))
            request["cost_exceeded_quote"] = cost > max(Decimal(request["quoted_price"]) * record["shares"], Decimal(request["minimum_fee"]))
            request["cost_exceeded_ceiling"] = cost > Decimal(record["max_total_cost"])
            record["state"] = "located"
            self._save()
            return
        if row["status"] in _ENDED and not row["filled"] and not seen:
            request.update(phase="failed", id=row["id"], notes=row.get("notes", ""))
            record["state"] = "failed"
            self._save()
            return
        raise DasError(f"DAS accepted locate remains unconfirmed: {self.client._locate_context(row)}; "
                       "no alternative acceptance will be sent")

    def _reject_ready(self, *, except_token: str = "", only_tokens: set[str] | None = None) -> int:
        """Reject only journal-owned requests that can never be accepted."""
        self._check_evidence()
        ready = []
        for token, (_, request) in self.known.items():
            if only_tokens is not None and token not in only_tokens:
                continue
            if token == except_token or request["phase"] != "never_accept":
                continue
            row = self._current(token)
            if row and row["status"] == "offered" and not row["filled"]:
                ready.append((token, request, row))
        if ready:
            for _, request, row in ready:
                request.update(id=row["id"], reject_pending=True)
            self._save()  # Intent survives disconnect before/after the send.
            for _, request, row in ready:
                self.client._send(f"SLOFFEROPERATION {_atom(row['id'], 'locate id')} Reject")
        return len(ready)

    def service(self) -> dict[str, Any]:
        """Explicit runtime cleanup only; this method never sends Accept."""
        now = time.monotonic()
        if now - self.last_service < 5.0:
            return {"checked": False, "rejected": 0}
        self.last_service = now
        self._load()
        if not self.known:
            return {"checked": False, "rejected": 0}
        self._snapshot()
        rejected = self._reject_ready()
        self._reconcile_paid()
        return {"checked": True, "rejected": rejected}

    def _quote_candidate(self, symbol: str, shares: int, max_price: float,
                         request: dict[str, Any], quote: dict[str, Any] | None,
                         fee: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
        route = request["route"]
        current = self.replies.get((symbol, route))
        result = {
            "route": route, "route_type": 0, "price": quote["price"] if quote else None,
            "price_cap_enforced": request.get("price_cap_enforced", True),
            "quote_only": not request.get("price_cap_enforced", True) and not self.client.settings.allow_uncapped_locate_purchases,
            "available_qty": quote["available_qty"] if quote else 0,
            "minimum_fee": float(fee) if fee is not None else None,
            "total_cost": None, "effective_price": None, "eligible": False, "selected": False,
            "notes": (quote or current or {}).get("notes", ""),
            "reason": "No locate quote received within comparison window",
            "latency_seconds": quote["received_at"] - self.started[request["token"]] if quote else None,
        }
        if not quote:
            return result, quote
        if quote["result"] != "1":
            result["reason"] = f"Route inquiry failed: {quote['notes'] or 'no broker reason supplied'}"
        elif quote["available_qty"] < shares:
            result["reason"] = "Insufficient shares for the complete order"
        elif (not current or current["result"] != "1"
              or Decimal(current["price_text"]) != Decimal(quote["price_text"])
              or current["available_qty"] < shares):
            result["reason"] = "Quote changed after the comparison window"
        elif Decimal(quote["price_text"]) <= 0:
            result["reason"] = "Immediate locate quote must have a positive price"
        elif fee is None:
            result["reason"] = "Minimum charge not received within comparison window"
        elif Decimal(self.fees.get(route, "-1")) != Decimal(fee):
            result["reason"] = "Minimum charge changed after the comparison window"
        else:
            price = Decimal(quote["price_text"])
            total = max(price * shares, Decimal(fee))
            result.update(total_cost=float(total), effective_price=float(total / shares), total_cost_text=str(total))
            if price > Decimal(str(max_price)) or total > Decimal(str(max_price)) * shares:
                result["reason"] = "Effective locate cost exceeds configured ceiling"
            elif result["quote_only"]:
                result["reason"] = "Quote only: route cannot enforce the purchase-price cap"
            else:
                result.update(eligible=True, reason="")
        return result, quote

    def _refresh_uncapped_quote(self, symbol: str, shares: int, route: str, key: str) -> None:
        """Recheck the selected uncapped route immediately before spending."""
        delay = self.client._last_inquiry + 3.0 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._before_purchase(symbol, key)
        self.fees.pop(route, None)
        self.replies.pop((symbol, route), None)
        self.client._send(f"SLRouteMinCharge {route}")
        self.client._send(f"SLPRICEINQUIRE {symbol} {shares} {route}")
        self.client._last_inquiry = time.monotonic()
        self.client._wait(lambda _parts: route in self.fees and (symbol, route) in self.replies,
                          observe=lambda _parts: self._check_evidence())

    def _purchase_quote(self, symbol: str, shares: int, key: str, winner: dict[str, Any],
                        quote: dict[str, Any], result: dict[str, Any], minimum_fee: str
                        ) -> tuple[bool, str, str, float]:
        """Spend once using the authorized purchase mode, then reconcile its token."""
        route, token = winner["route"], winner["token"]
        capped = winner.get("price_cap_enforced", True)
        if not capped:
            if not self.client.settings.allow_uncapped_locate_purchases or route not in UNCAPPED_LOCATE_ROUTES:
                raise DasError("Uncapped locate purchases are not enabled; no purchase was sent")
            try:
                self._refresh_uncapped_quote(symbol, shares, route, key)
            except DasError as exc:
                result.update(eligible=False, reason=f"Selected quote could not be refreshed; no purchase sent: {exc}")
                raise
        skipped = _locate_size_skip(route, shares)
        if skipped is not None:
            raise DasError(skipped["reason"] + "; no purchase was sent")
        price = Decimal(quote["price_text"])
        current_quote = self.replies.get((symbol, route))
        if (self._current(token) is not None or not current_quote or current_quote["result"] != "1"
                or Decimal(current_quote["price_text"]) != price or current_quote["available_qty"] < shares
                or Decimal(self.fees.get(route, "-1")) != Decimal(minimum_fee)
                or max(price * shares, Decimal(minimum_fee)) > Decimal(self.records[key]["max_total_cost"])):
            self._reject_ready()
            result.update(eligible=False, reason="Selected quote or minimum fee changed before purchase")
            raise DasError("DAS selected quote or minimum fee changed before purchase; no purchase was sent")
        self._before_purchase(symbol, key)
        winner.update(phase="purchase_pending", quoted_price=str(price), minimum_fee=minimum_fee)
        self.records[key]["state"] = "purchase_pending"
        self._save()
        result["selected"] = True
        limit_text = f"{price:.4f}" if price.as_tuple().exponent >= -4 else f"{price:f}"
        failure: dict[str, Any] | None = None

        def completed(parts: list[str]) -> bool:
            nonlocal failure
            self._check_evidence()
            reply = self.client._locate_return(parts)
            if reply and reply["symbol"] == symbol and reply["route"] == route and reply["result"] == "2":
                failure = reply
                return True
            row = self._current(token)
            return row is not None and row["status"] in _ENDED | {"located", "offered"}

        try:
            command = f"SLNEWORDER {symbol} {shares} {route} {token}"
            if capped:
                command += f" {limit_text}"
            self._paid_send(key, winner, command)
            self.client._wait(completed)
            # A route-level failure has no token. Reconcile the reserved token
            # rather than interpreting that message as proof no charge occurred.
            self._snapshot()
            self._resolve_acceptance(key, winner)
            locate_id = winner.get("id", "")
            if winner["phase"] != "located":
                notes = winner.get("notes") or (failure or {}).get("notes", "")
                reason = f"DAS {route} did not fill the {'price-limited' if capped else 'quoted-price'} locate: {notes}"
                result.update(eligible=False, reason=reason)
                return False, reason, locate_id, float(price)
            actual_total = Decimal(winner["total_cost"])
            result.update(locate_id=locate_id, actual_price=float(winner["actual_price"]),
                          actual_total_cost=float(actual_total), actual_effective_price=float(actual_total / shares),
                          cost_exceeded_quote=winner["cost_exceeded_quote"],
                          cost_exceeded_ceiling=winner["cost_exceeded_ceiling"], warning="")
            if winner["cost_exceeded_quote"] or winner["cost_exceeded_ceiling"]:
                result["warning"] = ("Warning: uncapped purchase actual cost exceeded "
                                     + ("the configured quote ceiling" if winner["cost_exceeded_ceiling"] else "the quoted cost"))
            if self.client._available_locates(symbol) < shares:
                result.update(eligible=False, reason="Purchased locate shares are not yet available")
                return False, result["reason"], locate_id, float(actual_total / shares)
            return (True, f"DAS {route}: lowest affordable priced locate received; "
                    f"{shares} shares, ${actual_total:.2f} total"
                    + (f". {result['warning']}" if result["warning"] else ""), route,
                    float(actual_total / shares))
        except LocateDeferred as exc:
            result.update(eligible=False, selected=False, reason=str(exc))
            raise
        except DasError as exc:
            notes = f" ({failure['notes']})" if failure and failure["notes"] else ""
            result.update(eligible=False, reason=f"Locate purchase requires reconciliation{notes}: {exc}")
            raise

    def ensure(self, symbol: str, shares: int, max_price: float, *,
               preview: bool = False,
               still_valid: Callable[[], bool] | None = None) -> tuple[bool, str, str, float]:
        self.still_valid = still_valid
        priced_mode = getattr(self.client.settings, "locate_priced_only", False)
        offer_routes = tuple(dict.fromkeys(_atom(route, "offer route").upper()
                                          for route in self.client.settings.locate_offer_routes))
        capped_routes = tuple(dict.fromkeys(_atom(route, "limit-price route").upper()
                                           for route in self.client.settings.locate_limit_price_routes)) if priced_mode else ()
        uncapped_routes = tuple(dict.fromkeys(_atom(route, "quote route").upper()
                                             for route in self.client.settings.locate_quote_routes)) if priced_mode else ()
        quote_routes = uncapped_routes + capped_routes
        routes = offer_routes + quote_routes
        if preview and (not (priced_mode or getattr(self.client.settings, "locate_offer_only", False))
                        or not set(offer_routes) <= OFFER_LOCATE_ROUTES
                        or not set(capped_routes) <= CAPPED_LOCATE_ROUTES
                        or not set(uncapped_routes) <= UNCAPPED_LOCATE_ROUTES):
            raise DasError("Locate preview requires confirmed offer and quote routes in a targeted locate mode")
        if (not routes or len(set(routes)) != len(routes)
                or not set(offer_routes) <= OFFER_LOCATE_ROUTES
                or not set(capped_routes) <= CAPPED_LOCATE_ROUTES
                or not set(uncapped_routes) <= UNCAPPED_LOCATE_ROUTES
                or type(self.client.settings.allow_uncapped_locate_purchases) is not bool):
            raise DasError("Supported routes are LOCATE4/LOCATE6 offers, capped LOCATE10, "
                           "and quote routes LOCATE1/LOCATE7/LOCATE8/LOCATE12/LOCATE14")
        self._load()
        key = f"{datetime.now(MARKET_TIME_ZONE):%Y-%m-%d}:{symbol}"
        previous = self.records.get(key)
        prior_paid_unresolved = bool(preview and previous and (
            previous.get("state") in _PAID_PENDING or any(
                request.get("phase") in _PAID_PENDING for request in previous.get("requests", []))))
        preview_warning = (" WARNING: Existing paid locate attempt remains unresolved; "
                           "this preview does not reconcile or retry it.") if prior_paid_unresolved else ""
        self._snapshot()
        if not preview:
            self._reconcile_paid()
        # An independent GUI/API request must be reconciled instead of allowing
        # a second paid workflow on this symbol.
        if any(row["symbol"] == symbol and row["token"] not in self.known
               and row["status"] not in _ENDED | {"located"}
               for row in self.client._locates.values()):
            raise DasError("An unowned unresolved DAS locate exists for this symbol; no new request sent")
        # This guard precedes ETB and available-locate shortcuts. Existing shares
        # cannot conceal an acceptance whose cost/quantity remains unconfirmed.
        if previous and not preview:
            if previous.get("mode") not in _MODES:
                raise DasError("DAS has a prior legacy locate attempt today; reconcile it before using offer-only execution")
            for request in previous["requests"]:
                if request["phase"] in _PAID_PENDING:
                    self._resolve_acceptance(key, request)
        asset = self.client.get_asset(symbol)
        if asset["short_prohibited"]:
            return False, "DAS prohibits shorting this symbol" + preview_warning, "", 0.0
        if not preview and asset["shortable"] and asset["short_size"] >= shares:
            return True, "DAS confirms shortable size; no offer purchase needed", "", 0.0
        if not preview and self.client._available_locates(symbol) >= shares:
            return True, "DAS confirms existing located shares", "existing", 0.0
        if previous and not preview:
            if not (previous.get("state") == "entry_deferred"
                    and previous.get("deferred_before_paid_send") is True
                    and self._never_paid(previous)):
                raise DasError("DAS locate already attempted today; no duplicate offer request or acceptance was sent")
            self._reject_ready()
        # Reconcile prior paid intents and reuse existing borrow BEFORE applying
        # current route minimums. A minimum must not hide an old uncertain buy.
        skipped = [{**row, "route_type": 1 if route in offer_routes else 0}
                   for route in routes if (row := _locate_size_skip(route, shares)) is not None]
        offer_routes = tuple(route for route in offer_routes if _locate_size_skip(route, shares) is None)
        quote_routes = tuple(route for route in quote_routes if _locate_size_skip(route, shares) is None)
        routes = offer_routes + quote_routes
        if skipped:
            self.client.locate_comparisons[symbol] = list(skipped)
        if not routes:
            note = "No eligible locate route: " + "; ".join(row["reason"] for row in skipped)
            return False, note + preview_warning, "", 0.0
        if preview:
            # Keep diagnostic attempts separate from the strategy's one-attempt
            # daily key, while retaining ownership for normal late-offer cleanup.
            key = f"{datetime.now(MARKET_TIME_ZONE):%Y-%m-%d}:preview:{uuid.uuid4().hex}:{symbol}"
        used = set(self.known)
        requests = []
        for route in routes:
            for _ in range(100):
                token = self.client.new_client_order_id()
                if token not in used:
                    break
            else:
                raise DasError("Could not reserve unique DAS offer tokens")
            used.add(token)
            requests.append({"route": route, "token": token,
                             "route_type": 1 if route in offer_routes else 0,
                             "price_cap_enforced": route not in uncapped_routes,
                             "phase": "never_accept" if route in offer_routes else "quote_only"})
        if previous and not preview:
            # Keep every old token owned and permanently unpaid. Late offers
            # are rejected by cleanup and never enter the new candidate list.
            archive_key = f"{key}:deferred:{uuid.uuid4().hex}"
            self.records[archive_key] = previous
        self.records[key] = {
            "mode": "priced_only" if priced_mode else "offer_only",
            "symbol": symbol, "shares": shares, "state": "offers_requested",
            "max_total_cost": str(Decimal(str(max_price)) * shares), "requests": requests,
        }
        if preview:
            self.records[key]["preview"] = True
            if prior_paid_unresolved:
                self.records[key]["prior_paid_unresolved"] = True
        self._save()
        # Observe the broker's three-second inquiry throttle before starting
        # each targeted quote. Quotes cannot incur a locate purchase. Capped
        # quotes and explicit offers are requested after uncapped inquiries;
        # the uncapped winner is refreshed again immediately before purchase.
        for request in sorted(requests, key=lambda item: item["route_type"]):
            route, token = request["route"], request["token"]
            if request["route_type"] == 0:
                delay = self.client._last_inquiry + 3.0 - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            self.fees.pop(route, None)
            self.replies.pop((symbol, route), None)
            self.quoted_at.pop((symbol, route), None)
            self.client._send(f"SLRouteMinCharge {route}")
            self.started[token] = time.monotonic()
            if request["route_type"] == 0:
                self.client._send(f"SLPRICEINQUIRE {symbol} {shares} {route}")
                self.client._last_inquiry = time.monotonic()
            else:
                self.client._send(f"SLNEWORDER {symbol} {shares} {route} {token}")

        deadline = time.monotonic() + self.client.settings.locate_quote_wait_seconds
        while time.monotonic() < deadline:
            line = self.client._line(deadline, allow_idle=True)
            if line is None:
                break
            if line:
                self.client._process_line(line)
                self._check_evidence()
        offers_at_cutoff = {request["token"]: dict(self.rows[request["token"]]) for request in requests
                            if self.rows.get(request["token"], {}).get("status") == "offered"}
        quotes_at_cutoff = {route: {**self.replies[(symbol, route)], "received_at": self.quoted_at[(symbol, route)]}
                            for route in quote_routes
                            if (symbol, route) in self.replies}
        fees_at_cutoff = {route: self.fees[route] for route in routes if route in self.fees}
        self._snapshot()  # Own-token snapshots do not wait for pending routes.
        ceiling = Decimal(str(max_price)) * shares
        comparisons = list(skipped)
        candidates = []
        for request in requests:
            token, route = request["token"], request["route"]
            row = self._current(token)
            fee = (fees_at_cutoff if priced_mode else self.fees).get(route)
            reply = self.replies.get((symbol, route))
            if request["route_type"] == 0:
                result, quote = self._quote_candidate(
                    symbol, shares, max_price, request, quotes_at_cutoff.get(route),
                    fees_at_cutoff.get(route))
                comparisons.append(result)
                if result["eligible"]:
                    candidates.append((Decimal(result["total_cost_text"]), Decimal(quote["price_text"]),
                                       routes.index(route), request, quote, result, fees_at_cutoff[route]))
                continue
            notes = row.get("notes", "") if row else (reply or {}).get("notes", "")
            result = {"route": route, "route_type": 1, "price": row["price"] if row else None,
                      "available_qty": row["open"] if row else 0, "minimum_fee": float(fee) if fee is not None else None,
                      "total_cost": None, "effective_price": None, "eligible": False, "selected": False,
                      "notes": notes, "reason": "No priced offer received within comparison window",
                      "latency_seconds": ((self.offered_at if row and row["status"] == "offered"
                                           else self.response_at)[token] - self.started[token])
                      if token in self.response_at else None}
            if row:
                result.update(locate_id=row["id"], status=row["status"])
                if row["status"] != "offered":
                    result["reason"] = f"{row['status']}: skipped before acceptance"
                elif token not in offers_at_cutoff:
                    result["reason"] = "Late offer: never accepted after the comparison window"
                elif (row["id"] != offers_at_cutoff[token]["id"]
                      or Decimal(row["price_text"]) != Decimal(offers_at_cutoff[token]["price_text"])):
                    result["reason"] = "Offer changed after the comparison window"
                elif row["open"] != shares or row["shares"] != shares:
                    result["reason"] = "Offer does not cover the complete requested quantity"
                elif fee is None:
                    result["reason"] = "Minimum charge not received"
                elif Decimal(self.fees.get(route, "-1")) != Decimal(fee):
                    result["reason"] = "Minimum charge changed after the comparison window"
                else:
                    price = Decimal(row["price_text"])
                    total = max(price * shares, Decimal(fee))
                    result.update(total_cost=float(total), effective_price=float(total / shares))
                    if price > Decimal(str(max_price)) or Decimal(fee) > ceiling or total > ceiling:
                        result["reason"] = "Effective offer cost exceeds configured ceiling"
                    else:
                        result.update(eligible=True, reason="")
                        candidates.append((total, price, routes.index(route), request, dict(row), result, fee))
            elif reply and reply["result"] == "2":
                result["reason"] = f"Offer request failed: {notes or 'no broker reason supplied'}"
            comparisons.append(result)
        self.client.locate_comparisons[symbol] = comparisons
        if preview:
            # This return is deliberately before either paid path, including
            # the winning route's acceptance or price-limited purchase intent.
            # Only this diagnostic's offers may be rejected here.
            preview_tokens = {request["token"] for request in requests}
            self._reject_ready(only_tokens=preview_tokens)
            self._snapshot()
            self._reject_ready(only_tokens=preview_tokens)
            self.records[key]["state"] = "previewed"
            self._save()
            return (bool(candidates), f"DAS locate preview: {len(candidates)} eligible route(s); "
                    "priced offers rejected, no shares purchased" + preview_warning, "", 0.0)
        if not candidates:
            self._reject_ready()
            return False, "No affordable full-size priced locate arrived; unresolved requests will never be accepted", "", 0.0

        _, price, _, winner, offered, result, minimum_fee = min(candidates, key=lambda item: item[:3])
        self._reject_ready(except_token=winner["token"])
        self._snapshot()
        if winner["route_type"] == 0:
            return self._purchase_quote(symbol, shares, key, winner, offered, result, minimum_fee)
        current = self._current(winner["token"])
        if (not current or current["status"] != "offered" or current["id"] != offered["id"]
                or current["filled"] or current["open"] != shares
                or Decimal(current["price_text"]) != price
                or Decimal(self.fees.get(winner["route"], "-1")) != Decimal(minimum_fee)
                or max(price * shares, Decimal(minimum_fee)) > ceiling):
            self._reject_ready()
            raise DasError("DAS selected offer or minimum fee changed before acceptance; no acceptance was sent")
        # Sending rejects is harmless for other never-accepted requests. Their
        # acknowledgments, Pending and Waiting responses do not gate this offer.
        self._before_purchase(symbol, key)
        winner.update(phase="accept_pending", id=offered["id"], quoted_price=str(price),
                      minimum_fee=minimum_fee)
        self.records[key]["state"] = "accept_pending"
        self._save()
        result["selected"] = True
        def accepted(parts: list[str]) -> bool:
            self._check_evidence()
            row = self._current(winner["token"])
            return parts[0].lower() == "%slorder" and row is not None and row["status"] in _ENDED | {"located"}

        try:
            self._paid_send(key, winner, f"SLOFFEROPERATION {_atom(offered['id'], 'locate id')} Accept")
            self.client._wait(accepted)
            self._check_evidence()
            self._resolve_acceptance(key, winner)
            if winner["phase"] != "located":
                reason = f"DAS {winner['route']} did not fill the accepted offer: {winner.get('notes', '')}"
                result.update(eligible=False, reason=reason)
                return False, reason, offered["id"], float(price)
            if self.client._available_locates(symbol) < shares:
                result.update(eligible=False, reason="Accepted locate shares are not yet available")
                return False, result["reason"], offered["id"], float(price)
            return (True, f"DAS {winner['route']}: lowest affordable priced offer received; "
                    f"{shares} shares, ${Decimal(winner['total_cost']):.2f} total", winner["route"],
                    float(Decimal(winner["total_cost"]) / shares))
        except LocateDeferred as exc:
            result.update(eligible=False, selected=False, reason=str(exc))
            raise
        except DasError as exc:
            result.update(eligible=False, reason=f"Accepted offer requires reconciliation: {exc}")
            raise
