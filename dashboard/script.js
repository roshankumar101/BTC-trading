const $ = (id) => document.getElementById(id);
const unavailable = "—";

function display(value, suffix = "") {
  return value === null || value === undefined || value === "" ? unavailable : `${value}${suffix}`;
}

function setValue(id, value, suffix = "") {
  $(id).textContent = display(value, suffix);
}

function setBadge(element, value) {
  const status = value || "—";
  element.textContent = status;
  element.className = "status-badge";
  if (/CONNECTED|FILLED|OPEN|CLOSED|SUCCESS|ACKNOWLEDGED|RECOVERED/i.test(status)) {
    element.classList.add("status-success");
  } else if (/FAILED|REJECTED|ERROR|RECONCILIATION_REQUIRED/i.test(status)) {
    element.classList.add("status-failed");
  } else if (/PENDING|WAITING|PARTIALLY|SUBMITTED/i.test(status)) {
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

function fillTable(body, rows, columns, emptyMessage) {
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = columns.length;
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
      cell.textContent = value === null || value === undefined ? unavailable : String(value);
      row.append(cell);
    }
    body.append(row);
  }
}

function showSnapshot(data) {
  const { status, market, instruments, positions, orders, trades, pnl, config } = data;
  $("connection-indicator").className = `indicator ${status.connected ? "indicator-on" : "indicator-off"}`;
  $("connection-label").textContent = status.connected ? "Connected" : "Disconnected";
  $("notice").textContent = status.error || status.message || "Engine state is available.";
  $("notice").classList.toggle("notice-error", Boolean(status.error) || !status.connected);
  setBadge($("strategy-status"), status.status);

  setValue("btc-price", market.reference_price);
  $("btc-updated").textContent = market.timestamp
    ? `Updated ${new Date(market.timestamp).toLocaleString()}`
    : "No exchange update";
  setValue("atm-strike", instruments.selected?.strike);
  $("selected-expiry").textContent = instruments.selected?.expiry
    ? new Date(instruments.selected.expiry).toLocaleString()
    : "No contracts selected";

  const orderFor = (kind) => orders.items.filter((item) => item.option_type === kind).pop();
  setBadge($("call-order-status"), orderFor("CALL")?.status);
  setBadge($("put-order-status"), orderFor("PUT")?.status);

  $("entry-premium").textContent = display(pnl.combined_entry_premium);
  $("entry-currency").textContent = pnl.quote_currency
    ? `Quote currency: ${pnl.quote_currency}`
    : "Actual filled quantities and multipliers";
  setValue("buyback-value", pnl.current_buyback_value);
  setValue("unrealized-pnl", pnl.unrealized_pnl);
  const hasOpenPosition = positions.items.some(
    (item) => Number(item.remaining_quantity) > 0,
  );
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
  );
  setValue("pnl-entry", pnl.combined_entry_premium, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-buyback", pnl.current_buyback_value, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-unrealized", pnl.unrealized_pnl, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("pnl-realized", pnl.realized_pnl, pnl.quote_currency ? ` ${pnl.quote_currency}` : "");
  setValue("exit-reason", pnl.exit_reason);

  fillTable(
    $("trades-table"),
    trades.items,
    [
      "timestamp", "instrument", "order_side", "order_type", "filled_quantity",
      "average_fill_price", "status", "exchange_order_id",
    ],
    "No exchange executions received.",
  );
  fillTable(
    $("orders-table"),
    orders.items,
    [
      "timestamp", "instrument", "order_side", "order_type", "requested_quantity",
      "filled_quantity", "average_fill_price", "status", "exchange_order_id",
    ],
    "No order activity received.",
  );
  $("config-environment").textContent = config.environment;
  $("config-enabled").textContent = config.strategy_enabled ? "Yes" : "No";
  $("config-quantity").textContent = config.quantity;
  $("config-stop").textContent = `${config.stop_loss_percent}%`;
  $("config-take").textContent = `${config.take_profit_percent}%`;
  $("last-refresh").textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

async function refresh() {
  try {
    const [status, market, instruments, positions, orders, trades, pnl, config] = await Promise.all([
      getJSON("/api/strategy/status"),
      getJSON("/api/market/btc"),
      getJSON("/api/strategy/instruments"),
      getJSON("/api/positions"),
      getJSON("/api/orders"),
      getJSON("/api/trades"),
      getJSON("/api/pnl"),
      getJSON("/api/config"),
    ]);
    showSnapshot({ status, market, instruments, positions, orders, trades, pnl, config });
  } catch (error) {
    $("connection-indicator").className = "indicator indicator-off";
    $("connection-label").textContent = "Disconnected";
    $("notice").textContent = `Dashboard update failed: ${error.message}`;
    $("notice").classList.add("notice-error");
  }
}

refresh();
window.setInterval(refresh, 2000);
