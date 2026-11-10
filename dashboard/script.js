const $ = (id) => document.getElementById(id);
const unavailable = "—";
let contractSignature = "";
let latestSnapshot = null;
let commandPending = false;
let refreshInProgress = false;

function display(value, suffix = "") {
  return value === null || value === undefined || value === ""
    ? unavailable
    : `${value}${suffix}`;
}

function setValue(id, value, suffix = "") {
  $(id).textContent = display(value, suffix);
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function setBadge(element, value) {
  const status = value || "—";
  element.textContent = status;
  element.className = "status-badge";
  if (/CONNECTED|FILLED|OPEN|CLOSED|SUCCESS|ACKNOWLEDGED|RECOVERED|TRADING|FRESH/i.test(status)) {
    element.classList.add("status-success");
  } else if (/FAILED|REJECTED|ERROR|RECONCILIATION_REQUIRED|NOT_TRADING/i.test(status)) {
    element.classList.add("status-failed");
  } else if (/PENDING|WAITING|PARTIALLY|SUBMITTED|UNKNOWN|STALE/i.test(status)) {
    element.classList.add("status-pending");
  } else if (/SELECTED|ARMED|TRIGGERED/i.test(status)) {
    element.classList.add("status-selected");
  } else {
    element.classList.add("status-inactive");
  }
}

async function getJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function postJSON(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) {
    const detail = typeof result.detail === "string" ? result.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return result;
}

function fillTable(body, rows, columns, emptyMessage, addCloseAction = false) {
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = columns.length + Number(addCloseAction);
    cell.className = "empty-state";
    cell.textContent = emptyMessage;
    row.append(cell);
    body.append(row);
    return;
  }

  for (const item of rows) {
    const row = document.createElement("tr");
    for (const key of columns) {
      const cell = document.createElement("td");
      const value = item[key];
      cell.textContent = value === null || value === undefined
        ? unavailable
        : /date|expiry|timestamp/i.test(key)
          ? formatDate(value)
          : String(value);
      if (/date|expiry|timestamp/i.test(key)) cell.classList.add("date-display");
      row.append(cell);
    }
    if (addCloseAction) {
      const cell = document.createElement("td");
      const remaining = Number(item.remaining_quantity);
      if (remaining > 0) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button-secondary position-close";
        button.dataset.closeAction = `CLOSE_${item.option_type}`;
        button.dataset.instrument = item.instrument;
        button.textContent = `Close ${item.option_type}`;
        const selected = latestSnapshot?.instruments.selected;
        const kind = item.option_type.toLowerCase();
        button.disabled = commandPending
          || !latestSnapshot?.status.connected
          || latestSnapshot?.config.strategy_enabled
          || Number.isNaN(remaining)
          || remaining <= 0
          || selected?.[kind] !== item.instrument
          || !selected?.[`${kind}_ask`]
          || selected?.[`${kind}_trading_status`] === "NOT_TRADING";
        cell.append(button);
      } else {
        cell.textContent = unavailable;
      }
      row.append(cell);
    }
    body.append(row);
  }
}

function updateContractOptions(contracts, selected) {
  const expirySelect = $("contract-expiry");
  const strikeSelect = $("contract-strike");
  const signature = JSON.stringify(contracts.map((item) => [item.expiry_ns, item.strikes]));
  if (signature !== contractSignature) {
    contractSignature = signature;
    expirySelect.replaceChildren();
    for (const item of contracts) {
      const option = document.createElement("option");
      option.value = String(item.expiry_ns);
      option.textContent = new Date(item.expiry).toLocaleString();
      expirySelect.append(option);
    }
  }

  const preferredExpiry = selected?.expiry_ns === undefined
    ? expirySelect.value
    : String(selected.expiry_ns);
  if (preferredExpiry && document.activeElement !== expirySelect) {
    expirySelect.value = preferredExpiry;
  }
  const chosenExpiry = contracts.find((item) => String(item.expiry_ns) === expirySelect.value);
  const strikeSignature = JSON.stringify(chosenExpiry?.strikes ?? []);
  if (strikeSelect.dataset.signature !== strikeSignature) {
    strikeSelect.dataset.signature = strikeSignature;
    strikeSelect.replaceChildren();
    for (const item of chosenExpiry?.strikes ?? []) {
      const option = document.createElement("option");
      option.value = item.strike;
      option.textContent = item.strike;
      strikeSelect.append(option);
    }
  }
  if (selected?.strike && document.activeElement !== strikeSelect) {
    strikeSelect.value = selected.strike;
  }
  const available = contracts.length > 0;
  expirySelect.disabled = !available || commandPending;
  strikeSelect.disabled = !available || commandPending;
  $("auto-atm").disabled = !available || !latestSnapshot?.status.connected || commandPending;
}

