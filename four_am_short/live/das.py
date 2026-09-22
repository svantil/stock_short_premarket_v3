"""Account-scoped DAS CMD transport for the independent 4am short strategy.

All market data comes from Alpaca. This module only reads broker state, arranges
borrow and submits/cancels limit orders. A timeout never proves rejection.
Protocol fields follow the account owner's CMD API Manual (July 2025 revision).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})


class DasError(RuntimeError):
    """A failed or unconfirmed DAS operation."""


class OrderRejected(DasError):
    """Exact full submission definitively rejected with no execution evidence."""


class OrderSubmissionUncertain(DasError):
    """A command may have reached DAS; only reconcile, never blindly retry."""


def _atom(value: object, label: str) -> str:
    text = str(value)
    if not text or not re.fullmatch(r"[!-~]+", text):
        raise DasError(f"DAS {label} must be one nonempty ASCII token")
    return text

def _order_route(value: object, label: str = "order route") -> str:
    route = _atom(value, label).upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]*", route, flags=re.ASCII):
        raise DasError(f"DAS {label} must be one literal ASCII route name")
    if route in {"ALL", "ALLROUTE", "ALLROUTEWTTYPE1"}:
        raise DasError(f"DAS {label} cannot use a broadcast route")
    if route in {"LIMIT", "MARKET", "STOP"}:
        raise DasError(f"DAS {label} must use the CMD base route SMAT for Montage {route}")
    montage_bases = {"ARCAEL": "ARCAE", "CBATSL": "CBATS"}
    if route in montage_bases:
        raise DasError(f"DAS {label} must use CMD base route {montage_bases[route]} for Montage {route}")
    return route

def _number(value: str, label: str, *, integer: bool = False) -> float | int:
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise DasError(f"Invalid DAS {label}") from exc
    if not math.isfinite(result) or result < 0 or (integer and not result.is_integer()):
        raise DasError(f"Invalid DAS {label}")
    return int(result) if integer else result

def _locate_size_skip(route: str, shares: int) -> dict[str, Any] | None:
    """Account-owner-confirmed route minimum; never increase requested size."""
    if route.upper() != "LOCATE10" or shares >= 100:
        return None
    return {
        "route": route, "minimum_shares": 100, "requested_shares": shares,
        "status": "skipped", "eligible": False, "selected": False,
        "price": None, "available_qty": 0, "minimum_fee": None,
        "total_cost": None, "effective_price": None, "latency_seconds": None,
        "notes": "", "reason": f"Skipped: LOCATE10 requires at least 100 shares; requested {shares}",
    }


class DasClient:
    strict_reconciliation = True

    def __init__(self, settings: Any, *, journal_path: Path,
                 socket_factory: Callable[..., socket.socket] = socket.create_connection):
        self.settings = settings
        self.order_routes = tuple(_order_route(value) for value in
                                  (settings.route, settings.backup_route) if value)
        if not self.order_routes or len(set(self.order_routes)) != len(self.order_routes):
            raise DasError("Configure a primary DAS route and a distinct optional backup route")
        self.mode = "PAPER" if settings.paper else "LIVE"
        self.endpoint = f"das://{settings.host}:{settings.port}/{self.mode.lower()}"
        self._socket_factory = socket_factory
        self._socket: socket.socket | None = None
        self._buffer = b""
        self._lock = threading.RLock()
        # Writers hold the transport lock and replace this whole dictionary.
        # UI/event-loop readers never wait for a blocked socket operation.
        self._connection_status: dict[str, Any] = {
            "connected": False, "checking": False, "connecting": False,
            "last_checked_at": None, "last_connected_at": None,
            "last_disconnected_at": None, "last_error": None,
        }
        self._orders: dict[str, dict[str, Any]] = {}
        self._trades: dict[str, dict[str, Any]] = {}
        self._positions: dict[tuple[str, int], dict[str, Any]] = {}
        self._locates: dict[str, dict[str, Any]] = {}
        self._sent: set[str] = set()
        self._order_requests: dict[str, dict[str, Any]] = {}
        self._cancel_at: dict[str, float] = {}
        self._last_inquiry = -math.inf
        self.locate_comparisons: dict[str, list[dict[str, Any]]] = {}
        self._offer_manager: Any = None
        self._account_lock: Any = None
        self._journal_lock: Any = None
        self._journal_path = Path(journal_path).expanduser().resolve()
        # PAPER is intentionally absent: a label cannot create a separate account.
        identity = f"{settings.host.strip().lower()}:{settings.port}:{settings.account}"
        self._identity = hashlib.sha256(identity.encode()).hexdigest()
        self._account_lock_path = Path(tempfile.gettempdir()) / f"4am_short_das_{self._identity}.lock"

    @property
    def connection_status(self) -> dict[str, Any]:
        """Nonblocking snapshot of the most recently observed CMD connection health."""
        snapshot = self._connection_status
        return dict(snapshot)

    def _connection_update(self, **changes: Any) -> None:
        self._connection_status = {**self._connection_status, **changes}

    def _connection_ready(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # Latest successful verification, including checks on the same session.
        self._connection_update(connected=True, checking=False, connecting=False,
                                last_checked_at=now, last_connected_at=now, last_error=None)

    @staticmethod
    def _buying_power(parts: list[str]) -> float:
        if len(parts) < 2:
            raise DasError("DAS buying-power response is incomplete")
        return float(_number(parts[1], "buying power"))

    @staticmethod
    def _file_lock(path: Path) -> Any:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise
        return handle

    def _claim_account(self) -> None:
        if self._account_lock is not None:
            return
        try:
            self._account_lock = self._file_lock(self._account_lock_path)
            self._journal_lock = self._file_lock(self._journal_path.with_suffix(".lock"))
            records = self._load_journal()
            self._order_requests = records["orders"]
            self._sent.update(self._order_requests)
            # Load owned locate tokens before LOGIN delivers unsolicited rows.
            self._offers()._load()
            if not self._journal_path.exists():
                self._write_journal(records)
        except (OSError, DasError) as exc:
            self.close()
            if isinstance(exc, DasError):
                raise
            raise DasError("Another process owns this DAS account or journal; stop it before connecting") from exc

    def _load_journal(self) -> dict[str, Any]:
        if not self._journal_path.exists():
            return {"schema": 1, "identity": self._identity, "orders": {}, "locates": {}}
        try:
            records = json.loads(self._journal_path.read_text())
            if (not isinstance(records, dict) or records.get("schema") != 1
                    or records.get("identity") != self._identity
                    or not isinstance(records.get("orders"), dict)
                    or not isinstance(records.get("locates"), dict)):
                raise ValueError("journal identity/schema mismatch")
            for token, request in records["orders"].items():
                if (not token.isdigit() or not isinstance(request, dict)
                        or not {"symbol", "qty", "das_side", "route", "price"} <= request.keys()):
                    raise ValueError("invalid stock-order journal")
            return records
        except (OSError, ValueError, TypeError) as exc:
            raise DasError("DAS journal invalid or belongs to another account; preserve it and reconcile") from exc

    def _write_journal(self, records: dict[str, Any]) -> None:
        temporary = self._journal_path.with_suffix(".tmp")
        try:
            self._journal_path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w") as stream:
                os.chmod(temporary, 0o600)
                json.dump(records, stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._journal_path)
            # Persist rename metadata before any order/acceptance command.
            if os.name != "nt":
                descriptor = os.open(self._journal_path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except (OSError, ValueError, TypeError) as exc:
            raise DasError("Cannot persist DAS intent; no further order/locate command was sent") from exc

    def _save_orders(self) -> None:
        records = self._load_journal()
        records["orders"] = self._order_requests
        self._write_journal(records)

    def _offers(self) -> Any:
        if self._offer_manager is None:
            from .das_locates import OfferManager
            self._offer_manager = OfferManager(self)
        return self._offer_manager

    def ensure_shortable(self, symbol: str, shares: int, max_price: float, *,
                         still_valid: Callable[[], bool] | None = None) -> tuple[bool, str, str, float]:
        with self._lock:
            symbol = _atom(symbol, "symbol")
            if (type(shares) is not int or shares <= 0 or not math.isfinite(max_price) or max_price < 0):
                raise DasError("Invalid locate share quantity or fee ceiling")
            if not (self.settings.locate_offer_only or self.settings.locate_priced_only):
                raise DasError("Use explicit offer routes and/or confirmed price-capped locate routes")
            self._claim_account()
            return self._offers().ensure(symbol, shares, max_price, still_valid=still_valid)

    def service_locate_offers(self) -> dict[str, Any]:
        """Reject late unselected offers; never initiates a paid purchase."""
        with self._lock:
            self._claim_account()
            return self._offers().service()

    def new_client_order_id(self) -> str:
        # Positive signed 32-bit tokens, also persisted by the supervisor before send.
        with self._lock:
            used = (self._sent | {x["client_order_id"] for x in self._orders.values()}
                    | {x["token"] for x in self._locates.values()})
            if self._offer_manager is not None:
                used |= set(self._offer_manager.known)
            while True:
                token = str(secrets.randbelow(2_147_483_646) + 1)
                if token not in used:
                    return token

    def close(self) -> None:
        with self._lock:
            self._disconnect()
            if self._account_lock is not None:
                self._account_lock.close()
                self._account_lock = None
            if self._journal_lock is not None:
                self._journal_lock.close()
                self._journal_lock = None

    def _disconnect(self, error: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._connection_update(connected=False, checking=False, connecting=False,
                                last_checked_at=now, last_disconnected_at=now,
                                last_error=self._broker_notes(error) if error else self._connection_status["last_error"])
        # Invalidate before touching the socket, including if close itself fails.
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None
        self._buffer = b""
        self._orders.clear()
        self._trades.clear()
        self._positions.clear()
        self._locates.clear()

    def _send(self, command: str) -> None:
        if "\r" in command or "\n" in command:
            raise DasError("Invalid multiline DAS command")
        try:
            assert self._socket is not None
            self._socket.settimeout(self.settings.timeout_seconds)
            self._socket.sendall((command + "\r\n").encode("ascii"))
        except (OSError, UnicodeError) as exc:
            error = "DAS connection lost while sending command"
            self._disconnect(error)
            raise DasError(error) from exc

    def _line(self, deadline: float, *, allow_idle: bool = False) -> str | None:
        try:
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                assert self._socket is not None
                self._socket.settimeout(remaining)
                data = self._socket.recv(65536)
                if not data:
                    raise OSError("closed")
                self._buffer += data
                if len(self._buffer) > 1_048_576:
                    raise OSError("DAS response exceeds maximum line length")
            raw, self._buffer = self._buffer.split(b"\n", 1)
            return raw.decode("ascii").strip()
        except TimeoutError as exc:
            if allow_idle and not self._buffer:
                return None
            error = "DAS response timeout; result is unconfirmed"
            self._disconnect(error)
            raise DasError(error) from exc
        except (OSError, UnicodeError) as exc:
            error = "DAS response timeout or disconnected session; result is unconfirmed"
            self._disconnect(error)
            raise DasError(error) from exc

    def _process_line(self, line: str) -> list[str]:
        parts = line.split()
        if not parts:
            return []
        lower = line.lower()
        if lower.startswith("#orderserver:") and any(
            status in lower for status in ("failed", "disconnect", "lost connection", "missing heartbeat")
        ):
            error = "DAS order server is disconnected or not authenticated"
            if "missing heartbeat" in lower:
                error += " (missing order-server heartbeat)"
            elif "failed" in lower and "logon" in lower:
                error += " (order-server logon failed)"
            elif "failed" in lower and "connect" in lower:
                error += " (order-server connection failed)"
            else:
                error += " (order-server connection lost)"
            self._disconnect(error)
            raise DasError(error)
        if lower.startswith("#login") and any(x in lower for x in ("fail", "invalid", "denied")):
            error = "DAS login failed; check account and CMD API permission"
            self._disconnect(error)
            raise DasError(error)
        previous = json.dumps(self._order_requests, sort_keys=True)
        try:
            try:
                self._ingest(parts)
                if self._offer_manager is not None:
                    self._offer_manager.observe(parts)
            finally:
                # Contradictory evidence must survive disconnects and restarts.
                if json.dumps(self._order_requests, sort_keys=True) != previous:
                    self._save_orders()
        except (IndexError, ValueError, DasError) as exc:
            error = f"Unsupported or malformed DAS {parts[0]} response; reconciliation required"
            self._disconnect(error)
            raise DasError(error) from exc
        return parts

    def _wait(self, predicate: Callable[[list[str]], bool], *,
              observe: Callable[[list[str]], None] | None = None) -> list[str]:
        deadline = time.monotonic() + self.settings.timeout_seconds
        while True:
            line = self._line(deadline)
            if not line:
                continue
            parts = self._process_line(line)
            if observe is not None:
                observe(parts)
            if predicate(parts):
                return parts
            if time.monotonic() >= deadline:
                error = "DAS operation timed out; result is unconfirmed"
                self._disconnect(error)
                raise DasError(error)

    def _connect(self) -> None:
        if self._socket is not None:
            return
        self._connection_update(checking=True, connecting=True)
        # Keep local ownership failures separate from network/authentication failures.
        try:
            username = _atom(self.settings.username, "username")
            password = _atom(self.settings.password, "password")
            account = _atom(self.settings.account, "account")
            self._claim_account()
        except DasError as exc:
            self._disconnect(str(exc))
            raise
        except OSError as exc:
            error = "DAS local account lock could not be opened; check state-directory permissions"
            self._disconnect(error)
            raise DasError(error) from exc
        try:
            self._socket = self._socket_factory(
                (self.settings.host, self.settings.port), timeout=self.settings.timeout_seconds
            )
        except OSError as exc:
            if isinstance(exc, TimeoutError):
                reason = "timed out; check the Windows VM address, CMD port and firewall"
            elif isinstance(exc, ConnectionRefusedError):
                reason = "refused; check that DAS is running with CMD API enabled on this port"
            elif isinstance(exc, socket.gaierror):
                reason = "hostname resolution failed; check DAS_HOST"
            elif isinstance(exc, PermissionError):
                reason = "permission denied; check local network permissions or sandbox restrictions"
            else:
                reason = f"failed (OS errno {exc.errno}); check the network connection to Windows"
            # Never include the raw exception, which may contain supplied values.
            error = f"DAS TCP connection {reason}. No login was sent"
            self._disconnect(error)
            raise DasError(error) from exc

        ended: set[str] = set()
        started: set[str] = set()
        snapshots = {"#posend": "POSITIONS", "#orderend": "ORDERS", "#tradeend": "TRADES"}
        stage = "login/account snapshots"
        try:
            self._send(f"LOGIN {username} {password} {account} 0")
            # DAS sends these complete snapshots only after successful login.
            def ready(p: list[str]) -> bool:
                if p[0].lower() in {"#pos", "#order", "#trade"}:
                    started.add(p[0].lower() + "end")
                if p[0].lower() in snapshots and p[0].lower() in started:
                    ended.add(p[0].lower())
                return len(ended) == 3
            self._wait(ready)
            # Drain the login burst before issuing a subsequent snapshot request.
            stage = "buying-power response"
            self._send("GET BP")
            self._buying_power(self._wait(lambda p: p[0] == "BP"))
            self._connection_ready()
        except (OSError, DasError) as exc:
            detail = str(exc) if isinstance(exc, DasError) else "connection closed or unavailable"
            missing = ", ".join(name for marker, name in snapshots.items() if marker not in ended)
            suffix = f". Missing completed snapshots: {missing}" if missing else ""
            error = f"DAS connection failed during {stage}: {detail}{suffix}"
            self._disconnect(error)
            raise DasError(error) from exc

    def _snapshot(self, name: str, *, observe: Callable[[list[str]], None] | None = None) -> None:
        self._connect()
        begin, end = {"POSITIONS": ("#pos", "#posend"), "ORDERS": ("#order", "#orderend"),
                      "TRADES": ("#trade", "#tradeend"), "LOCATES": ("#slorder", "#slorderend")}[name]
        self._send(f"GET {name}")
        started = False
        def done(p: list[str]) -> bool:
            nonlocal started
            if p[0].lower() == begin:
                started = True
            return started and p[0].lower() == end
        self._wait(done, observe=observe)

    def _broker_notes(self, value: str) -> str:
        """Keep useful broker reasons without credentials or control sequences."""
        value = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
        for secret in sorted({str(getattr(self.settings, field, ""))
                              for field in ("username", "password", "account")}, key=len, reverse=True):
            if secret:
                value = re.sub(re.escape(secret), "[redacted]", value, flags=re.IGNORECASE)
        return " ".join(value.split())[:320]

    def _locate_return(self, p: list[str]) -> dict[str, Any] | None:
        # Notes may contain spaces; Account is the last field, not field seven.
        if (not p or p[0].lower() != "%slret" or len(p) < 7
                or p[-1] != self.settings.account):
            return None
        if p[1] not in {"1", "2"}:
            raise DasError("Unsupported DAS locate response type")
        return {"result": p[1], "symbol": p[2], "route": p[5],
                "price": float(_number(p[3], "locate quote")), "price_text": p[3],
                "available_qty": int(_number(p[4], "locate size", integer=True)),
                "notes": self._broker_notes(" ".join(p[6:-1]))}

    def _locate_context(self, locate: dict[str, Any]) -> str:
        detail = (f"{locate['route']} id={locate['id']} status={locate['status']} "
                  f"shares={locate['shares']}")
        return detail + (f": {locate['notes']}" if locate.get("notes") else "")

    def _ingest(self, p: list[str]) -> None:
        kind = p[0].lower()
        if kind == "#pos":
            self._positions.clear()
        elif kind == "#order":
            self._orders.clear()
        elif kind == "#trade":
            self._trades.clear()
        elif kind == "#slorder":
            self._locates.clear()
        elif kind == "%pos":
            position_type = int(p[2])
            if position_type not in {1, 2, 3}:
                raise DasError("Unsupported position type")
            qty = _number(p[3], "position quantity", integer=True)
            self._positions[(p[1], position_type)] = {
                "symbol": p[1], "qty": -qty if position_type == 3 else qty,
                "avg_entry_price": _number(p[4], "position cost"),
            }
        elif kind == "%order":
            if len(p) < 16 and len(p) > 2 and p[2] in self._order_requests:
                self._order_requests[p[2]]["uncertain"] = "incomplete owned order response"
            if len(p) < 13:
                raise DasError("Incomplete order")
            # Newer versions append account/trader/orderSrc; legacy sessions are login-scoped.
            if len(p) < 16:
                raise DasError("DAS order response lacks account identity; update DAS")
            if p[14] != self.settings.account:
                if p[2] in self._order_requests:
                    self._order_requests[p[2]]["identity_conflict"] = True
                    self._order_requests[p[2]]["uncertain"] = "owned token appeared on another account"
                return
            if p[4] not in {"B", "S", "SS"}:
                raise DasError("Unsupported order side")
            if p[2] in self._order_requests:
                request = self._order_requests[p[2]]
                request["order_seen"] = True
                if (request["symbol"] != p[3] or request["das_side"] != p[4]
                        or request["qty"] != _number(p[6], "order quantity", integer=True)
                        or request["route"] != p[10]
                        or Decimal(request["price"]) != Decimal(p[9])
                        or request.get("broker_order_id", p[1]) != p[1]):
                    request["identity_conflict"] = True
                request["broker_order_id"] = p[1]
            self._orders[p[1]] = {
                "id": p[1], "client_order_id": p[2], "symbol": p[3], "das_side": p[4],
                "side": "buy" if p[4] == "B" else "sell", "type": p[5],
                "qty": _number(p[6], "order quantity", integer=True),
                "leaves": _number(p[7], "open quantity", integer=True),
                "canceled": _number(p[8], "canceled quantity", integer=True),
                "limit_price": _number(p[9], "order price"), "route": p[10],
                "das_status": p[11].lower(),
            }
        elif kind == "%orderact":
            self._observe_order_action(p)
        elif kind == "%trade":
            if p[8] == "0":
                for request in self._order_requests.values():
                    if request["symbol"] == p[2]:
                        request["uncertain"] = "execution has no broker order identity"
            self._trades[p[1]] = {
                "id": p[1], "symbol": p[2], "side": p[3],
                "qty": _number(p[4], "execution quantity", integer=True),
                "price": _number(p[5], "execution price"), "order_id": p[8],
                "execution_clock": p[7],
            }
            # DAS sends the execution clock without a date. Own orders bind it
            # to their durable Eastern submission date; this strategy is intraday.
            datetime.fromisoformat(f"2000-01-01T{p[7]}")
            for request in self._order_requests.values():
                if request.get("broker_order_id") == p[8]:
                    evidence = request.setdefault("executions", {})
                    if p[1] in evidence and evidence[p[1]] != self._trades[p[1]]:
                        request["identity_conflict"] = True
                    evidence[p[1]] = self._trades[p[1]].copy()
        elif kind == "%slorder":
            if len(p) < 12:
                raise DasError("DAS locate response lacks token; update DAS")
            self._locates[p[1]] = {
                "id": p[1], "symbol": p[2], "shares": _number(p[3], "locate shares", integer=True),
                "open": _number(p[4], "locate open shares", integer=True),
                "filled": _number(p[5], "located shares", integer=True),
                "price": _number(p[6], "locate price"), "status": p[7].lower(), "route": p[8],
                "token": p[11], "price_text": p[6],
                "notes": self._broker_notes(" ".join(p[12:])),
            }
            locate = self._locates[p[1]]
            if (locate["status"] not in {"pending", "waiting", "located", "offered", "canceled",
                                          "rejected", "closed", "declined"}
                    or locate["filled"] + locate["open"] > locate["shares"]):
                raise DasError("Invalid locate status/quantity")

    def _observe_order_action(self, p: list[str]) -> None:
        # %OrderAct id action side symbol qty price route time [notes...] token
        # The notes field has variable length; token is always the last field.
        request = self._order_requests.get(p[-1])
        if request is None:
            return
        if len(p) < 10:
            request["uncertain"] = "incomplete order action"
            return
        action = p[2].lower()
        side = {"shrt": "SS", "ss": "SS", "buy": "B", "b": "B"}.get(p[3].lower())
        identity_matches = (p[4] == request["symbol"] and side == request["das_side"]
                            and p[7].upper() == request["route"].upper())
        if not identity_matches:
            request["uncertain"] = "order action identity does not match its submission"
            return
        if action == "sending":
            return
        if action != "send_rej":
            # Accept/Execute/TimeOut/CancelRej/ReplaceRej cannot establish that
            # the original order was rejected without entering the market.
            request["uncertain"] = "order activity requires broker-order reconciliation"
            return
        try:
            quantity = _number(p[5], "rejected quantity", integer=True)
            _number(p[6], "rejected price")
            price = Decimal(p[6])
        except DasError:
            # Keep the contradiction even if malformed input disconnects this
            # session and subsequent snapshots contain no order information.
            request["uncertain"] = "malformed rejection quantity or price"
            raise
        if p[1] != "0" or quantity != request["qty"] or price != Decimal(request["price"]):
            request["uncertain"] = "rejection does not identify a complete unaccepted submission"
            return
        request["rejection"] = self._broker_notes(" ".join(p[9:-1])) or "No broker reason supplied"

    def _confirmed_rejection(self, token: str) -> str | None:
        request = self._order_requests.get(token)
        if request and request.get("local_rejected"):
            if request.get("order_seen") or request.get("identity_conflict") or request.get("executions"):
                raise OrderSubmissionUncertain("Unsent DAS token conflicts with observed broker activity")
            return request["local_rejected"]
        if not request or not request.get("rejection"):
            return None
        # Never allow later empty snapshots to erase conflicting order evidence.
        if request.get("uncertain") or request.get("order_seen") or request.get("identity_conflict"):
            raise OrderSubmissionUncertain(
                "DAS rejection conflicts with order activity; reconcile the saved token")
        if any(order["client_order_id"] == token for order in self._orders.values()):
            raise OrderSubmissionUncertain("DAS rejection has a matching broker order; reconcile it")
        # A frontend rejection has order ID zero. No trade can safely be
        # attributed to that ID; an unexpected zero-ID execution is ambiguous.
        if any(trade["order_id"] == "0" and trade["symbol"] == request["symbol"]
               for trade in self._trades.values()):
            raise OrderSubmissionUncertain("DAS rejection conflicts with execution evidence")
        return f"DAS rejected {request['symbol']} order on {request['route']}: {request['rejection']}"

    def get_account(self) -> dict[str, Any]:
        with self._lock:
            self._connection_update(checking=True)
            try:
                self._connect()
                self._connection_update(checking=True)
                self._send("GET BP")
                buying_power = self._buying_power(self._wait(lambda p: p[0] == "BP"))
                self._connection_ready()
            except DasError as exc:
                self._disconnect(str(exc))
                raise
            return {"id": self.settings.account, "status": "CONNECTED", "trading_blocked": False,
                    "buying_power": buying_power, "broker": "DAS"}

    def get_asset(self, symbol: str) -> dict[str, Any]:
        with self._lock:
            symbol = _atom(symbol, "symbol")
            self._connect()
            self._send(f"GET SHORTINFO {symbol}")
            p = self._wait(lambda p: p[0] == "$SHORTINFO" and len(p) > 1 and p[1] == symbol)
            if len(p) < 9 or p[2] not in {"Y", "N"} or p[7] not in {"Y", "N"}:
                raise DasError("Invalid DAS short info")
            size = _number(p[3], "short size", integer=True)
            return {"symbol": symbol, "tradable": True, "shortable": p[2] == "Y",
                    "easy_to_borrow": p[2] == "Y" and size > 0, "short_size": size,
                    "short_prohibited": p[7] == "Y", "reg_sho": p[8] == "Y"}

    def get_available_locates(self, symbol: str) -> int:
        with self._lock:
            return self._available_locates(_atom(symbol, "symbol"))

    def validate_shortable(self, symbol: str, shares: int) -> tuple[bool, str]:
        """Recheck borrow at entry time without placing another locate request."""
        with self._lock:
            asset = self.get_asset(symbol)
            if asset["short_prohibited"]:
                return False, "DAS now prohibits shorting this symbol"
            if asset["shortable"] and asset["short_size"] >= shares:
                return True, "DAS shortable size reconfirmed"
            if self._available_locates(symbol) >= shares:
                return True, "DAS located shares reconfirmed"
            return False, "DAS borrow is no longer sufficient; short entry blocked"

    def _available_locates(self, symbol: str) -> int:
        self._connect()
        self._send(f"SLAvailQuery {_atom(self.settings.account, 'account')} {_atom(symbol, 'symbol')}")
        p = self._wait(lambda p: p[0].lower() == "$slavailqueryret" and len(p) >= 4
                       and p[1] == self.settings.account and p[2] == symbol)
        return int(_number(p[3], "available located shares", integer=True))

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            self._snapshot("POSITIONS")
            rows = [x for x in self._positions.values() if x["symbol"] == symbol and x["qty"]]
            if len(rows) > 1:
                raise DasError("Multiple DAS position types for symbol; reconcile before trading")
            return dict(rows[0]) if rows else None

    def position_qty(self, symbol: str) -> int:
        row = self.get_position(symbol)
        return int(row["qty"]) if row else 0

    def _order(self, row: dict[str, Any]) -> dict[str, Any]:
        request = self._order_requests.get(row["client_order_id"], {})
        if request.get("identity_conflict") or request.get("local_rejected"):
            raise OrderSubmissionUncertain("DAS order identity changed from persisted submission")
        fills = [t for t in self._trades.values() if t["order_id"] == row["id"]]
        if any(self._trades.get(trade_id) != execution
               for trade_id, execution in request.get("executions", {}).items()):
            raise OrderSubmissionUncertain("DAS previously observed executions missing or changed in snapshot")
        if any(t["symbol"] != row["symbol"] or t["side"] != row["das_side"] for t in fills):
            raise DasError("DAS executions conflict with order identity")
        filled = sum(t["qty"] for t in fills)
        reported = row["qty"] - row["leaves"] - row["canceled"]
        if reported < 0 or filled > row["qty"] or reported != filled:
            raise DasError("DAS order/execution snapshots do not agree; waiting for reconciliation")
        status = row["das_status"]
        if status not in {"closed", "hold", "sending", "accepted", "canceled", "rejected", "executed", "partial", "triggered"}:
            raise DasError("Unrecognized DAS order status")
        if row["leaves"] == 0 and filled == row["qty"]:
            normalized = "filled"
        elif status in {"canceled", "rejected", "closed"} and row["leaves"]:
            raise DasError("DAS terminal order still reports open shares; reconcile before replacing")
        elif status in {"canceled", "rejected"}:
            normalized = status
        elif status == "closed":
            normalized = "expired"
        else:
            normalized = "partially_filled" if filled else "new"
        day = request.get("submitted_at", datetime.now(EASTERN).isoformat())[:10]
        fill_times = sorted(datetime.fromisoformat(f"{day}T{trade['execution_clock']}")
                            .replace(tzinfo=EASTERN).isoformat() for trade in fills)
        return {**row, "status": normalized, "filled_qty": filled,
                "filled_avg_price": sum(t["qty"] * t["price"] for t in fills) / filled if filled else 0.0,
                "first_fill_time": fill_times[0] if fill_times else None,
                "last_fill_time": fill_times[-1] if fill_times else None}

    def _refresh_orders(self) -> None:
        self._snapshot("ORDERS")
        self._snapshot("TRADES")

    def list_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_orders()
            orders = [self._order(x) for x in self._orders.values() if x["symbol"] == symbol]
            return [x for x in orders if x["status"] not in TERMINAL_STATUSES]

    def get_order(self, order_id: str) -> dict[str, Any]:
        with self._lock:
            self._refresh_orders()
            if order_id not in self._orders:
                raise OrderSubmissionUncertain("DAS order missing from completed snapshot; manual reconciliation required")
            return self._order(self._orders[order_id])

    def get_order_by_client_id(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._refresh_orders()
            matches = [x for x in self._orders.values() if x["client_order_id"] == client_id]
            if not matches:
                rejection = self._confirmed_rejection(client_id)
                if rejection:
                    raise OrderRejected(rejection)
                if client_id not in self._order_requests:
                    return None
            if len(matches) != 1:
                raise OrderSubmissionUncertain("DAS token unresolved or duplicated; no replacement will be sent")
            return self._order(matches[0])

    def submit_limit_order(self, *, symbol: str, qty: int, side: str,
                           limit_price: float, client_order_id: str,
                           route: str | None = None,
                           still_valid: Callable[[], bool] | None = None) -> dict[str, Any]:
        with self._lock:
            symbol = _atom(symbol, "symbol")
            route = _order_route(route if route is not None else (self.order_routes[0] if self.order_routes else ""))
            if route not in self.order_routes:
                raise DasError(f"DAS order route {route} is not a configured primary or backup route")
            token = _atom(client_order_id, "order token")
            if not token.isdigit() or not 0 < int(token) < 2_147_483_648:
                raise DasError("DAS client order token must be a positive 32-bit integer")
            if side not in {"buy", "sell"} or type(qty) is not int or qty <= 0:
                raise DasError("Invalid DAS order side/quantity")
            if not math.isfinite(limit_price) or limit_price <= 0:
                raise DasError("Invalid DAS limit price")
            increment = Decimal("0.01") if limit_price >= 1 else Decimal("0.0001")
            price = Decimal(str(limit_price)).quantize(increment, rounding=ROUND_HALF_UP)
            if price <= 0:
                raise DasError("DAS limit price rounds to zero")
            self._snapshot("ORDERS")
            if token in self._sent:
                raise OrderSubmissionUncertain("DAS token already used; reconcile existing order without resending")
            if any(x["client_order_id"] == token for x in self._orders.values()):
                raise DasError("DAS token collision; this order was not sent")
            self._sent.add(token)
            request = {"symbol": symbol, "qty": qty, "das_side": "SS" if side == "sell" else "B",
                       "route": route, "price": f"{price:f}", "submitted_at": datetime.now(EASTERN).isoformat()}
            self._order_requests[token] = request
            self._save_orders()  # Fsync completes before a possibly paid operation.
            try:
                if still_valid is not None and not still_valid():
                    request["local_rejected"] = "Entry expired or paused before DAS submission; no stock order sent"
                    self._save_orders()
                    raise OrderRejected(request["local_rejected"])
                self._send(f"NEWORDER {token} {'SS' if side == 'sell' else 'B'} {symbol} {route} {qty} {price:f} TIF=DAY+")
                p = self._wait(lambda p: bool(request.get("rejection"))
                               or (p[0].lower() == "%order" and len(p) > 2 and p[2] == token
                                   and p[1] in self._orders))
                if request.get("rejection"):
                    # Read complete snapshots before treating a frontend
                    # Send_Rej as terminal. This also processes any queued,
                    # contradictory acceptance/execution messages.
                    self._refresh_orders()
                    rejection = self._confirmed_rejection(token)
                    if rejection:
                        raise OrderRejected(rejection)
                order_id = p[1]
                self._snapshot("TRADES")
                return self._order(self._orders[order_id])
            except OrderRejected:
                raise
            except (DasError, KeyError) as exc:
                detail = self._broker_notes(str(exc)) if isinstance(exc, DasError) else "order record unavailable"
                raise OrderSubmissionUncertain(
                    f"DAS order may have been sent; awaiting token reconciliation ({detail})") from exc

    def cancel_order(self, order_id: str) -> None:
        with self._lock:
            order_id = _atom(order_id, "order id")
            if not order_id.isdigit():
                raise DasError("DAS cancellation requires a numeric order id")
            # Repeated supervisor requests remain idempotent and avoid cancel flooding.
            if time.monotonic() - self._cancel_at.get(order_id, -math.inf) < 2.0:
                return
            self._connect()
            self._send(f"CANCEL {order_id}")
            self._cancel_at[order_id] = time.monotonic()
