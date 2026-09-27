# 4am short dashboard

The independent v3 dashboard displays live Alpaca SIP data, DAS execution and locate state, and saved 4am short backtest results. It reads strategy settings from `live_4am_short.json`; backtests continue to use `backtest_4am_short.json`.

All clocks, signal times, and fill times are shown in **America/New_York**, including daylight saving time. The rule cards display the actual configured times and percentages.

The **Find the gap** card shows the full discovery interval. The supplied `strategy.late_gap_enabled: true` allows new stocks to qualify strictly above 30% from 04:00 until 09:00, with 09:00 excluded. Original 04:00–04:15 qualifiers retain their early-window setup. Later first qualifiers use their own fixed `late_gap_window_minutes` window (15 minutes in the supplied JSON), then the same ten-minute delay from the high. The **Wait for high** card shows both requirements and the high-time convention. Entry must still fill before 09:00; a late signal does not extend that deadline. Set `late_gap_enabled` to `false` to restore early-window discovery. Restart the service to load rule changes.

The re-entry cards show its effective live **ON/OFF** setting, limit above the original setup high, stop and target percentages, entry deadline, and time exit. Set `strategy.reentry` in `backtest_4am_short.json` for the shared rules. Set `execution.reentry_enabled` to `true` or `false` in `live_4am_short.json` to control live re-entry independently; omitted or JSON `null` inherits the shared switch. Changes take effect after restarting the service. The dashboard controls do not change these settings. The supplied re-entry cutoff remains 08:00, independently of the expanded initial discovery.

## Controls

- **Start monitoring** starts discovery in monitor mode. Monitor mode sends no locate or trading orders.
- **Start DAS paper trading / Start DAS LIVE trading** enables new entries in the selected DAS execution mode. The mode badge remains visible beside connection status. Configuration must point to the intended DAS account before starting.
- **Stop entries** disables new entries and cancels entry orders. Existing positions continue to be managed while the service is running.
- **Cover positions** stops new entries and requests covers for the strategy's remaining shares. It requires typing `COVER 4AM SHORT`; prices are determined by fills, not the dashboard quote.
- **Run backtest** runs the named backtest and updates the saved summary and monthly table.

Opening or refreshing the page only reads state. It never starts the strategy. Closing the browser does not stop the Python service. Keep the service, Alpaca connection, and DAS connection running while managing exposure; shutting down the service cannot continue software-managed exits.

## Reading the desk

The candidate table shows the previous regular close, first qualifying gap and time, setup high and time, short-entry limit and activation time, bid/ask with quote age, locate state, and entry status. The setup high comes from the early window or a late qualifier's own fixed window. Both use the configured final-bar wait and refresh before live entry. Filter by symbol or show only qualified candidates.

The **Short entry** rule card also shows the live locate trigger from `execution.locate_trigger_below_entry_percent`. The default `1.0` waits for a fresh bid at or above 99% of the rounded entry limit; bids at or above the limit qualify too. For a $9.00 limit, the candidate's **Locate** cell reads **Bid ≥ $8.91 / Not requested** until an attempt starts. A candidate below that threshold displays **waiting for price** in the amber status style. The existing window-finalization, high-delay, deadline, and quote-freshness checks still apply. Waiting for price leaves the stock/date unconsumed.

Adjust the setting in `live_4am_short.json`: `0` waits for the bid to reach the entry limit, while JSON `null` restores borrowing as soon as the other entry gates allow it. The 1% default is a starting point: a wider threshold allows more borrow-processing time but can increase unused locate costs; a narrower threshold can miss brief entry opportunities. The bid and other gates are checked again before paid locate actions. A fade at that check can stop the charge, but cannot undo a charge already in flight. Once borrow is confirmed, a later fade does not cancel the resting entry; its original deadline and other cancellation rules still apply. Failed or interrupted locate attempts are not automatically retried. Monitor mode uses the same trigger for simulated entries and sends no locate requests. This live setting does not change the historical backtest.

When comparison data is available, expand **route quotes** in a candidate or trade's **Locate** cell. Each route shows its quote, available shares, minimum fee, quoted total/effective per-share cost, eligibility or selection, and the reason for an unavailable quote. Expansion stays open through automatic refreshes. **Quote only** explicitly means that route cannot be purchased under the current settings. **Quoted-cost purchase · final charge can change** identifies an enabled route without a broker-enforced price cap; **Price cap enforced** identifies the existing supported offer/capped routes. Reported actual per-share and total costs appear after a purchase, with a warning if the actual charge exceeded the quote or configured quoted-cost ceiling.

