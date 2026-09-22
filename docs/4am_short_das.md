# 4am short: DAS execution

`four_am_short/live/das.py` and `das_locates.py` are independent v3 modules.
They have no v2 imports, environment dependency, or shared trading journal.
Alpaca SIP supplies all strategy prices. DAS CMD supplies buying power,
positions, execution reports, shortability, locates, and stock orders.

## Connection and routes

Keep DAS Trader Pro running and logged in with CMD API permission enabled.
Set the Windows machine/VM's reachable address and CMD port in the v3 private
`.env`, together with `DAS_USERNAME`, `DAS_PASSWORD`, and `DAS_ACCOUNT`.
`127.0.0.1` on the Mac reaches the Mac, unless a port forward is configured.
CMD is plain TCP; use the trusted local/VM network rather than exposing this port.

`mode: "das_paper"` only labels the account as a simulator. It does not transform
real broker credentials into a simulator account. Use a DAS simulator account
for `das_paper` mode. Monitor/demo modes never send broker commands.

In DAS execution modes, once started or restoring saved exposure, the supervisor
periodically makes a read-only account request to verify authenticated
order-server access. A socket alone is not proof
of a working DAS session. Set these optional fields inside `das` in
`live_4am_short.json` to adjust the timing:

```json
"health_check_seconds": 10,
"reconnect_seconds": 5
```

A failed check or detected disconnect clears the verified state and triggers
automatic reconnection and login. Checks and retries continue while the supervisor
is active, including after **Stop entries**. Merely opening the UI does not connect
to DAS. Retries stop when the process shuts down;
invalid credentials, unavailable DAS, or missing CMD permission must be fixed
before they can succeed. Health checks do not buy locates or submit stock orders.
New entries wait for verified connectivity without changing the user's Start/Stop
choice. Recovery does not replay previously skipped stock/day attempts or blindly
resubmit uncertain orders: persisted orders and fills still require reconciliation.
Existing broker orders may fill during an outage, while the supervisor cannot
send cancels or covers. Check open exposure directly in DAS during an outage.

Restart the Python service to load this connection-health change or changed
settings. Browser refresh alone does not reload the Python supervisor; after
a restart, new entries are paused until Start is pressed again.

The default stock routes are primary **ARCAE** and optional backup **CBATS**.
These are the CMD base routes for the Montage names ARCAEL/CBATSL. The CMD
manual maps Montage LIMIT/MARKET/STOP to SMAT. Use broker-enabled route names.
Every stock order here is a numeric limit with `TIF=DAY+`: `SS` for short entry
and `B` for cover. Broker acceptance/fills, not the submitted limit, determine
position size and average entry price.
The adapter returns the first and last broker execution times. DAS provides an
execution clock without a date; owned intraday orders use their journaled Eastern
submission date, including the Eastern offset applicable to that date.

The adapter never changes routes or resubmits automatically. The supervisor may
use the configured backup after an exact, complete, unfilled rejection. Timeout,
missing response, partial rejection, conflicting execution, or changed identity
must be reconciled first. A canceled request is not a confirmed cancellation:
complete order and trade snapshots must show the terminal state before replacing
an order. Canceled partial fills remain part of the position.
Entry eligibility is rechecked after submission preflight and immediately before
the stock command. Existing DAY+ entries still require a confirmed cancellation;
network/broker latency can cause fills while cancellation is pending.

## Locates

The supported locate workflow is deliberately explicit:

1. Reconcile any earlier paid request before checking available borrow.
2. Reuse confirmed shortable size or already available located shares.
3. Request **LOCATE4/LOCATE6** offers, a **LOCATE10** price inquiry, and any
   additional configured quote routes.
4. Compare full-size responses by total cost including the route minimum charge.
5. Reject unselected offers. Accept only the winning offer, or submit one purchase
   on the winning inquiry route under that route's configured price policy.
6. Confirm the requested quantity, cost, and current available borrow before entry.