function updateTradingControls() {
  const snapshot = latestSnapshot;
  const selected = snapshot?.instruments.selected;
  const connected = Boolean(snapshot?.status.connected);
  const entryReady = connected && !snapshot?.config.strategy_enabled && !commandPending;
  const contractChoice = $("trade-contract").value;
  const hasOpenCall = snapshot?.positions.items.some(
    (item) => item.option_type === "CALL" && Number(item.remaining_quantity) > 0,
  );
  const hasOpenPut = snapshot?.positions.items.some(
    (item) => item.option_type === "PUT" && Number(item.remaining_quantity) > 0,
  );
  for (const button of document.querySelectorAll(".trade-action")) {
    const action = button.dataset.action;
    const correctChoice = action.endsWith("_BOTH")
      ? contractChoice === "BOTH"
      : action.endsWith("_CALL")
        ? contractChoice === "CALL"
        : contractChoice === "PUT";
    const kind = action.endsWith("_CALL") ? "CALL" : action.endsWith("_PUT") ? "PUT" : "BOTH";
    const hasPosition = kind === "CALL" ? hasOpenCall : kind === "PUT" ? hasOpenPut : hasOpenCall || hasOpenPut;
    const kinds = kind === "BOTH" ? ["CALL", "PUT"] : [kind];
    const isEntry = action.startsWith("SELL_");
    const activeKinds = isEntry
      ? kinds
      : kinds.filter((legKind) =>
          legKind === "CALL" ? hasOpenCall : hasOpenPut,
        );
    const quotesReady = activeKinds.every((legKind) => {
      const leg = legKind.toLowerCase();
      return isEntry
        ? Boolean(selected?.[`${leg}_bid`] && selected?.[`${leg}_ask`])
        : Boolean(selected?.[`${leg}_ask`]);
    });
    const tradable = activeKinds.every((legKind) => {
      const status = selected?.[`${legKind.toLowerCase()}_trading_status`];
      return status !== "NOT_TRADING";
    });
    const entryHasExposure = isEntry && kinds.some((legKind) =>
      legKind === "CALL" ? hasOpenCall : hasOpenPut,
    );
    button.disabled = !entryReady
      || !correctChoice
      || !quotesReady
      || !tradable
      || entryHasExposure
      || (!isEntry && !hasPosition);
  }
  $("trade-quantity").disabled = !entryReady;
  $("trade-order-type").disabled = !entryReady;
  $("call-limit-price").disabled = !entryReady;
  $("put-limit-price").disabled = !entryReady;
  $("positions-table").querySelectorAll(".position-close").forEach((button) => {
    const kind = button.dataset.closeAction.endsWith("_CALL") ? "call" : "put";
    button.disabled = commandPending
      || !connected
      || snapshot?.config.strategy_enabled
      || selected?.[kind] !== button.dataset.instrument
      || !selected?.[`${kind}_ask`]
      || selected?.[`${kind}_trading_status`] === "NOT_TRADING";
  });
  const orderType = $("trade-order-type").value;
  const choice = $("trade-contract").value;
  $("call-price-field").hidden = orderType !== "LIMIT" || choice === "PUT";
  $("put-price-field").hidden = orderType !== "LIMIT" || choice === "CALL";
}

