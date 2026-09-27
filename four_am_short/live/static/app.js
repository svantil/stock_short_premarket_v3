"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const dollars = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const stockPrice = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 });
  const etTime = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const etDate = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", year: "numeric" });
  let state = null;
  let controlToken = "";
  let busy = false;
  let online = false;
  let lastUpdate = null;
  const openLocateComparisons = new Set();

  const has = (value) => value !== null && value !== undefined && value !== "";
  const numeric = (value) => has(value) && Number.isFinite(Number(value));
  const num = (value) => numeric(value) ? number.format(Number(value)) : "—";
  const price = (value) => numeric(value) ? stockPrice.format(Number(value)) : "—";
  const money = (value) => numeric(value) ? dollars.format(Number(value)) : "—";
  const percent = (value) => numeric(value) ? Number(value).toFixed(1) + "%" : "—";
  const signClass = (value) => Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "";
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const readable = (value) => String(value ?? "—").replace(/_/g, " ");
  const time = (value) => {
    if (!has(value)) return "—";
    if (/^\d\d:\d\d(?::\d\d)?$/.test(String(value))) return String(value);
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : etTime.format(date);
  };
  const datedTime = (value) => {
    if (!has(value)) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : `${etDate.format(date)} ${etTime.format(date)} ET`;
  };
  const age = (value) => {
    if (!has(value)) return "No quote";
    const stamp = new Date(value).valueOf();
    if (!Number.isFinite(stamp)) return "Quote time unavailable";
    const seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000));
    return seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`;
  };
  const text = (id, value) => { $(id).textContent = String(value ?? "—"); };
  const pair = (main, sub, className = "") => `<span class="${className}">${esc(main)}</span><small>${esc(sub)}</small>`;
  const tag = (value) => {
    const label = readable(value);
    const tone = /^(entered|open|filled|qualified|located|closed|ready|profit.target|available|selected|eligible)$/i.test(label) ? "positive" : /reject|error|fail|stop.loss/i.test(label) ? "negative" : /pending|waiting|locating|deferred|partial|stale|quote only/i.test(label) ? "caution" : "";
    return `<span class="cell-tag ${tone}">${esc(label)}</span>`;
  };
  const empty = (columns, message) => `<tr><td colspan="${columns}" class="empty">${esc(message)}</td></tr>`;

  function locateComparisons(rows, key) {
    const comparisons = Array.isArray(rows) ? rows.filter((row) => row && typeof row === "object") : [];
    if (!comparisons.length) return "";
    const contents = comparisons.map((row) => {
      const status = row.quote_only ? "quote_only" : row.selected ? "selected" : row.eligible ? "eligible" : row.status || "unavailable";
      const values = [`Quote ${price(row.price)}/sh`, `${num(row.available_qty)} shares`];
      if (numeric(row.minimum_fee)) values.push(`Minimum ${money(row.minimum_fee)}`);
      if (numeric(row.total_cost)) values.push(`Quoted total ${money(row.total_cost)}`);
      if (numeric(row.effective_price)) values.push(`${price(row.effective_price)}/sh incl. minimum`);
      const policy = row.quote_only ? "Quote only · no purchase" : row.price_cap_enforced === false ? "Quoted-cost purchase · final charge can change" : row.price_cap_enforced === true || row.route_type === 1 ? "Price cap enforced" : "";
      const reason = row.reason || (row.quote_only ? "Purchase disabled for this route." : "");
      const actual = [];
      if (numeric(row.actual_price)) actual.push(`Actual ${price(row.actual_price)}/sh`);
      if (numeric(row.actual_total_cost)) actual.push(`Paid ${money(row.actual_total_cost)}`);
      if (numeric(row.actual_effective_price)) actual.push(`${price(row.actual_effective_price)}/sh incl. minimum`);
      const warning = row.warning || (row.cost_exceeded_ceiling ? "Actual cost exceeded the configured quoted-cost ceiling." : row.cost_exceeded_quote ? "Actual cost exceeded the quote." : "");
      return `<li><strong>${esc(row.route || "Unknown route")}</strong> ${tag(status)}<small>${esc(values.join(" · "))}</small>${policy ? `<small>${esc(policy)}</small>` : ""}${reason ? `<small>${esc(reason)}</small>` : ""}${row.notes && row.notes !== reason ? `<small>${esc(row.notes)}</small>` : ""}${actual.length ? `<small>${esc(actual.join(" · "))}</small>` : ""}${warning ? `<small class="locate-cost-warning">${esc(warning)}</small>` : ""}</li>`;
    }).join("");
    return `<details class="locate-comparisons" data-locate-key="${esc(key)}"${openLocateComparisons.has(key) ? " open" : ""}><summary>${num(comparisons.length)} route quotes</summary><ul>${contents}</ul></details>`;
  }

  function locateCell(locate, triggerPrice, comparisonKey = "") {
    if (!locate || (typeof locate === "object" && !Object.keys(locate).length)) {
      return pair(numeric(triggerPrice) ? `Bid ≥ ${price(triggerPrice)}` : "—", "Not requested");
    }
    if (typeof locate !== "object") return esc(readable(locate));
    const quantity = locate.shares ?? locate.quantity ?? locate.available_qty ?? locate.located_qty;
    const cost = locate.total_cost ?? locate.cost;
    const pieces = [];
    if (numeric(quantity)) pieces.push(`${num(quantity)} sh`);
    if (numeric(cost)) pieces.push(money(cost));
    if (numeric(locate.fee_per_share)) pieces.push(`${price(locate.fee_per_share)}/sh`);
    if (locate.route) pieces.push(locate.route);
    const check = locate.eligibility;
    const diagnostics = [];
    if (check?.reason) {
      if (check.checked_at) diagnostics.push(`Checked ${time(check.checked_at)} ET`);
      if (numeric(check.bid)) diagnostics.push(`Bid ${price(check.bid)}`);
      if (numeric(check.trigger_price)) diagnostics.push(`Trigger ${price(check.trigger_price)}`);
      if (numeric(check.quote_age_seconds)) diagnostics.push(`Quote age ${Number(check.quote_age_seconds).toFixed(2)}s`);
    }
    return tag(locate.status || "requested") + `<small>${esc(pieces.join(" · ") || locate.note || "—")}</small>` +
      (diagnostics.length ? `<small>${esc(diagnostics.join(" · "))}</small>` : "") + locateComparisons(locate.comparisons, comparisonKey);
  }

  function renderRules(raw = {}) {
    const rules = raw.strategy || raw;
    const shares = raw.shares ?? rules.shares;
    const reentry = rules.reentry || {};
    const reentryEnabled = raw.reentry_enabled ?? reentry.enabled ?? false;
    const lateGaps = rules.late_gap_enabled === true;
    const discoveryEnd = lateGaps ? rules.entry_deadline : rules.early_end;
    const highReset = rules.repeated_high_policy === "first" ? "Within window: higher high resets wait" : "Within window: higher or equal high resets wait";
    const locateTrigger = raw.locate_trigger_below_entry_percent;
    const quoteRoutes = Array.isArray(raw.locate_quote_routes) ? raw.locate_quote_routes : [];
    $("locateRouteMode").classList.toggle("hidden", !quoteRoutes.length);
    text("locateRouteMode", quoteRoutes.length ? `${quoteRoutes.join(", ")}: ${raw.allow_uncapped_locate_purchases ? "purchases use the quoted cost; the final charge can change. Each quote must meet the configured per-share ceiling, including minimum fees." : "quotes only; no purchases from these routes."}` : "");
    const locateTiming = locateTrigger === null ? "Locate: after timing gates" : numeric(locateTrigger)
      ? Number(locateTrigger) === 0 ? "Locate: bid ≥ entry limit" : `Locate: bid ≥ entry − ${Number(locateTrigger)}%`
      : "Locate timing unavailable";
    const entryPriceRange = numeric(rules.min_entry_price) && numeric(rules.max_entry_price)
      ? `${price(rules.min_entry_price)}–${price(rules.max_entry_price)} inclusive`
      : numeric(rules.min_entry_price) ? `≥ ${price(rules.min_entry_price)}`
      : numeric(rules.max_entry_price) ? `≤ ${price(rules.max_entry_price)}` : "Unrestricted";
    const cards = [
      ["01 / Find the gap", `>${has(rules.gap_percent) ? rules.gap_percent : "—"}%`, `${rules.early_start || "—"}–${discoveryEnd || "—"} ET · end excluded`, lateGaps ? "New stocks qualify throughout discovery" : "Early-window discovery"],
      ["02 / Wait for high", `${rules.wait_after_high_minutes ?? "—"} min`, lateGaps ? `After high + window complete · late window: ${rules.late_gap_window_minutes ?? "—"} min from first gap` : "After high + window complete", `${highReset} · from ${rules.high_time_reference === "bar_start" ? "minute start" : "minute end"}`],
      ["03 / Short entry", `${rules.entry_below_high_percent ?? "—"}% below high`, `${num(shares)} shares · initial entry`, locateTiming, `Price gate (both entries): ${entryPriceRange}`],
      ["04 / Entry deadline", rules.entry_deadline || "—", "Must fill before this time"],
      ["05 / Stop & target", `+${rules.stop_loss_percent ?? "—"}% / −${rules.profit_target_percent ?? "—"}%`, "From actual average entry"],
      ["06 / Time exit", rules.time_exit || "—", "Cover remaining shares"],
    ];
    cards.push(
      ["07 / Re-entry", reentryEnabled ? "ON · after stop-loss" : "OFF", `${reentry.entry_above_high_percent ?? "—"}% above original setup high`, "At most one · existing borrow only"],
      ["08 / Re-entry stop & target", `+${reentry.stop_loss_percent ?? "—"}% / −${reentry.profit_target_percent ?? "—"}%`, "From actual re-entry fill", `${num(shares)} shares`],
      ["09 / Re-entry timing", `Enter < ${reentry.entry_deadline || "—"}`, `Cover remaining at ${reentry.time_exit || "—"} ET`, "After first position is fully stopped out"],
    );
    $("ruleCards").innerHTML = cards.map(([label, value, ...details], index) => `<article class="rule-card${index >= 6 ? " reentry-rule" : ""}"><span>${esc(label)}</span><strong>${esc(value)}</strong>${details.filter(Boolean).map((detail) => `<small>${esc(detail)}</small>`).join("")}</article>`).join("");
    text("reentryStatus", reentryEnabled ? "One re-entry after a stop-loss · existing borrow only" : "Re-entry off");
  }

  function renderControls() {
    const current = state || {};
    const available = online && !!controlToken && !busy;
    const monitor = current.mode === "monitor";
    text("startButton", monitor ? "Start monitoring" : current.mode === "das_live" ? "Start DAS LIVE trading" : "Start DAS paper trading");
    $("startButton").disabled = !available || (current.running && (current.demo || current.entries_enabled));
    $("stopButton").disabled = !available || (!current.running && !current.entries_enabled);
    $("coverButton").disabled = !available || monitor || !(current.trades || []).some((trade) => Number(trade.remaining_qty) > 0 || /pending|working|partial|submitted/.test(trade.status || ""));
    $("backtestButton").disabled = !available || !!current.backtest?.running;
    text("backtestButton", current.backtest?.running ? "Backtest running…" : "Run backtest");
  }

  function renderStatus() {
    const data = state.data || {};
    const broker = state.broker || {};
    const counts = state.counts || {};
    const monitor = state.mode === "monitor";
    const live = state.mode === "das_live";
    const dasUnavailable = !monitor && !state.demo && !broker.connected;
    $("modeBadge").className = `badge ${monitor ? "monitor" : live ? "live" : "paper"}`;
    text("modeBadge", state.demo ? "DEMO · SAMPLE DATA" : monitor ? "MONITOR ONLY" : live ? "DAS LIVE" : "DAS PAPER");
    text("executionStatus", dasUnavailable && state.entries_enabled ? "Waiting for DAS" : state.entries_enabled ? "New entries enabled" : state.running ? "Monitoring active" : "Entries stopped");
    text("executionDetail", state.demo ? "Sample signals and fills. No locate or trade orders." : monitor ? "Watching signals only. No locate or trade orders." : dasUnavailable ? state.entries_enabled ? "New entries are enabled but waiting for a verified DAS connection. Reconnection is automatic." : state.running ? "New entries are disabled. Position management is waiting for DAS to reconnect." : "New entries are disabled. Start the supervisor to connect to DAS." : state.entries_enabled ? "Qualifying entries and locates may be sent to DAS." : "New entries are disabled. Existing positions stay managed.");
    $("sipDot").className = `dot ${data.connected && data.ready ? "on" : data.connected ? "warn" : "off"}`;
    $("dasDot").className = `dot ${monitor || state.demo ? "" : broker.connected ? "on" : broker.checking ? "warn" : "off"}`;
    text("sipStatus", data.status || (data.connected ? "Connected" : "Disconnected"));
    const dasLabel = state.demo ? "Sample connection · no broker session" : monitor ? "Not used in monitor mode" : broker.connected ? `Connected · verified${broker.checking ? " · checking…" : ""}` : broker.checking ? "Reconnecting…" : state.running ? "Disconnected" : "Not connected · supervisor stopped";
    text("dasStatus", [dasLabel, !monitor && !state.demo && broker.account_masked].filter(Boolean).join(" · "));
    const dasDetails = [];
    if (!monitor && !state.demo) {
      dasDetails.push(broker.last_connected_at ? `Last verified ${datedTime(broker.last_connected_at)}` : "No verified DAS connection yet");
      if (!broker.connected && !broker.checking && broker.next_check_at) dasDetails.push(`Next retry ${time(broker.next_check_at)} ET`);
      if (!broker.connected && broker.last_error) dasDetails.push(broker.last_error);
    }
    text("dasHealthDetail", dasDetails.join(" · "));
    text("closeDate", data.previous_close_date || "Not loaded");
    text("universeSize", `${num(data.universe_size ?? counts.candidates ?? 0)} stocks · ${data.backfill_ready ? "history ready" : "history pending"}`);
    text("qualifiedCount", num(counts.qualified ?? 0));
    text("candidateCount", `${num(counts.candidates ?? state.candidates?.length ?? 0)} watched stocks`);
    text("enteredCount", num(counts.entered ?? 0));
    text("skippedCount", `${num(counts.skipped ?? 0)} skipped`);
    text("openCount", num(counts.open ?? 0));
    text("closedCount", `${num(counts.closed ?? 0)} closed`);
    const realized = (state.trades || []).reduce((total, trade) => total + (Number(trade.realized_pnl) || 0), 0);
    text("realizedPnl", money(realized));
    text("pnlBasis", `${state.demo ? "Fictional" : monitor ? "Simulated" : "DAS"} fills · before commissions and locates`);
    text("fillBasis", state.demo ? "Fictional preview fills" : monitor ? "Simulated entries at bid and exits at ask" : "Entry and exit prices reflect confirmed DAS fills");
    $("realizedPnl").className = signClass(realized);
    const warning = $("connectionWarning");
    const issues = [];
    if (state.demo) issues.push("Demo preview uses sample data. No market connection, locates, or trade orders are sent.");
    if (state.entry_block) issues.push(state.entry_block);
    if (data.error) issues.push(`SIP: ${data.error}`);
    if (state.running && !data.ready) issues.push("SIP data is not ready. New entries require healthy market data.");
    if (dasUnavailable && state.running) issues.push("DAS is not verified. New entries are blocked while the service reconnects. Existing orders may still fill at the broker; this service cannot manage positions or submit covers until DAS is available. Check any open exposure in DAS.");
    if (state.running && data.backfill_ready === false) issues.push("Discovery history is still loading.");
    warning.classList.toggle("hidden", !issues.length);
    text("connectionWarning", issues.join(" "));
    text("candidateFooter", data.last_message ? `Last SIP message ${time(data.last_message)} ET` : "Awaiting market data");
  }

  function renderCandidates() {
    if (!state) return;
    const filter = $("symbolFilter").value.trim().toUpperCase();
    const qualifiedOnly = $("qualifiedOnly").checked;
    const candidates = (state.candidates || []).filter((row) => (!filter || String(row.symbol || "").toUpperCase().includes(filter)) && (!qualifiedOnly || row.first_gap_time || row.qualified));
    candidates.sort((a, b) => Number(!!b.first_gap_time) - Number(!!a.first_gap_time) || String(a.symbol).localeCompare(String(b.symbol)));
    text("candidateBadge", num(candidates.length));
    $("candidateRows").innerHTML = candidates.length ? candidates.map((row) => {
      const quote = row.quote || {};
      const gap = row.first_gap_price ?? row.first_gap_bar_high ?? row.first_gap_high ?? row.gap_trigger_price;
      const rules = state.rules?.strategy || state.rules || {};
      const threshold = Number(row.previous_close) * (1 + Number(rules.gap_percent ?? 30) / 100);
      const gapPrice = numeric(gap) ? price(gap) : row.first_gap_time ? `>${price(threshold)}` : "—";
      const leg = Number(row.trade_number ?? 1) === 2 ? "Re-entry" : "Initial";
      return `<tr><td>${pair(row.symbol || "—", `Close ${price(row.previous_close)}`, "stock-symbol")}</td><td>${pair(gapPrice, time(row.first_gap_time))}</td><td>${pair(price(row.early_high), time(row.early_high_time))}</td><td>${pair(`${leg} ${price(row.entry_limit)}`, time(row.active_at))}</td><td>${pair(`${price(quote.bid)} / ${price(quote.ask)}`, age(quote.timestamp))}</td><td>${locateCell(row.locate, row.locate_trigger_price, `candidate:${row.symbol}:${leg}`)}</td><td>${tag(row.status)}<small class="cell-note">${esc(row.note || "")}</small></td></tr>`;
    }).join("") : empty(7, filter || qualifiedOnly ? "No candidates match this filter." : "No gap candidates yet. Start monitoring to discover stocks.");
  }

  function renderTrades() {
    const trades = state.trades || [];
    text("tradeBadge", num(trades.length));
    $("tradeRows").innerHTML = trades.length ? trades.map((row) => {
      const hasExitFill = numeric(row.exit_avg_price) && Number(row.exit_avg_price) > 0;
      const target = hasExitFill ? row.exit_reason === "profit_target" ? "YES" : "NO" : "Pending";
      const leg = Number(row.trade_number ?? 1) === 2 ? "Re-entry" : "Initial";
      return `<tr><td><strong class="stock-symbol">${esc(row.symbol)}</strong><small>${esc(leg)} · ${tag(row.status)}</small></td><td>${pair(`${num(row.entry_filled_qty ?? 0)} / ${num(row.requested_qty)}`, `${num(row.remaining_qty ?? 0)} open`)}</td><td>${pair(price(row.entry_avg_price), time(row.entry_time))}</td><td>${pair(`Stop ${price(row.stop_price)}`, `Target ${price(row.target_price)}`)}</td><td>${pair(price(row.exit_avg_price), time(row.exit_time))}</td><td>${pair(readable(row.exit_reason), `Target hit: ${target}`)}</td><td class="${signClass(row.realized_pnl)}">${esc(money(row.realized_pnl))}</td><td>${locateCell(row.locate, null, `trade:${row.symbol}:${leg}`)}<small class="cell-note">${esc(row.note || "")}</small></td></tr>`;
    }).join("") : empty(8, "No orders or positions for this session.");
  }

  function renderBacktest() {
    const backtest = state.backtest || {};
    const summary = backtest.latest_summary?.statistics || backtest.latest_summary?.summary || backtest.latest_summary || {};
    text("backtestStatus", backtest.running ? "Backtest running with backtest_4am_short.json. Results appear here when complete." : backtest.error ? `Backtest did not finish: ${backtest.error}` : has(summary.trades) ? "Latest saved results · 4am short" : "No saved results yet. Run the backtest using your configured input file.");
    const items = [["Gap triggers", num(summary.gap_triggered)], ["Traded / not traded", `${num(summary.gap_traded)} / ${num(summary.gap_not_traded)}`], ["Completed trades", num(summary.trades)], ["Win rate", percent(summary.win_percent)], ["Net P/L", money(summary.net_pnl), signClass(summary.net_pnl)], ["Max drawdown", money(summary.max_closed_trade_drawdown)]];
    $("backtestSummary").innerHTML = has(summary.trades) ? items.map(([label, value, tone]) => `<div class="summary-item"><span>${esc(label)}</span><strong class="${tone || ""}">${esc(value)}</strong></div>`).join("") : "";
    text("reentryBacktestSummary", has(summary.reentry_trades) ? `Re-entry: ${num(summary.reentry_trades)} completed · ${num(summary.reentry_attempts)} attempts · ${num(summary.reentry_skipped)} skipped. Gap counts include each stock/date once; completed trades include both entries.` : "Gap counts include each stock/date once; completed trades include both entries when re-entry is enabled.");
    const monthly = Array.isArray(backtest.monthly) ? [...backtest.monthly] : [];
    if (monthly.length && has(summary.trades)) monthly.push({ ...summary, period: "TOTAL" });
    $("monthlyRows").innerHTML = monthly.length ? monthly.map((row) => {
      const factor = numeric(row.profit_factor) ? Number(row.profit_factor).toFixed(2) : Number(row.wins) > 0 && !Number(row.losses) ? "∞" : "—";
      const cells = [row.period || row.month || "—", num(row.gap_triggered), num(row.gap_traded), num(row.gap_not_traded), num(row.trades), num(row.wins), num(row.losses), percent(row.win_percent), percent(row.loss_percent), money(row.net_pnl), money(row.average_net_pnl), factor, money(row.max_closed_trade_drawdown), num(row.winning_days), num(row.losing_days), num(row.stops)];
      return `<tr class="${row.period === "TOTAL" ? "total-row" : ""}">${cells.map((cell, index) => `<td${index === 9 ? ` class="${signClass(row.net_pnl)}"` : ""}>${esc(cell)}</td>`).join("")}</tr>`;
    }).join("") : empty(16, "No monthly results available yet.");
    text("reportsDir", backtest.reports_dir || "No report directory yet.");
    text("backtestOutput", Array.isArray(backtest.last_output) ? backtest.last_output.join("\n") : backtest.last_output || "No output yet.");
  }

  function renderEvents() {
    const events = [...(state.events || [])].sort((a, b) => String(b.time).localeCompare(String(a.time))).slice(0, 150);
    text("eventCount", `${num(events.length)} recent events`);
    $("eventRows").innerHTML = events.length ? events.map((event) => `<li class="event"><time>${esc(time(event.time))}</time><span class="event-level ${["info", "warning", "error", "critical"].includes(event.level) ? event.level : ""}">${esc(event.level || "info")}</span><span class="event-symbol">${esc(event.symbol || "—")}</span><span class="event-message">${esc(event.message)}</span></li>`).join("") : '<li class="empty">No events yet.</li>';
  }

  function render(nextState) {
    state = nextState;
    lastUpdate = new Date();
    online = true;
    renderStatus();
    renderRules(state.rules);
    renderCandidates();
    renderTrades();
    renderBacktest();
    renderEvents();
    renderControls();
    text("updatedAt", `Updated ${time(lastUpdate)} ET · Refreshes every second`);
  }

  async function request(path, options = {}) {
    const abort = new AbortController();
    const timeout = window.setTimeout(() => abort.abort(), options.method === "POST" ? 45000 : 5000);
    try {
      const response = await fetch(path, { cache: "no-store", credentials: "same-origin", ...options, signal: abort.signal });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : `Request failed (${response.status}).`);
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Request timed out. Check orders and activity before retrying the action.");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function poll() {
    try {
      if (!controlToken) controlToken = (await request("/api/session")).control_token;
      render(await request("/api/state"));
    } catch (_) {
      online = false;
      $("connectionWarning").classList.remove("hidden");
      text("connectionWarning", `Dashboard connection lost. ${lastUpdate ? `Last update ${time(lastUpdate)} ET. Displayed values may be stale.` : "Check that the local strategy service is running."} Reconnecting…`);
      $("sipDot").className = "dot warn";
      $("dasDot").className = "dot warn";
      text("sipStatus", "Unknown · dashboard disconnected");
      text("dasStatus", "Unknown · dashboard disconnected");
      text("executionStatus", "Trading status unavailable");
      text("executionDetail", "The dashboard cannot verify the strategy service. Check DAS for current orders and positions.");
      text("updatedAt", "Dashboard disconnected");
      renderControls();
    } finally {
      window.setTimeout(poll, 1000);
    }
  }

  async function command(action, body = {}) {
    if (busy || !online || !controlToken) return;
    busy = true;
    $("controlError").classList.add("hidden");
    renderControls();
    try {
      render(await request(`/api/${action}`, { method: "POST", headers: { "Content-Type": "application/json", "X-Control-Token": controlToken }, body: JSON.stringify(body) }));
    } catch (error) {
      text("controlError", error.message);
      $("controlError").classList.remove("hidden");
      // If the service restarted, obtain its new token before another action.
      controlToken = "";
    } finally {
      busy = false;
      renderControls();
    }
  }

  $("startButton").addEventListener("click", () => command("start"));
  document.addEventListener("toggle", (event) => {
    const key = event.target.dataset?.locateKey;
    if (key) event.target.open ? openLocateComparisons.add(key) : openLocateComparisons.delete(key);
  }, true);
  $("stopButton").addEventListener("click", () => command("stop"));
  $("backtestButton").addEventListener("click", () => command("backtest"));
  $("symbolFilter").addEventListener("input", renderCandidates);
  $("qualifiedOnly").addEventListener("change", renderCandidates);
  $("coverButton").addEventListener("click", () => { $("coverConfirmation").value = ""; $("confirmCover").disabled = true; $("coverDialog").showModal(); $("coverConfirmation").focus(); });
  $("cancelCover").addEventListener("click", () => $("coverDialog").close());
  $("coverConfirmation").addEventListener("input", () => { $("confirmCover").disabled = $("coverConfirmation").value !== "COVER 4AM SHORT"; });
  $("coverForm").addEventListener("submit", (event) => { event.preventDefault(); const confirmation = $("coverConfirmation").value; if (confirmation !== "COVER 4AM SHORT") return; $("coverDialog").close(); command("cover", { confirmation }); });
  function clock() { const now = new Date(); text("clock", etTime.format(now)); text("clockDate", `${etDate.format(now)} · ET`); }
  clock();
  window.setInterval(clock, 1000);
  poll();
})();