The candidate panel states the active policy for `das.locate_quote_routes` (`LOCATE7`, `LOCATE1`, `LOCATE8`, `LOCATE12`, `LOCATE14` in the supplied live JSON). `das.allow_uncapped_locate_purchases: false` keeps those routes quote-only; `true` permits a purchase based on the quoted cost, with possible final-price changes. The supplied live JSON enables those purchases. Each quote must still cover the full quantity and fit the per-share ceiling including minimum fees, and only the winning route is purchased. The additional five inquiries can add about 15 seconds because requests remain spaced three seconds apart. Changes require restarting the Python service.

The positions table labels each row **Initial** or **Re-entry** and retains both trades for the same stock. It uses confirmed fills for entry average price, entry time, remaining shares, stop and target prices, exit average and time, exit reason, and profit-target outcome. Realized live P/L is shown separately from backtest net P/L; live fees may not be available from the fill stream.

Enabled re-entry permits one second attempt only after the first position is fully stopped out. Its default limit is 5% above the original setup high, stop is 20% above its actual fill, target is 40% below its fill, and both the entry cutoff and time exit are 09:20 ET; the cards show the actual configured values. A market already above that limit can fill immediately. DAS must confirm reusable borrow; the second attempt never purchases new locates. The initial paid-locate proximity gate does not delay this borrow check. Stop entries also disables re-entries.

The monthly backtest table includes gap triggers, traded and untraded setups, completed trades, wins and losses, win/loss percentages, net P/L, average per trade, profit factor, drawdown, winning and losing days, and total stop-loss exits. A TOTAL row uses the overall saved summary. Gap counts include each stock/date once; completed trades count initial and re-entry trades separately. Both stopping out counts as two stop-loss exits. Entered setups can include unresolved exits. The table footer shows saved re-entry attempts, completed trades, and skipped attempts when available.

The event log provides recent operational messages. A disconnected dashboard freezes displayed values and disables controls until it reconnects. The SIP and DAS connection states are distinct from the dashboard's connection to the Python service.

### DAS connection health

**Connected · verified** means the service completed a DAS account check, not just opened a TCP socket. The card shows when that check last succeeded. Once the supervisor starts or restores saved exposure, checks run every `das.health_check_seconds` (default **10 seconds**), including after **Stop entries**. Opening the UI alone does not connect to DAS. A failed check or detected disconnect removes the green state; the service automatically reconnects and authenticates, with retries every `das.reconnect_seconds` (default **5 seconds**). The card shows **Reconnecting…** during an attempt, or **Disconnected** with the next retry and latest failure.

When new entries are enabled during an outage, the desk reads **Waiting for DAS**. Recovery preserves the Start/Stop choice and allows eligible, unattempted candidates to continue. An already skipped symbol, such as a failed locate attempt, is not retried. A candidate's failure note describes that earlier attempt; the DAS connection card describes current service health.

Existing broker orders may still fill while disconnected. Software-managed stops, targets, and covers cannot be submitted until DAS is available, so check any open exposure directly in DAS. Incorrect credentials, a closed DAS application, or disabled CMD permission must be corrected for reconnection to succeed. If the dashboard loses the Python service, both connection indicators become **Unknown** rather than retaining their previous green state.

Restart the Python service to load the connection-health fix or changed connection settings; a browser refresh alone does not update the running supervisor. After a service restart, new entries are paused until Start is pressed again. Monitor and demo modes do not connect to DAS.

## Local control security

The server is intended for loopback access. Open the address printed by the launcher (the supplied configuration uses `http://127.0.0.1:8002`; the code default is port 8003). It accepts only loopback Host names, checks browser Origin and Fetch Metadata, and requires a per-process random control token for every POST. It provides no cross-origin permissions. Cover confirmation is also enforced by the API. This is a local control surface, not a multi-user authenticated service; do not expose it through a public proxy.

The dashboard does not display API keys or passwords. UI controls return sanitized failure messages; detailed public events come from the engine. The HTML, CSS, and JavaScript are served locally without external CDNs.

## Offline verification

`tests/test_4am_short_live_app.py` uses a fake engine to verify all controls without connecting to Alpaca or DAS. It checks startup behavior, exact cover confirmation, tokens, cross-origin requests, DNS rebinding protection, redacted failures, and server lifecycle.

The runner's demo option, when enabled, uses sample state and displays a **DEMO · SAMPLE DATA** badge. Demo controls do not contact a broker.