The account-specific v2 reference records broker confirmation that LOCATE4/6 are
explicit-acceptance routes and LOCATE10 supports a purchase price limit. LOCATE10
requires at least **100 shares**. Smaller requests skip it without increasing the
share count. Changing the account/broker requires checking those route behaviors.

For offers and price-capped routes, the configured maximum locate price is an
effective per-share ceiling:
`max(quoted_price * requested_shares, route_minimum_fee) / requested_shares`.
An absent minimum-fee response is not treated as zero. Slow unaccepted offers
never become a second purchase; the running supervisor rejects its late offers.
Entry-deadline/pause eligibility is checked immediately before the paid command.
An ambiguous or malformed paid result never causes an alternative purchase.

### Additional quote routes and optional purchases

`das.locate_quote_routes` configures type-0 `SLPRICEINQUIRE` routes. The supplied
live JSON lists `LOCATE7`, `LOCATE1`, `LOCATE8`, `LOCATE12`, and `LOCATE14`.
These routes are distinct from the confirmed price-capped `LOCATE10` route.
When `das.allow_uncapped_locate_purchases` is `false` (the code default), their
comparison rows are marked **quote only** and cannot win a purchase.

The supplied live JSON explicitly sets `allow_uncapped_locate_purchases: true`.
This permits a winning purchase on these routes based on a complete quote that
covers the requested shares and fits `execution.max_locate_price`, including
the route minimum charge. The current configured quote ceiling is **$0.06/share**.
The purchase command on these routes has no enforceable price limit: the final
charge may differ from the quote or exceed the configured quoted-cost ceiling.
Only one route is purchased; an uncertain result never triggers a second purchase.
The original `LOCATE4`/`LOCATE6` acceptance and `LOCATE10` enforced-cap policies
remain in place.

The comparison records the quoted price/cost, whether a cap can be enforced,
and the reported actual price and total cost after purchase. The dashboard shows
these in **route quotes** and flags an actual charge above the quote or ceiling.
A quote preview never buys shares, regardless of this setting. Queries retain
three-second spacing, so five added routes can add about **15 seconds** to the
comparison. Current entry gates are still checked before the paid command.
Restart the Python service after changing route or purchase-policy settings.

## Persistence and ownership

The journal binds to a hash of DAS host, port and account, and contains separate
stock-order and locate records. Login credentials are never journaled. Intents
are atomically written and fsynced before stock submission or locate acceptance.
Owned order tokens and observed executions survive restart; contradictory or
missing evidence remains unresolved rather than becoming a zero-fill result.

An account lock spans v3 clients even if they select different journal paths.
A second lock protects the journal itself, including attempts with other accounts.
Do not delete journals or lock files to bypass reconciliation. The v2 process and
manual DAS workflows do not share v3's lock: use one execution controller for the
account and avoid concurrent locate purchases/manual trading in managed symbols.

The Python supervisor manages stop, target and time exits. It must keep running,
with current SIP data and a working DAS connection. A marketable limit can remain
unfilled, including at 9:30; the UI must display the unresolved position until
broker execution is confirmed. Exiting the process cannot guarantee a flat account.

## Verification and protocol provenance

The adapter was checked against the local `CMD API Manual.pdf` (revision history
through July 31, 2025), specifically login/snapshots (pages 4–7), NEWORDER and base
routes (pages 7–8), and short locates (pages 19–20). The local manual is not copied
into this project. Public DAS references confirm the [CMD running-app requirement](https://mirror.dastrader.com/docs/api-overview/)
and [token/base-route protocol updates](https://dastrader.com/notice.html).

Run the isolated protocol regression tests from this directory:

```bash
python3 -m unittest discover -s tests -p 'test_4am_short_live_das*.py' -v
```

These tests use an in-memory fragmented socket. They exercise completed login
snapshots, signed positions, execution-derived partial fills, cancellation,
rejections, uncertain submission/restart, account locks, identity-bound journals,
locate comparisons, price/minimum-fee caps, minimum quantities, and deadline checks.
No live account, paid locate, or broker order was used to validate this implementation.
