# 4am short

**4am short** (`strategy_id: 4am_short`) includes a Python backtest, a local dashboard, and a live supervisor for the 04:00–04:15 Eastern gap-and-pullback short setup. Backtests use a dated stock list and Massive historical data. Live discovery and quotes use Alpaca's live SIP websocket; locates, orders, and fills use the DAS Trader Pro CMD API. This directory is independent of `stock_short_premarket_v2` and imports no v2 code.

Live mode defaults to **monitor**, which simulates entries/exits from live quotes without sending locates or orders. The implementation has been checked with synthetic data and mock broker responses; an actual Alpaca SIP entitlement and the broker's DAS CMD session have not been exercised by these tests.

## Strategy files

The strategy has its own launcher, configuration, Python package, and report directory so future strategies can use distinct names:

```text
backtest_4am_short.py       # Backtest launcher
backtest_4am_short.json     # Shared rules, shares, and backtest configuration
run_4am_short_ui.py         # Local dashboard launcher
live_4am_short.py           # Headless live supervisor (--start enables new entries)
live_4am_short.json         # Live connections, execution mode, and safeguards
four_am_short/             # Python implementation
four_am_short/live/        # Independent live feed, DAS, engine, and dashboard
outcome/4am_short/          # Reports from new runs
state/4am_short/            # Durable live attempts, order tokens, fills, and events
.cache/massive/            # Reusable historical-data cache
```

`four_am_short` spells out the number because Python package identifiers cannot begin with a digit. The legacy `backtest.py` command still launches this strategy, and `backtest.json` is a symlink to `backtest_4am_short.json`, keeping one configuration source. Existing reports retain their original names and locations.

## Run the backtest

Requires Python 3.11 or newer. The backtest itself needs no third-party Python packages.

```sh
cd /Users/stevevantil/stocks/stock_short_premarket_v3
test -f .env || cp .env.example .env
```

Set `MASSIVE_API_KEY` in `.env`, then edit `backtest_4am_short.json` as needed. An existing environment variable takes precedence over `.env`. Credentials stay outside the JSON configuration and reports.

```sh
# Check configuration and input without requesting market data.
python3 backtest_4am_short.py --config backtest_4am_short.json --validate-only

# Start with a small sample.
python3 backtest_4am_short.py --config backtest_4am_short.json --max-candidates 5

# Run the configured stock list.
python3 backtest_4am_short.py --config backtest_4am_short.json
```

The default input is `../stock_daily_scanner/outcome/2026-NEW_all.csv`. All relative paths in the configuration resolve from the JSON file's directory, regardless of the shell's working directory.

Date and symbol filters can be supplied in JSON or overridden on the command line:

```sh
python3 backtest_4am_short.py --config backtest_4am_short.json \
  --from-date 2026-01-02 --to-date 2026-01-09 --symbols CHOW DXF
```

Date bounds are inclusive. Candidates are deduplicated and sorted by date, then symbol; `--max-candidates` limits this filtered, sorted list. Validation fails if no candidates remain.

Use `--offline` to rerun entirely from the local cache; missing cache entries become candidate errors. Use `--refresh-cache` to fetch fresh data. These two options cannot be combined. Historical access depends on the Massive account's entitlements; request errors are reported rather than replaced with invented prices.

Candidates whose configured time-exit minute has not yet finished are marked `incomplete` with reason `session_not_finished`, without fetching their prices. The run can still process earlier dates.

## Run the dashboard and live supervisor

Install the live dependencies into the v3 virtual environment:

```sh
cd /Users/stevevantil/stocks/stock_short_premarket_v3
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# Preview the dashboard with fictional data and no external connections.
.venv/bin/python run_4am_short_ui.py --demo

# Validate the live JSON without connecting to either API.
.venv/bin/python run_4am_short_ui.py --validate-only

# Open the real dashboard; new entries remain paused until its Start control.
.venv/bin/python run_4am_short_ui.py
```