function showSnapshot(data) {
  latestSnapshot = data;
  document.querySelector("main").classList.remove("data-stale-view");
  const { status, market, instruments, contracts, positions, orders, trades, pnl, config } = data;
  $("connection-indicator").className = `indicator ${status.engine_active ? "indicator-on" : "indicator-off"}`;
  $("connection-label").textContent = status.engine_active ? "Trading engine active" : "Trading engine inactive";
  $("notice").textContent = status.error || status.message || "Engine state is available.";
  $("notice").classList.toggle("notice-error", Boolean(status.error) || !status.connected);
  setBadge($("strategy-status"), status.status);
  setBadge(
    $("market-data-status"),
    status.market_data_fresh ? "BTC DATA FRESH" : "BTC DATA STALE / WAITING",
  );

  setValue("btc-price", market.fresh ? market.reference_price : null);
  setValue("watch-btc-price", market.fresh ? market.reference_price : null);
  const referenceTime = market.timestamp
    ? formatDate(market.timestamp)
    : "No exchange update";
  $("btc-updated").textContent = market.timestamp
    ? `${market.fresh ? "Updated" : "Last update (stale)"} ${referenceTime}`
    : referenceTime;
  $("btc-updated").classList.toggle("data-stale", !market.fresh && Boolean(market.timestamp));
  $("watch-btc-time").textContent = market.timestamp
    ? `${market.fresh ? "Updated" : "Last update (stale)"} ${referenceTime}`
    : referenceTime;
  $("watch-btc-time").classList.toggle("data-stale", !market.fresh && Boolean(market.timestamp));
  setValue("atm-strike", instruments.selected?.strike);
  $("selected-expiry").textContent = instruments.selected?.expiry
    ? formatDate(instruments.selected.expiry)
    : "No contracts selected";
  updateContractOptions(contracts.items, instruments.selected);
  setValue("watch-call-symbol", instruments.selected?.call);
  setValue("watch-put-symbol", instruments.selected?.put);
  $("watch-call-quote").textContent = `Bid ${display(instruments.selected?.call_bid)} / Ask ${display(instruments.selected?.call_ask)} · ${instruments.selected?.call_trading_status ?? "UNKNOWN"}`;
  $("watch-put-quote").textContent = `Bid ${display(instruments.selected?.put_bid)} / Ask ${display(instruments.selected?.put_ask)} · ${instruments.selected?.put_trading_status ?? "UNKNOWN"}`;
  const watchSelection = $("watch-selection");
  watchSelection.replaceChildren();
  if (instruments.selected) {
    const expiryText = document.createElement("span");
    expiryText.className = "date-display";
    expiryText.textContent = formatDate(instruments.selected.expiry);
    watchSelection.append(
      expiryText,
      document.createTextNode(` / ${instruments.selected.strike}`),
    );
  } else {
    watchSelection.textContent = unavailable;
  }
  const quoteTimestamp = instruments.selected?.call_quote_timestamp ?? instruments.selected?.put_quote_timestamp;
  $("watch-update-time").textContent = quoteTimestamp
    ? `Last option update ${formatDate(quoteTimestamp)}`
    : "No option quote update";

  const orderFor = (kind) => orders.items.filter((item) => item.option_type === kind).pop();
  setBadge($("call-order-status"), orderFor("CALL")?.status);
  setBadge($("put-order-status"), orderFor("PUT")?.status);
  $("entry-premium").textContent = display(pnl.combined_entry_premium);
  $("entry-currency").textContent = pnl.quote_currency
    ? `Quote currency: ${pnl.quote_currency}`
    : "Actual filled quantities and multipliers";
  setValue("buyback-value", pnl.current_buyback_value);
  setValue("unrealized-pnl", pnl.unrealized_pnl);
  const hasOpenPosition = positions.items.some((item) => Number(item.remaining_quantity) > 0);
  setBadge(
    $("stop-status"),
    pnl.stop_loss_triggered
      ? "TRIGGERED"
      : hasOpenPosition
        ? `${pnl.stop_loss_percent ?? unavailable}% ARMED`
        : "INACTIVE",
  );
  setBadge(
    $("take-status"),
    pnl.take_profit_triggered
      ? "TRIGGERED"
      : hasOpenPosition
        ? `${pnl.take_profit_percent ?? unavailable}% ARMED`
        : "INACTIVE",
  );

  fillTable(
    $("positions-table"),
    positions.items,
    [
      "option_type", "instrument", "strike", "expiry", "requested_quantity",
      "filled_quantity", "remaining_quantity", "entry_premium", "current_bid",
      "current_ask", "current_buyback_value", "unrealized_pnl", "position_status",
    ],
    "No exchange position data received.",
    true,
  );
  setValue("pnl-call-entry", pnl.call_entry_premium, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-put-entry", pnl.put_entry_premium, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-entry", pnl.combined_entry_premium, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-call-buyback", pnl.call_buyback_value, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-put-buyback", pnl.put_buyback_value, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-buyback", pnl.current_buyback_value, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-unrealized", pnl.unrealized_pnl, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-realized", pnl.realized_pnl, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("exit-reason", pnl.exit_reason);

  fillTable(
    $("trades-table"),
    trades.items,
    [
      "order_id", "timestamp", "instrument", "order_side", "order_type",
      "filled_quantity", "average_fill_price", "status", "exchange_order_id",
    ],
    "No exchange executions received.",
  );
  fillTable(
    $("orders-table"),
    orders.items,
    [
      "client_order_id", "timestamp", "instrument", "order_side", "order_type",
      "requested_quantity", "filled_quantity", "average_fill_price", "status", "exchange_order_id",
    ],
    "No order activity received.",
  );
  $("config-environment").textContent = config.environment;
  $("config-enabled").textContent = config.strategy_enabled ? "Yes" : "No";
  $("config-quantity").textContent = config.quantity;
  $("config-stop").textContent = `${config.stop_loss_percent}%`;
  $("config-take").textContent = `${config.take_profit_percent}%`;
  $("last-refresh").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  updateTradingControls();
}

async function refresh() {
  if (refreshInProgress) return;
  refreshInProgress = true;
  try {
    showSnapshot(await getJSON("/api/dashboard"));
  } catch (error) {
    $("connection-indicator").className = "indicator indicator-off";
    $("connection-label").textContent = "Status unavailable";
    $("market-data-status").textContent = "DATA UNAVAILABLE";
    $("market-data-status").className = "status-badge status-failed";
    $("notice").textContent = `Dashboard update failed: ${error.message}. Values below are stale and must not be used.`;
    $("notice").classList.add("notice-error");
    document.querySelector("main").classList.add("data-stale-view");
    latestSnapshot = null;
    updateTradingControls();
  } finally {
    refreshInProgress = false;
  }
}

async function changeContractSelection(autoAtm) {
  const expiry = $("contract-expiry").value;
  const strike = $("contract-strike").value;
  try {
    await postJSON("/api/market/selection", {
      expiry_ns: expiry || null,
      strike: autoAtm ? null : strike,
      auto_atm: autoAtm,
    });
    $("trade-message").textContent = "Contract selection updated.";
    $("trade-message").classList.remove("form-message-error");
    await refresh();
  } catch (error) {
    $("trade-message").textContent = error.message;
    $("trade-message").classList.add("form-message-error");
  }
}

function orderDescription(action, selected, positions, orderType, limitPrices) {
  let kinds = action.endsWith("BOTH")
    ? ["CALL", "PUT"]
    : [action.endsWith("CALL") ? "CALL" : "PUT"];
  const isEntry = action.startsWith("SELL_");
  if (!isEntry && action.endsWith("BOTH")) {
    kinds = kinds.filter((kind) =>
      positions.some((item) => item.option_type === kind && Number(item.remaining_quantity) > 0),
    );
  }
  return kinds.map((kind) => {
    const instrument = selected?.[kind.toLowerCase()];
    const position = positions.find((item) => item.option_type === kind);
    const quantity = isEntry
      ? $("trade-quantity").value
      : position?.remaining_quantity ?? "0";
    const price = orderType === "LIMIT" ? limitPrices[kind] : null;
    return `${instrument ?? kind} — ${isEntry ? "SELL" : "BUY"}, ${orderType}, quantity ${quantity}${price ? `, limit ${price}` : ""}, Bybit Demo`;
  }).join("\n");
}

async function submitManualOrder(action) {
  const snapshot = latestSnapshot;
  if (!snapshot?.instruments.selected) return;
  const orderType = $("trade-order-type").value;
  const limitPrices = {
    CALL: $("call-limit-price").value || null,
    PUT: $("put-limit-price").value || null,
  };
  const description = orderDescription(
    action,
    snapshot.instruments.selected,
    snapshot.positions.items,
    orderType,
    limitPrices,
  );
  if (!window.confirm(`Confirm manual Bybit Demo order:\n\n${description}\n\nSubmit this order?`)) {
    return;
  }

  commandPending = true;
  updateTradingControls();
  $("trade-message").textContent = "Sending request to the NautilusTrader engine…";
  $("trade-message").classList.remove("form-message-error");
  const payload = {
    action,
    order_type: orderType,
    quantity: action.startsWith("SELL_") ? $("trade-quantity").value : null,
    limit_price_call: orderType === "LIMIT" ? limitPrices.CALL : null,
    limit_price_put: orderType === "LIMIT" ? limitPrices.PUT : null,
  };
  try {
    const result = await postJSON("/api/trading/order", payload);
    $("trade-message").textContent = `Engine accepted ${result.orders.map((item) => item.instrument).join(", ")} for submission. Confirm fills and order status in Order History.`;
    await refresh();
  } catch (error) {
    $("trade-message").textContent = `${error.message} Do not retry until you check Order History.`;
    $("trade-message").classList.add("form-message-error");
    await refresh();
  } finally {
    commandPending = false;
    updateTradingControls();
  }
}

$("contract-expiry").addEventListener("change", () => changeContractSelection(true));
$("contract-strike").addEventListener("change", () => changeContractSelection(false));
$("auto-atm").addEventListener("click", () => changeContractSelection(true));
$("trade-contract").addEventListener("change", updateTradingControls);
$("trade-order-type").addEventListener("change", updateTradingControls);
document.querySelectorAll(".trade-action").forEach((button) => {
  button.addEventListener("click", () => submitManualOrder(button.dataset.action));
});
$("positions-table").addEventListener("click", (event) => {
  const button = event.target.closest("[data-close-action]");
  if (button) {
    $("trade-contract").value = button.dataset.closeAction.endsWith("_CALL")
      ? "CALL"
      : "PUT";
    updateTradingControls();
    submitManualOrder(button.dataset.closeAction);
  }
});

refresh();
window.setInterval(refresh, 2000);
