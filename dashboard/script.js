const $ = (id) => document.getElementById(id);
const unavailable = "—";
let contractSignature = "";
let latestSnapshot = null;
let commandPending = false;
let dashboardSocket = null;
let reconnectTimer = null;
let reconnectDelay = 1000;

function display(value, suffix = "") {
  return value === null || value === undefined || value === ""
    ? unavailable
    : `${value}${suffix}`;
}

function setValue(id, value, suffix = "") {
  setText($(id), display(value, suffix));
}

function setText(element, value) {
  if (element && element.textContent !== value) {
    element.textContent = value;
  }
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function setBadge(element, value) {
  const status = value || "—";
  let className = "status-badge";
  if (/FAILED|REJECTED|ERROR|DISCONNECTED|RECONCILIATION_REQUIRED|NOT_TRADING/i.test(status)) {
    className += " status-failed";
  } else if (/PENDING|WAITING|PARTIALLY|SUBMITTED|UNKNOWN|STALE/i.test(status)) {
    className += " status-pending";
  } else if (/CONNECTED|FILLED|OPEN|CLOSED|SUCCESS|ACKNOWLEDGED|RECOVERED|TRADING|FRESH/i.test(status)) {
    className += " status-success";
  } else if (/SELECTED|ARMED|TRIGGERED/i.test(status)) {
    className += " status-selected";
  } else {
    className += " status-inactive";
  }
  setText(element, status);
  if (element.className !== className) {
    element.className = className;
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
  const signature = JSON.stringify([
    rows,
    columns,
    emptyMessage,
    addCloseAction,
    addCloseAction
      ? [
        commandPending,
        latestSnapshot?.status.connected,
        latestSnapshot?.config.strategy_enabled,
        latestSnapshot?.instruments.selected,
      ]
      : null,
  ]);
  if (body.dataset.renderSignature === signature) return;
  body.dataset.renderSignature = signature;
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
  const hasOpenCall = snapshot?.positions.items.some(
    (item) => item.option_type === "CALL" && Number(item.remaining_quantity) > 0,
  );
  const hasOpenPut = snapshot?.positions.items.some(
    (item) => item.option_type === "PUT" && Number(item.remaining_quantity) > 0,
  );
  const hasOpenExposure = hasOpenCall || hasOpenPut;
  const hasPendingOrder = snapshot?.orders.items.some((item) =>
    ["SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "SUBMISSION_UNKNOWN"].includes(item.status),
  );
  for (const button of document.querySelectorAll(".trade-action")) {
    const action = button.dataset.action;
    if (action === "OPEN_STRADDLE") {
      const quotesReady = ["call", "put"].every((kind) =>
        Boolean(selected?.[kind] && selected?.[`${kind}_bid`] && selected?.[`${kind}_ask`]),
      );
      const tradable = selected?.call_trading_status === "TRADING"
        && selected?.put_trading_status === "TRADING";
      button.disabled = !entryReady || !quotesReady || !tradable
        || hasOpenExposure || hasPendingOrder;
      continue;
    }

    const kinds = action === "CLOSE_BOTH"
      ? ["CALL", "PUT"]
      : [action.endsWith("_CALL") ? "CALL" : "PUT"];
    const activeKinds = kinds.filter((kind) =>
      kind === "CALL" ? hasOpenCall : hasOpenPut,
    );
    const quotesReady = activeKinds.every((legKind) => {
      const leg = legKind.toLowerCase();
      return Boolean(selected?.[`${leg}_ask`]);
    });
    const tradable = activeKinds.every((legKind) => {
      const status = selected?.[`${legKind.toLowerCase()}_trading_status`];
      return status !== "NOT_TRADING";
    });
    button.disabled = !entryReady
      || activeKinds.length === 0
      || activeKinds.some((kind) =>
        selected?.[kind.toLowerCase()] !== snapshot?.positions.items.find(
          (item) => item.option_type === kind && Number(item.remaining_quantity) > 0,
        )?.instrument,
      )
      || !quotesReady
      || !tradable;
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
  $("call-price-field").hidden = orderType !== "LIMIT";
  $("put-price-field").hidden = orderType !== "LIMIT";
}

function renderConnectionStatus(status) {
  const dataConnected = Boolean(status.data_connected);
  const executionConnected = Boolean(status.execution_connected);
  const indicatorClass = `indicator ${dataConnected && executionConnected ? "indicator-on" : "indicator-off"}`;
  if ($("connection-indicator").className !== indicatorClass) {
    $("connection-indicator").className = indicatorClass;
  }
  setText(
    $("connection-label"),
    `${dataConnected ? "Data connected" : "Data disconnected"} · ${executionConnected ? "Execution connected" : "Execution disconnected"}`,
  );
  const noticeText = [
    status.error,
    status.strategy_message,
    status.message,
  ].filter(Boolean).join(" ");
  setText($("notice"), noticeText);
  $("notice").classList.toggle(
    "notice-error",
    Boolean(status.error || status.strategy_message) || !status.connected,
  );
  const marketStatus = !status.market_data_fresh
    ? "BTC DATA STALE / WAITING"
    : status.option_quotes_fresh
      ? "BTC & OPTION DATA FRESH"
      : "BTC DATA FRESH · OPTIONS WAITING";
  setBadge($("market-data-status"), marketStatus);
}

function renderStrategyState(status) {
  setBadge($("strategy-status"), status.status);
  renderConnectionStatus(status);
}

function renderMarket(data) {
  const { market, instruments, contracts } = data;
  const selected = instruments.selected;
  setValue("btc-price", market.fresh ? market.reference_price : null);
  setValue("watch-btc-price", market.fresh ? market.reference_price : null);
  const referenceTime = market.timestamp
    ? formatDate(market.timestamp)
    : "No exchange update";
  setText(
    $("btc-updated"),
    market.timestamp
      ? `${market.fresh ? "Updated" : "Last update (stale)"} ${referenceTime}`
      : referenceTime,
  );
  $("btc-updated").classList.toggle("data-stale", !market.fresh && Boolean(market.timestamp));
  setText(
    $("watch-btc-time"),
    market.timestamp
      ? `${market.fresh ? "Updated" : "Last update (stale)"} ${referenceTime}`
      : referenceTime,
  );
  $("watch-btc-time").classList.toggle("data-stale", !market.fresh && Boolean(market.timestamp));
  setValue("atm-strike", selected?.strike);
  setText(
    $("selected-expiry"),
    selected?.expiry ? formatDate(selected.expiry) : "No contracts selected",
  );
  updateContractOptions(contracts.items, selected);
  setValue("watch-call-symbol", selected?.call);
  setValue("watch-put-symbol", selected?.put);
  setText(
    $("watch-call-quote"),
    `Bid ${display(selected?.call_bid)} / Ask ${display(selected?.call_ask)} · ${selected?.call_trading_status ?? "UNKNOWN"}`,
  );
  setText(
    $("watch-put-quote"),
    `Bid ${display(selected?.put_bid)} / Ask ${display(selected?.put_ask)} · ${selected?.put_trading_status ?? "UNKNOWN"}`,
  );
  const watchSelection = $("watch-selection");
  const selectionText = selected
    ? `${formatDate(selected.expiry)} / ${selected.strike}`
    : unavailable;
  if (watchSelection.dataset.value !== selectionText) {
    watchSelection.dataset.value = selectionText;
    watchSelection.replaceChildren();
    if (selected) {
      const expiryText = document.createElement("span");
      expiryText.className = "date-display";
      expiryText.textContent = formatDate(selected.expiry);
      watchSelection.append(
        expiryText,
        document.createTextNode(` / ${selected.strike}`),
      );
    } else {
      watchSelection.textContent = unavailable;
    }
  }
  const quoteTimestamp = selected?.call_quote_timestamp ?? selected?.put_quote_timestamp;
  setText(
    $("watch-update-time"),
    quoteTimestamp
      ? `Last option update ${formatDate(quoteTimestamp)}`
      : "No option quote update",
  );
}

function renderOrders(orders, trades) {
  const orderFor = (kind) => orders.items.filter((item) => item.option_type === kind).pop();
  setBadge($("call-order-status"), orderFor("CALL")?.status);
  setBadge($("put-order-status"), orderFor("PUT")?.status);
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
}

function renderPositions(positions) {
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
}

function renderRisk(pnl, positions) {
  const quoteCurrency = pnl.quote_currency ? ` ${pnl.quote_currency}` : "";
  setValue("entry-premium", pnl.combined_entry_premium);
  setText(
    $("entry-currency"),
    pnl.quote_currency
      ? `Quote currency: ${pnl.quote_currency}`
      : "Actual filled quantities and multipliers",
  );
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
  setValue("pnl-call-entry", pnl.call_entry_premium, quoteCurrency);
  setValue("pnl-put-entry", pnl.put_entry_premium, quoteCurrency);
  setValue("pnl-entry", pnl.combined_entry_premium, quoteCurrency);
  setValue("pnl-call-buyback", pnl.call_buyback_value, quoteCurrency);
  setValue("pnl-put-buyback", pnl.put_buyback_value, quoteCurrency);
  setValue("pnl-buyback", pnl.current_buyback_value, quoteCurrency);
  setValue("pnl-unrealized", pnl.unrealized_pnl, quoteCurrency);
  setValue("pnl-realized", pnl.realized_pnl, quoteCurrency);
  setValue("exit-reason", pnl.exit_reason);
}

function renderConfig(config) {
  setText($("config-environment"), config.environment);
  setText($("config-enabled"), config.strategy_enabled ? "Yes" : "No");
  setText($("config-quantity"), config.quantity);
  setText($("config-stop"), `${config.stop_loss_percent}%`);
  setText($("config-take"), `${config.take_profit_percent}%`);
}

function renderSnapshot(data, changedType = "snapshot") {
  latestSnapshot = data;
  document.querySelector("main").classList.remove("data-stale-view");
  if (changedType === "snapshot" || changedType === "connection_status") {
    renderConnectionStatus(data.status);
  }
  if (changedType === "snapshot" || changedType === "strategy_state") {
    renderStrategyState(data.status);
  }
  if (changedType === "snapshot" || changedType === "market_data") {
    renderMarket(data);
  }
  if (changedType === "snapshot" || changedType === "order_update") {
    renderOrders(data.orders, data.trades);
  }
  if (changedType === "snapshot" || changedType === "position_update") {
    renderPositions(data.positions);
  }
  if (
    changedType === "snapshot"
    || changedType === "position_update"
    || changedType === "risk_update"
  ) {
    renderRisk(data.pnl, data.positions);
  }
  if (changedType === "snapshot") {
    renderConfig(data.config);
  }
  updateTradingControls();
}

function mergeSection(target, source) {
  return { ...target, ...source };
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasItems(value) {
  return isRecord(value) && Array.isArray(value.items);
}

function handleDashboardMessage(message) {
  if (!message || typeof message !== "object" || typeof message.type !== "string") {
    console.warn("Ignoring malformed dashboard WebSocket message");
    return;
  }
  if (message.type === "snapshot") {
    const data = message.data;
    if (!isRecord(data) || !isRecord(data.status) || !isRecord(data.market)
      || !isRecord(data.instruments) || !isRecord(data.contracts)
      || !hasItems(data.contracts) || !hasItems(data.positions)
      || !hasItems(data.orders) || !hasItems(data.trades)
      || !isRecord(data.pnl) || !isRecord(data.config)) {
      console.warn("Ignoring incomplete dashboard snapshot");
      return;
    }
    renderSnapshot(data);
    return;
  }
  if (!latestSnapshot || !message.data || typeof message.data !== "object") {
    console.warn("Ignoring dashboard update before a valid snapshot");
    return;
  }

  const data = message.data;
  const validUpdate = (() => {
    if (!isRecord(data)) return false;
    if (message.type === "connection_status" || message.type === "strategy_state") {
      return true;
    }
    if (message.type === "market_data") {
      return (!Object.hasOwn(data, "market") || isRecord(data.market))
        && (!Object.hasOwn(data, "instruments") || isRecord(data.instruments))
        && (!Object.hasOwn(data, "contracts") || hasItems(data.contracts))
        && ["market", "instruments", "contracts"].some((key) => Object.hasOwn(data, key));
    }
    if (message.type === "order_update") {
      return ["orders", "trades"].some((key) =>
        Object.hasOwn(data, key) && hasItems(data[key]),
      ) && Object.entries(data).every(([key, value]) =>
        !["orders", "trades"].includes(key) || hasItems(value),
      );
    }
    if (message.type === "position_update") return hasItems(data.positions);
    if (message.type === "risk_update") return isRecord(data.pnl);
    return false;
  })();
  if (!validUpdate) {
    console.warn("Ignoring malformed dashboard update");
    return;
  }

  let updatedSnapshot = latestSnapshot;
  if (message.type === "connection_status" || message.type === "strategy_state") {
    updatedSnapshot = {
      ...updatedSnapshot,
      status: mergeSection(updatedSnapshot.status, data),
    };
  } else if (message.type === "market_data") {
    updatedSnapshot = { ...updatedSnapshot };
    for (const key of ["market", "instruments", "contracts"]) {
      if (data[key]) updatedSnapshot[key] = data[key];
    }
  } else if (message.type === "order_update") {
    updatedSnapshot = { ...updatedSnapshot };
    for (const key of ["orders", "trades"]) {
      if (data[key]) updatedSnapshot[key] = data[key];
    }
  } else if (message.type === "position_update") {
    updatedSnapshot = { ...updatedSnapshot, positions: data.positions };
  } else if (message.type === "risk_update") {
    updatedSnapshot = { ...updatedSnapshot, pnl: data.pnl };
  } else {
    console.warn(`Ignoring unknown dashboard WebSocket message type: ${message.type}`);
    return;
  }
  renderSnapshot(updatedSnapshot, message.type);
}

function setLiveConnectionStatus(status) {
  setText($("last-refresh"), status);
}

function connectDashboardSocket() {
  if (
    dashboardSocket
    && (dashboardSocket.readyState === WebSocket.CONNECTING
      || dashboardSocket.readyState === WebSocket.OPEN)
  ) {
    return;
  }
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  setLiveConnectionStatus("Connecting to live updates…");
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${scheme}//${window.location.host}/ws/dashboard`);
  dashboardSocket = socket;
  socket.addEventListener("open", () => {
    if (dashboardSocket !== socket) return;
    reconnectDelay = 1000;
    setLiveConnectionStatus("Live updates connected");
  });
  socket.addEventListener("message", (event) => {
    try {
      handleDashboardMessage(JSON.parse(event.data));
    } catch (error) {
      console.warn("Ignoring invalid dashboard WebSocket message", error);
    }
  });
  socket.addEventListener("error", () => {
    if (dashboardSocket === socket) {
      setLiveConnectionStatus("Live updates disconnected");
    }
  });
  socket.addEventListener("close", () => {
    if (dashboardSocket !== socket) return;
    dashboardSocket = null;
    setLiveConnectionStatus("Live updates reconnecting…");
    reconnectTimer = window.setTimeout(connectDashboardSocket, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
  });
}

async function loadInitialDashboard() {
  try {
    renderSnapshot(await getJSON("/api/dashboard"));
  } catch (error) {
    $("connection-indicator").className = "indicator indicator-off";
    setText($("connection-label"), "Status unavailable");
    setBadge($("market-data-status"), "DATA UNAVAILABLE");
    setText(
      $("notice"),
      `Dashboard initial load failed: ${error.message}. Waiting for live snapshot.`,
    );
    $("notice").classList.add("notice-error");
    document.querySelector("main").classList.add("data-stale-view");
    latestSnapshot = null;
    updateTradingControls();
  }
  connectDashboardSocket();
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
  } catch (error) {
    $("trade-message").textContent = error.message;
    $("trade-message").classList.add("form-message-error");
  }
}

function orderDescription(action, selected, positions, orderType, limitPrices) {
  const isEntry = action === "OPEN_STRADDLE";
  let kinds = isEntry || action === "CLOSE_BOTH"
    ? ["CALL", "PUT"]
    : [action.endsWith("CALL") ? "CALL" : "PUT"];
  if (!isEntry && action === "CLOSE_BOTH") {
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
    quantity: action === "OPEN_STRADDLE" ? $("trade-quantity").value : null,
    limit_price_call: orderType === "LIMIT" ? limitPrices.CALL : null,
    limit_price_put: orderType === "LIMIT" ? limitPrices.PUT : null,
  };
  try {
    const result = await postJSON("/api/trading/order", payload);
    $("trade-message").textContent = `Engine accepted ${result.orders.map((item) => item.instrument).join(", ")} for submission. This is not a fill confirmation; check Order History and Open Positions.`;
  } catch (error) {
    $("trade-message").textContent = `${error.message} Do not retry until you check Order History.`;
    $("trade-message").classList.add("form-message-error");
  } finally {
    commandPending = false;
    updateTradingControls();
  }
}

$("contract-expiry").addEventListener("change", () => changeContractSelection(true));
$("contract-strike").addEventListener("change", () => changeContractSelection(false));
$("auto-atm").addEventListener("click", () => changeContractSelection(true));
$("trade-order-type").addEventListener("change", updateTradingControls);
document.querySelectorAll(".trade-action").forEach((button) => {
  button.addEventListener("click", () => submitManualOrder(button.dataset.action));
});
$("positions-table").addEventListener("click", (event) => {
  const button = event.target.closest("[data-close-action]");
  if (button) {
    updateTradingControls();
    submitManualOrder(button.dataset.closeAction);
  }
});

loadInitialDashboard();