Open the local address printed by the launcher (the supplied live JSON uses **http://127.0.0.1:8002**; the code default is port 8003). The dashboard shows the mode, SIP and DAS connection health, configured rules, candidate timing/prices, locates, confirmed fills, stops/targets, exit reasons, and saved monthly backtest results. [Dashboard guide](docs/4am_short_ui.md) describes the controls and tables. `--port` changes the local UI port; `--config` selects a different live JSON. Loopback binding is required.

Set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in v3's `.env`. The feed is `wss://stream.data.alpaca.markets/v2/sip`, with no IEX or delayed-feed fallback. Alpaca supplies market data only; the implementation never sends orders through Alpaca. Existing environment variables override `.env` values.

`live_4am_short.json` selects `backtest_4am_short.json` through `strategy_config`. Both use its **shares, percentages, early window, high-time convention, entry deadline, and time exit**, including the optional re-entry rules. `execution.reentry_enabled` independently switches live re-entry on or off; see [Optional re-entry](#optional-re-entry-after-a-stop-loss). Changes take effect when the live process restarts. The strategy table below lists code defaults; the current JSON may override them, including the entry deadline. The dashboard's rule cards and launcher output show the active values.

| `mode` | Behavior |
| --- | --- |
| `monitor` | Live SIP discovery with simulated quote-based fills; no DAS connection or paid locates |
| `das_paper` | Sends actual CMD orders and locate requests to the configured DAS session, intended for a DAS paper account |
| `das_live` | Sends CMD orders and locate requests to the configured DAS live account |

For either DAS execution mode, enable the CMD API in DAS Trader Pro and configure `DAS_HOST`, `DAS_PORT`, `DAS_USERNAME`, `DAS_PASSWORD`, and `DAS_ACCOUNT` in v3's `.env`. DAS normally runs on Windows; `DAS_HOST` must reach that machine from this process. Selecting `das_paper` does **not** turn a live DAS account into a simulator: use actual paper-account credentials and verify the account in DAS. Route/locate permissions come from that broker session.

The dashboard never enables fresh entries on page load. Its Start button identifies monitor, DAS paper, or DAS LIVE mode. **Stop entries** cancels pending entries while continuing to supervise existing positions. **Cover positions** requires typing `COVER 4AM SHORT` and requests covers for this strategy's tracked exposure. To run without the UI:

```sh
# Explicitly start the configured mode (monitor by default).
.venv/bin/python live_4am_short.py --start

# Load persisted state and resume exit supervision only; new entries stay paused.
.venv/bin/python live_4am_short.py
```

Use one supervisor per state/account identity. The state lock prevents duplicate v3 instances from managing the same strategy. Entries/attempts and broker tokens are persisted before order submission; restart restores unresolved orders/positions with new entries paused. The initial stock/date attempt is consumed once attempted, including a failed locate. When re-entry is enabled, one separate attempt is permitted only after that initial position has fully closed by stop-loss. Failed initial attempts, other exits, and a second stop do not create further attempts. Existing unowned DAS positions or open orders in a candidate prevent a fresh trade.

Once started or restoring saved exposure, the supervisor verifies DAS with a read-only account check every `das.health_check_seconds` (default **10**) and automatically reconnects and authenticates after failures, retrying every `das.reconnect_seconds` (default **5**). These checks continue after **Stop entries**; opening the UI alone does not connect to DAS. The dashboard shows the latest verified connection time and retry state. A disconnect blocks new entries without changing your Start/Stop choice; eligible, unattempted stocks may proceed once DAS recovers. Previously skipped attempts are not replayed. Existing broker orders can still fill during an outage, while the supervisor cannot manage positions or send covers; check open exposure in DAS. Wrong credentials or disabled CMD access require correction before a retry can succeed. Restart the Python service to load this change or connection-setting changes; refreshing the browser alone is insufficient.

### Live timing and execution

The live universe includes Alpaca's active, tradable US equities, with optional `symbols` restriction. The supervisor obtains prior regular-session closes, collects early one-minute SIP bars and updated bars, and backfills the early window when starting or reconnecting. It does not depend on the backtest stock-list file for discovery. Prior closes use split-adjusted daily SIP bars for the exact preceding exchange session; Alpaca's [bar eligibility rules](https://docs.alpaca.markets/us/docs/market-data-faq) exclude extended-hours trade conditions from daily open/close prices.

The early-window high follows the same minute-bar convention as the backtest. By default, the process waits an additional **35 seconds after 04:15** (`execution.final_bar_wait_seconds`) and refreshes the complete early window before allowing entries. This accommodates Alpaca `updatedBars` corrections. The ten-minute high delay must also have elapsed. This finalization can delay an otherwise eligible 04:15 entry until approximately 04:15:35 plus the backfill duration. Highs freeze after finalization.

Entry is a resting DAS sell-short limit at the configured discount from the high, rounded upward to the valid price increment. If executable prices are already at/above the limit, DAS can fill immediately; otherwise it waits for a bounce. The actual fill depends on quotes, routing, liquidity, borrow, and the broker. Orders use DAS `DAY+`; the application requests cancellation at the entry deadline or when entries are paused/data readiness is lost. A cancel can race with a fill, so the application cannot promise exact deadline precision at the broker. Broker-reported fills at/after the deadline are flagged and covered. Partial fills are managed using the confirmed filled quantity and average price.

Borrow checks and paid locates now wait for price proximity as well as the existing timing, finalized-window, entry-deadline, and fresh-quote gates. Set `execution.locate_trigger_below_entry_percent` in `live_4am_short.json`: the default `1.0` starts a locate attempt when the fresh SIP **bid is at least 99% of the rounded entry limit**. For a $9.00 entry limit, the trigger is $8.91; bids at or above $9.00 also qualify. `0` requires the bid to reach the entry limit. JSON `null` disables the proximity gate and restores the legacy borrow attempt as soon as the other gates permit it. Waiting below the trigger does not consume the stock/date attempt.

The 1% value is an adjustable starting point. A wider percentage starts borrowing earlier and gives DAS more time, but can spend locate fees on stocks that never reach entry. A narrower percentage delays that cost, but inquiry, borrow confirmation, and order-routing latency can miss a brief entry opportunity. Quotes are requested when the attempt starts; an earlier inquiry would not lock in later pricing because [DAS locate quotes can change](https://mirror.dastrader.com/docs/why-doesnt-my-locate-price-match-the-inquiry-price/). The supervisor rechecks the current bid and other entry gates immediately before paid locate actions, so a fade can stop a charge before it is sent. A price change during an in-flight action can still leave paid borrow unused. After borrow is confirmed, proximity no longer blocks submission or cancels the resting entry: the order continues under its original entry deadline and other existing cancellation rules. An unsuccessful or interrupted locate attempt still consumes the stock/date and is not automatically retried.

Monitor mode mirrors the same price trigger before creating its simulated resting entry, without contacting DAS. The dashboard shows the configured locate timing on the Short entry rule card and each candidate's trigger in its Locate cell before a request. Historical backtest entry behavior and fees are unchanged by this live setting.

Stops, targets, and the scheduled exit are **software-managed**, not standing broker-side stop/OCO orders. Fresh SIP **ask** prices trigger covers at/above the stop or at/below the target. A quote-triggered exit is remembered until the supervisor processes it; the default one-second polling interval plus reconciliation/network latency can delay order submission. The configured time exit requests a cover when due, but also needs an executable fresh quote and functioning DAS connection. Covers use a marketable buy limit with the configured cushion, cancel/reconcile remaining entry shares first, and replace/reconcile working cover orders as needed. A stop, target, or 09:30 trigger does not guarantee its exact execution price or time. Live realized P/L is gross fill P/L; it does not automatically include every broker commission or locate charge.

Keep the Python supervisor, Alpaca connection, and DAS session running while exposure is open. Graceful process shutdown pauses entries, requests entry cancels and covers, and continues management for up to `execution.shutdown_grace_seconds` (default 30). If exposure remains unresolved, it is persisted and a critical console message requests manual verification in DAS. A crash, forced termination, stale quotes, or disconnected broker can prevent covers; restarting resumes supervision of persisted exposure. Closing the browser alone does not stop the supervisor.

| Live execution parameter | Default | Meaning |
| --- | --- | --- |
| `max_positions` | `0` | No additional position-count cap; positive values limit concurrent attempts/positions |
| `max_locate_price` | `0.06` | Maximum locate cost per share in dollars |
| `locate_trigger_below_entry_percent` | `1.0` | Begin borrowing when the fresh bid reaches this percent below the entry limit or higher; `0` requires the limit, `null` disables the price gate |
| `reentry_enabled` | `null` | `true` enables live re-entry, `false` disables it; omitted or `null` inherits `strategy.reentry.enabled`. The supplied live JSON sets `false` |
| `cover_cushion_percent` | `1` | Buy-limit cushion above the current ask for covers |
| `cover_replace_seconds` | `3` | Minimum interval before repricing/replacing a cover |
| `poll_seconds` | `1` | Supervisor reconciliation interval |
| `final_bar_wait_seconds` | `35` | Post-window wait before final early-window refresh |
| `shutdown_grace_seconds` | `30` | Graceful shutdown cover/reconciliation period |

`alpaca.quote_max_age_seconds` defaults to 5. Stale, crossed, empty, or future-dated quotes cannot authorize entries. The data adapter reconnects and backfills; entry eligibility requires current data readiness.

DAS execution defaults to entry route `ARCAE`, with `CBATS` as the explicit rejection fallback. Locate offers default to `LOCATE4`/`LOCATE6`; price-capped direct locates default to `LOCATE10`. Available borrow is checked first; quoted offers are compared by total cost including minimum fees. A paid locate can remain unused if conditions change or the entry deadline passes. Accepted command syntax and route availability depend on the installed DAS/broker setup; run the paper session through an actual full trade before selecting `das_live`.

`das.locate_quote_routes` adds inquiry routes, currently `LOCATE7`, `LOCATE1`, `LOCATE8`, `LOCATE12`, and `LOCATE14`. Their quotes appear alongside the existing routes. `das.allow_uncapped_locate_purchases` defaults to `false`, which makes these additional routes quote-only. The supplied live JSON explicitly enables it: a complete quote within `execution.max_locate_price` (currently **$0.06/share**, including minimum fees) may win the comparison and receive the one locate purchase. These routes cannot enforce the quoted price on the purchase command, so the final charge can differ from the quote or exceed that ceiling. The dashboard labels this policy and shows reported actual costs and any overrun in the expandable **route quotes** details. `LOCATE4`/`LOCATE6` and `LOCATE10` retain their existing acceptance/price-cap rules. Inquiries retain three-second spacing; five extra routes can add roughly **15 seconds** before a locate decision. Restart the Python service after changing these settings.

## Stock list

One Eastern trading date per line, followed by its symbols. The scanner's whitespace-delimited `.csv` format is supported:

```text
2026-01-02 CHOW DXF UAVS IRWD
2026-01-05 MOBX MKDW SOWG VRME
```

Commas can also separate fields. Blank lines and `#` comments are ignored. Omit a header row; each nonempty data line must have a date and at least one ticker. Tickers are normalized to uppercase, and repeated date/symbol pairs are merged.

The list supplies candidates, not confirmed setups. Each candidate must still pass the historical gap and entry rules. Each stock/date permits one initial trade and, when enabled, at most one re-entry after a stop-loss.

## Strategy

All timestamps use `America/New_York`, including daylight saving time. Percent settings use human units: `30` means 30%, and `12.5` means 12.5%.

1. Use the previous market session's regular-session close as the reference. A candidate qualifies if a one-minute bar's high is **strictly more than 30%** above that reference during `04:00 <= bar time < 04:15`.
2. Record the highest high across that complete early window, then freeze it. By default, the last occurrence of a repeated high sets the waiting clock.
3. Activate the short limit at the later of 04:15 and ten minutes after the early high. Minute bars cannot reveal the high's exact second, so the default clock starts at the **end of its minute**. For example, a high in the 04:14 bar permits entry from 04:25. Setting `high_time_reference` to `bar_start` uses 04:24 instead.
4. Set the sell limit to 90% of the early high. If the active bar opens at or above the limit, fill at that open, subject to configured slippage. Otherwise, wait for a bounce up to the limit. Entry slippage never reduces a sell-limit fill below its limit.
5. Permit fills only before 06:00. An unfilled order is canceled at that deadline.
6. Short 1,000 shares by default. Calculate the stop at 130% and target at 87.5% of the **actual simulated entry price**, including any entry slippage.
7. Cover on a stop, target, or the open of the bar labeled 09:30. An open above the stop fills at that adverse open; a favorable opening gap through the target fills at the target. Configured exit slippage increases the cover price.

The time exit takes precedence over later high/low prices within its minute. If an open position has no bar at the exact configured cutoff, it is marked `incomplete`; the simulator does not substitute a stale close or a later bar. A minute's open means its first eligible trade, not a guaranteed execution exactly on the minute boundary. Reported fill timestamps identify the bar, not the precise execution second.

### Optional re-entry after a stop-loss

Re-entry is off by default. To enable it for backtests, set `strategy.reentry.enabled` to `true` in `backtest_4am_short.json`. The complete section inside `strategy` is:

```json
"reentry": {
  "enabled": false,
  "entry_above_high_percent": 5,
  "stop_loss_percent": 20,
  "profit_target_percent": 40,
  "entry_deadline": "09:20",
  "time_exit": "09:20"
}
```

For live trading, set `execution.reentry_enabled` in `live_4am_short.json` to `true` or `false`. This overrides the backtest switch without changing the shared re-entry percentages or times. Omit the live setting, or set it to JSON `null`, to inherit the backtest switch. The supplied files leave both switches off. Restart the live process after changing either file; the dashboard displays its effective setting.

After the initial trade is fully covered by its stop-loss, submit one new sell-short limit at **105% of the original 04:00–04:15 high**. The high stays frozen; later highs do not replace it. There is no additional high-based waiting period. Use the existing configured share size, a stop **20% above the actual re-entry fill**, and a target **40% below that fill**. The order may fill strictly before **09:20 Eastern**, independently of the initial entry deadline. Cancel an unfilled re-entry at that cutoff; cover an open re-entry at its configured time exit, also 09:20 by default. All these percentages and both times are configurable. A profit target, time exit, manual cover, incomplete initial exit, or failed initial locate does not qualify, and there is no third trade.

This is a sell-short **limit**. When the market is already above the re-entry limit, it can fill immediately after the stop-out; it does not require an additional rise above the stop-out price. For example, with a $10 early high, a $9 initial fill stops at $11.70, while the re-entry limit is $10.50. A bid still near $11.70 is already eligible to fill that limit.

Live re-entry checks that enough borrow is still available in DAS and reuses it. It never buys additional locates for the second trade; insufficient available borrow skips the re-entry. Covering the first short does not by itself guarantee that the broker permits reuse. New entries must remain enabled, feed/quote and broker checks still apply, and the first position and its orders must be fully reconciled before re-entry. The paid-locate proximity gate applies to the initial trade; re-entry does not buy borrow and may place its resting limit after the stop-out.

The backtest begins re-entry eligibility at the **next minute after the stop-loss bar**, because minute OHLC cannot establish a second order's timing within that same bar. A stop in the 05:00 bar permits re-entry from 05:01, if a bar is available before its deadline. The re-entry uses the same fill-improvement, slippage, and intrabar assumptions as the first trade. Each trade incurs its own configured commissions; re-entry has **no second locate fee**. Both trades retain separate entry/exit records and contribute to performance.

### Compare re-entry parameters

`sweep_4am_short_reentry.py` evaluates the finite grid in
`sweep_4am_short_reentry.json`, keeping all first-trade rules and share size fixed.
The supplied grid has 5,850 valid combinations: entry offsets 0–30% in 2.5-point
steps, five stops, five targets, and 18 valid entry-deadline/time-exit pairs.

```sh
.venv/bin/python sweep_4am_short_reentry.py --validate-only
.venv/bin/python sweep_4am_short_reentry.py --offline
```

`--offline` uses the existing Massive cache without credentials or requests.
Omit it to allow the ordinary cached Massive client to fetch missing historical
data. The runner prepares completed first-trade stop-outs once, cross-checks
every current-setting re-entry against the original simulator, and then compares
settings using the same fill model. It never changes the active trading JSON or
connects to Alpaca/DAS.

Results go into a new `outcome/reentry_sweep/` directory: all combinations,
training and full-period rankings, entry-offset sensitivity with other current
parameters held fixed, selected trades/monthly reports, configuration snapshots,
data exclusions, and a Markdown report. All ranked P&L is **re-entry only**.
The JSON's `training_end_date` fixes the chronological split; settings are ranked
by training net P&L with `minimum_training_trades`, without consulting the later
period. The separate full-period comparison uses `minimum_full_sample_trades`.
Unresolved outcomes and missing initial data are reported explicitly. The later
period is a historical diagnostic, not untouched validation if it has already
been inspected or used to choose settings.

`analyze_4am_short_excursions.py` separately measures observed prices relative to
the original early high, using a completed report whose input/config matches the
active backtest. Pass `--source-report` and `--output-dir`; see `--help` for its
options. It verifies each original stop from cache. Next-minute opening prices
and future maximums are reported separately: a later maximum is hindsight, not
a price that the strategy can know when it places its re-entry order.

### Order within a minute

OHLC data does not show whether the high or low came first. The default `conservative` policy evaluates both open → high → low → close and open → low → high → close. It takes a stop if either path stops, and takes a target only if both paths reach it **after entry**. If only one path hits the target, the position remains open along the other path. This avoids crediting a low that occurred before a bounce entry.

This policy makes an adverse local choice within each ambiguous minute; it does not guarantee the lowest possible final P&L across all future paths. Set `intrabar_policy` to `ohlc` or `olhc` to compare the two explicit path assumptions. `ambiguous_bars` counts minutes whose simulated outcomes differ between those paths.

## Configuration

`backtest_4am_short.json` contains the editable defaults. Unknown parameters and invalid values are rejected. Strategy identity is validated so a configuration for a different strategy cannot silently run as 4am short.

| Root parameter | Default / meaning |
| --- | --- |
| `strategy_id` | `"4am_short"`, identity used in filenames and records |
| `strategy_name` | `"4am short"`, display name |
| `input_file` | Required path to the dated candidate list |
| `shares` | `1000`, positive whole shares |
| `output_dir` | `outcome/4am_short`, parent directory for per-run reports |
| `from_date`, `to_date` | Optional inclusive `YYYY-MM-DD` bounds |
| `symbols` | Optional ticker array; empty means all input symbols |

The following parameters belong inside `strategy`:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `gap_percent` | `30` | Strictly exceed this gain above previous close |
| `early_start` | `"04:00"` | Inclusive early-window start |
| `early_end` | `"04:15"` | Exclusive early-window end |
| `wait_after_high_minutes` | `10` | Minimum delay after the early high |
| `entry_below_high_percent` | `10` | Sell limit discount from early high |
| `entry_deadline` | `"06:00"` | Exclusive entry deadline |
| `stop_loss_percent` | `30` | Stop above actual entry |
| `profit_target_percent` | `12.5` | Target below actual entry |
| `time_exit` | `"09:30"` | Required time-exit bar |
| `high_time_reference` | `"bar_end"` | `bar_end` or `bar_start` |
| `repeated_high_policy` | `"last"` | `last` or `first` occurrence of equal high |
| `intrabar_policy` | `"conservative"` | `conservative`, `ohlc`, or `olhc` |
| `entry_slippage_bps` | `0` | Adverse sell-fill adjustment; capped by limit |
| `exit_slippage_bps` | `0` | Adverse buy-fill adjustment |
| `commission_per_share_per_side` | `0` | Dollars per share charged on each side |
| `locate_fee_per_share` | `0` | Dollars per share, charged on the completed initial trade; re-entry reuses borrow at no additional locate cost |
| `reentry` | See above | Optional second-trade switch, entry percentage, stop, target, deadline, and time exit |

One basis point is 0.01%; `100` bps is 1%. Times use `HH:MM`, are on one Eastern date, and must satisfy `early_start < early_end < entry_deadline <= time_exit` for the initial trade. Re-entry has its own `entry_deadline <= time_exit` on the same Eastern date.

The following parameters belong inside `data`:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `api_key_env` | `"MASSIVE_API_KEY"` | Name of credential environment variable |
| `env_file` | `".env"` | Optional credential file; `null` disables file lookup |
| `cache_dir` | `".cache/massive"` | Local validated response cache |
| `timeout_seconds` | `30` | Timeout per HTTP request |
| `max_retries` | `3` | Retries after the initial attempt |
| `request_delay_seconds` | `0.25` | Minimum spacing between request starts |
| `previous_close_lookback_days` | `14` | Calendar days searched for the previous session |
| `calendar_symbol` | `"SPY"` | Symbol whose daily bars identify market sessions |

## Reports

Every run creates a unique directory named `4am_short_<timestamp>_<suffix>` under `output_dir`, which defaults to `outcome/4am_short`. New report filenames also identify the strategy:

| File | Contents |
| --- | --- |
| `4am_short_candidates.csv` | Every processed candidate, including trades, skips, errors, and incomplete positions |
| `4am_short_trades.csv` | Completed trades with prices, timestamps, exit reason, costs, and P&L |
| `4am_short_trade_details.txt` | Readable per-candidate log and monthly summary matching the terminal output |
| `4am_short_gap_summary.txt` | Monthly and total counts of confirmed gaps, entered setups, and untraded setups |
| `4am_short_daily.csv` | Performance grouped by trading date |
| `4am_short_monthly.csv` | Performance grouped by month |
| `4am_short_summary.json` | Run totals, performance, and status counts |
| `4am_short_config.resolved.json` | Effective configuration with resolved paths and date/symbol overrides |
| `4am_short_input.snapshot.txt` | Original input bytes used by this run; their SHA256 is recorded in `4am_short_summary.json` |

CSV records include `strategy_id` and `strategy_name`; the summary and resolved configuration also preserve this identity.

The terminal and `4am_short_trade_details.txt` show the previous regular-session close,
the configured gap threshold, the first qualifying bar's time and high, the
04:00–04:15 early high and its source bar, the waiting-clock reference, the
short limit and activation time, actual entry time/price/shares, stop and target
prices, exit time/price/reason, target-hit status, and P&L. Window labels and
percentages follow the JSON settings. Times display Eastern AM/PM with EST/EDT.
Unfilled setups show that no entry or exit occurred; unresolved positions do
not receive a completed-trade result.

The candidate and trade CSV files also include `first_gap_bar_high`, `early_high_bar_time`, and
`profit_target_hit`. `first_gap_time` and `early_high_bar_time` identify minute
starts; `early_high_time` remains the wait reference (the minute's end by default).
The first qualifying bar's high is an observed bar high, not an exact first-crossing
trade price. `profit_target_hit` is `True` for a profit-target exit, `False` for a
completed stop or time exit, and blank when no completed exit exists. A profitable
time exit therefore still reports that the target was not hit.

Gross P&L is `(entry price - cover price) × shares`. Net P&L subtracts both sides' commissions and the initial trade's locate fee; a re-entry is not charged a second locate fee. Incomplete positions have no realized P&L and are excluded from completed-trade performance; inspect their status counts and candidate rows before evaluating a run. A skipped setup is distinct from missing or invalid market data. `trade_number` identifies the initial trade (`1`) or re-entry (`2`), with a separate row for each attempt.

Win rate uses net P&L. Profit factor is `null` when there are no losing trades. Drawdown uses closed-trade P&L in exit-time order from a zero starting balance, grouping exits with the same bar timestamp before updating equity; it excludes unrealized losses within trades. There are no portfolio buying-power or simultaneous-position limits. `4am_short_summary.json` also records the execution options `offline`, `refresh_cache`, and `max_candidates`.

The terminal output and trade-details file include a monthly table and a
`TOTAL` row:

```text
Month  Trades  Wins  Losses  Win%  Loss%  Net P/L  Avg/Trade  PF  Max DD  Win Days  Loss Days  Stop Stocks
```

When `strategy.reentry.enabled` is `true`, a separate **Re-entry monthly summary**
follows the combined monthly table in both the terminal and trade-details file.
It uses the same columns and a `TOTAL` row, calculated solely from completed
re-entry trades (`trade_number = 2`). Its P&L, win/loss days, profit factor,
drawdown, and stop counts all exclude initial trades. Processed months with no
completed re-entries show zero trades; skipped and incomplete attempts are
reported separately and excluded from performance. Interrupted runs mark this
table `PARTIAL`. No additional switch is required, and the table is omitted when
re-entry is disabled.

Enabled runs also save `4am_short_reentry_monthly.csv` and the
`reentry_statistics` and `reentry_monthly_statistics` sections in
`4am_short_summary.json`. The existing monthly table and reports still include
both initial and re-entry trades.

A final **Trade summary** follows the monthly tables, showing **Avg winner,
Avg loser, Biggest winner, Biggest loser, Avg trade, and Profit factor** for
the whole run. All amounts use completed trades' net P&L after configured
commissions and locate fees. Winners have positive net P&L; losers have negative
net P&L, and loss amounts keep their minus sign. Biggest loser is the most
negative individual trade. Breakeven trades count in Avg trade but neither
winner nor loser averages. An absent group displays `--`; profit factor displays
`inf` for wins without losses, `0.00` for losses without wins, and `--` when
neither exists. Interrupted runs mark the final summary `PARTIAL`.

These statistics are also saved in the summary JSON and daily/monthly CSVs as
`average_winner_net_pnl`, `average_loser_net_pnl`, `biggest_winner_net_pnl`,
`biggest_loser_net_pnl`, plus the existing `average_net_pnl` and `profit_factor`.
Unavailable numeric values remain JSON `null` (blank in CSV), including infinite
profit factor; the win/loss counts distinguish that case from an undefined ratio.

Before that performance table, a gap-to-trade summary shows each month's
`Gap Triggers`, `Traded`, `Not Traded`, and `Traded%`, with a `TOTAL` row.
It counts stock/date setups with a confirmed first gap in the configured early
window (by default, strictly more than 30% during 04:00–04:15 Eastern).
`Traded` means the entry filled, including positions whose exit remains
unresolved; the summary separately shows completed and unresolved entries.
Thus `Gap Triggers = Traded + Not Traded`. A stock/date is counted only once in
these setup counts. Completed `Trades` include initial and re-entry trades, so
they can exceed `Traded` when re-entry is enabled, or fall below it if an exit is missing.

Qualified but untraded setups are broken down by reason, such as no fill before
the entry deadline. No-early-bar, below-threshold, and data-error candidates
are outside the confirmed-trigger count; data errors do not establish that
the gap condition failed. The daily/monthly CSV and summary JSON include
`gap_triggered`, `gap_traded`, `gap_not_traded`, `gap_traded_percent`,
`gap_completed`, and `gap_unresolved`. JSON also records
`gap_not_traded_reasons`.

The reports also record `reentry_attempts`, `reentry_trades` (completed second
trades), and `reentry_skipped`. Re-entry outcomes are separate from the initial
gap's traded/untraded classification.

`Wins` and `Losses` count completed trades with positive and negative net P&L.
`Win%` and `Loss%` divide these counts by all completed trades, including
breakeven trades in the denominator. Breakeven trades are neither wins nor
losses, so the two percentages can sum to less than 100%. Both percentages
are zero when there are no completed trades. CSV and JSON summaries include
these fields as `wins`, `losses`, `win_percent`, and `loss_percent`.

`Avg/Trade` is net P&L divided by completed trades. `PF` is total positive net
trade P&L divided by the absolute total negative net trade P&L; the table shows
`inf` for wins without losses and `--` when neither wins nor losses exist.
Monthly `Max DD` resets at the start of each month; the `TOTAL` row calculates
drawdown across the whole run, rather than adding monthly drawdowns.
`Win Days` and `Loss Days` count dates with positive and negative combined net
P&L respectively. Breakeven days count as neither. `Stop Stocks` counts all
completed trades with a `stop_loss` exit during the month. The same stock
stopping out on different dates, or on both its initial and re-entry trades,
counts separately. The `TOTAL` row counts
stop-loss exits across the whole run. Losing time exits do not count.

Months with processed candidates but no completed trades remain visible with
zero trades. Incomplete positions and errors are excluded from performance and
identified above the table. Interrupted runs label the table `PARTIAL`.
The daily/monthly CSV files also include `winning_days`, `losing_days`,
`breakeven_days`, and `stops`; the summary JSON includes
`monthly_statistics`. The `TOTAL` row is computed from all completed trades,
so its win rate, average, and PF are not averages of the monthly values.

Exit codes are `0` for a clean run (skips are allowed), `2` for configuration/input/setup errors, `3` if any processed candidate has an error or incomplete position, and `130` for interruption. Ctrl+C during candidate processing writes partial reports for candidates already processed.

## Historical data and limitations

Prices come from Massive's [custom aggregate bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars) and [daily ticker summary](https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary). Execution-day bars and source closes are requested unadjusted. The previous close is then normalized for [splits effective on the trade date](https://massive.com/docs/rest/stocks/corporate-actions/splits), preventing a reverse split from becoming a false gap. The previous session is identified first, and its close is requested for the candidate; the code does not silently substitute an older ticker close.

Minute aggregates contain eligible trades rather than quotes or every print. A minute without eligible trades can be absent, so sparse bars are accepted. The model does not infer spread, queue position, partial fills, borrow availability, DAS locate success, or the market impact of a 1,000-share order. Fees and slippage are configurable approximations. The supplied scanner list can introduce selection or survivorship bias. Backtest results depend on those inputs and execution assumptions.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use synthetic data and mocked historical, SIP, and DAS responses. They require the live dependencies but no credentials and send no live orders. The `--demo` dashboard is also entirely offline: its example symbols, fills, and backtest results are fictional, and no reports or trading state are written.
