const API = (path) => fetch(path).then((r) => r.json());
const APIWithTimeout = async (path, timeoutMs = 3500) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, { signal: controller.signal });
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
};

const PLOT_LAYOUT = (extra = {}) => ({
  paper_bgcolor: "transparent",
  plot_bgcolor: "transparent",
  font: { color: "#edf3ff", size: 12, family: "SF Pro Display, Segoe UI, system-ui, sans-serif" },
  xaxis: { gridcolor: "rgba(124,155,197,0.12)", linecolor: "rgba(124,155,197,0.22)", zeroline: false },
  yaxis: { gridcolor: "rgba(124,155,197,0.12)", linecolor: "rgba(124,155,197,0.22)", zeroline: false },
  margin: { l: 56, r: 20, t: 20, b: 52 },
  hovermode: "x unified",
  ...extra,
});

const PLOT_CONFIG = { displayModeBar: false, responsive: true };

let liveData = null;
let accountData = null;
let decisionData = null;
let tradeSummary = null;
let navHistory = null;
let billsData = null;
let activeStrategy = "all";

const $ = (id) => document.getElementById(id);
const fmt = (n, d = 2) => (Number(n) || 0).toLocaleString("en-US", {
  minimumFractionDigits: d,
  maximumFractionDigits: d,
});
const fmtSigned = (n, d = 2) => {
  const v = Number(n) || 0;
  return `${v >= 0 ? "+" : ""}${fmt(v, d)}`;
};
const clsOf = (n) => Number(n) > 0 ? "positive" : Number(n) < 0 ? "negative" : "neutral";
const hasPlotly = () => typeof window !== "undefined" && typeof window.Plotly !== "undefined";

function toSGT(isoOrStr) {
  if (!isoOrStr) return "--";
  const d = new Date(isoOrStr);
  if (Number.isNaN(d.getTime())) return String(isoOrStr);
  return d.toLocaleString("en-SG", {
    timeZone: "Asia/Singapore",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function updateBadge(alive, pid) {
  $("dot").className = alive ? "dot" : "dot dead";
  $("engine-label").textContent = alive ? `running · pid ${pid || "--"}` : "stopped";
}

function makeTabs(portfolios) {
  const el = $("strategy-tabs");
  const tabs = ["all", ...portfolios];
  el.innerHTML = tabs.map((p) => {
    const label = p === "all" ? "All Strategies" : p;
    return `<button class="tab-btn ${activeStrategy === p ? "active" : ""}" data-p="${p}">${label}</button>`;
  }).join("");

  el.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.onclick = () => {
      activeStrategy = btn.dataset.p;
      makeTabs(portfolios);
      render();
    };
  });
}

function renderStats() {
  const summary = liveData?.summary || {};
  const portfolios = summary.portfolios || {};
  const snap = activeStrategy === "all" ? null : portfolios[activeStrategy];
  const account = accountData?.balance_view || {};
  const custom = accountData?.custom_strategies?.elite_flow || {};

  const stats = [
    {
      label: "Strategy NAV",
      value: `$${fmt(snap?.nav ?? summary.total_nav)}`,
      sub: `capital $${fmt(snap?.capital ?? summary.total_capital)}`,
      cls: "",
    },
    {
      label: "ROI",
      value: `${fmtSigned(snap?.pnl_pct ?? summary.total_pnl_pct)}%`,
      sub: `fees $${fmt(snap?.total_fees ?? Object.values(portfolios).reduce((acc, p) => acc + (Number(p.total_fees) || 0), 0))}`,
      cls: clsOf(snap?.pnl_pct ?? summary.total_pnl_pct),
    },
    {
      label: "Net PnL",
      value: `$${fmtSigned(snap?.pnl ?? summary.total_pnl)}`,
      sub: snap?.pos_state ? `state ${snap.pos_state}` : "summary view",
      cls: clsOf(snap?.pnl ?? summary.total_pnl),
    },
    {
      label: "Account Equity",
      value: `$${fmt(account.total_eq)}`,
      sub: `available $${fmt(account.usdt_avail)}`,
      cls: "",
    },
    {
      label: "Open Positions",
      value: `${snap?.n_positions ?? accountData?.positions_view?.length ?? 0}`,
      sub: custom.last_line ? custom.last_line.slice(0, 52) : "no recent custom log line",
      cls: "",
    },
    {
      label: "Last Update",
      value: toSGT(summary.updated_at),
      sub: `engine ${summary.engine_status || "--"}`,
      cls: "",
    },
  ];

  $("top-stats").innerHTML = stats.map((item) => `
    <div class="stat-card">
      <div class="stat-label">${item.label}</div>
      <div class="stat-value ${item.cls}">${item.value}</div>
      <div class="stat-subvalue">${item.sub}</div>
    </div>
  `).join("");
}

function renderNAVChart() {
  const summary = liveData?.summary || {};
  const rows = navHistory || [];
  $("nav-chart-title").textContent = activeStrategy === "all" ? "All strategy NAV" : `${activeStrategy} NAV`;

  if (!rows.length) {
    $("chart-nav").innerHTML = '<div class="muted">No NAV history yet.</div>';
    return;
  }

  const timestamps = rows.map((r) => r.ts);
  const traces = [];

  if (activeStrategy === "all") {
    traces.push({
      x: timestamps,
      y: rows.map((r) => Number(r.total) || 0),
      type: "scatter",
      mode: "lines",
      name: "Total NAV",
      line: { color: "#7ce6ff", width: 3 },
      fill: "tozeroy",
      fillcolor: "rgba(124,230,255,0.08)",
    });
    const keys = Object.keys(rows[0]).filter((k) => !["ts", "total"].includes(k));
    keys.forEach((key, i) => {
      traces.push({
        x: timestamps,
        y: rows.map((r) => Number(r[key]) || 0),
        type: "scatter",
        mode: "lines",
        name: key,
        line: { width: 1.6, color: ["#4dd9a6", "#6ab6ff", "#ffd166", "#ff7b72"][i % 4] },
      });
    });
  } else {
    traces.push({
      x: timestamps,
      y: rows.map((r) => Number(r[activeStrategy]) || 0),
      type: "scatter",
      mode: "lines",
      name: activeStrategy,
      line: { color: "#4dd9a6", width: 2.4 },
      fill: "tozeroy",
      fillcolor: "rgba(77,217,166,0.10)",
    });
  }

  if (hasPlotly()) {
    Plotly.react("chart-nav", traces, PLOT_LAYOUT({ height: 380, yaxis: { title: "NAV ($)" } }), PLOT_CONFIG);
    return;
  }

  const series = traces[0]?.y || [];
  if (!series.length) {
    $("chart-nav").innerHTML = '<div class="muted">No NAV series available.</div>';
    return;
  }

  const min = Math.min(...series);
  const max = Math.max(...series);
  const width = 920;
  const height = 320;
  const pad = 24;
  const span = Math.max(max - min, 1e-9);
  const points = series.map((v, i) => {
    const x = pad + (i / Math.max(series.length - 1, 1)) * (width - pad * 2);
    const y = height - pad - ((v - min) / span) * (height - pad * 2);
    return `${x},${y}`;
  }).join(" ");

  const first = Number(series[0]) || 0;
  const last = Number(series[series.length - 1]) || 0;
  const change = last - first;
  $("chart-nav").innerHTML = `
    <div class="fallback-chart-meta">
      <span class="pill">Fallback chart mode</span>
      <span class="${clsOf(change)}">change ${fmtSigned(change)}</span>
      <span class="muted">range $${fmt(min)} - $${fmt(max)}</span>
    </div>
    <svg viewBox="0 0 ${width} ${height}" class="fallback-chart" preserveAspectRatio="none" aria-label="NAV chart">
      <defs>
        <linearGradient id="navFill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stop-color="rgba(77,217,166,0.35)"></stop>
          <stop offset="100%" stop-color="rgba(77,217,166,0.02)"></stop>
        </linearGradient>
      </defs>
      <polyline points="${points}" fill="none" stroke="#7ce6ff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></polyline>
    </svg>
  `;
}

function signalCard(symbol, signal, position) {
  if (!signal && !position) {
    return `<div class="signal-card"><div class="signal-title">${symbol}</div><div class="signal-detail">No recent signal.</div></div>`;
  }

  const side = position?.side || (signal?.conviction >= 0 ? "long bias" : "short bias");
  const stateText = signal ? `${signal.from_state} -> ${signal.to_state}` : (position?.state || "--");
  const pnlText = position?.pnl_pct != null ? `${fmtSigned(position.pnl_pct)}%` : "--";

  return `
    <div class="signal-card">
      <div class="signal-top">
        <div>
          <div class="signal-title">${symbol}</div>
          <div class="signal-meta">${stateText}</div>
        </div>
        <span class="badge ${String(side).includes("short") ? "badge-red" : "badge-green"}">${String(side).toUpperCase()}</span>
      </div>
      <div class="signal-metrics">
        <div class="mini-metric"><div class="mini-label">Conviction</div><div class="mini-value ${clsOf(signal?.conviction)}">${signal ? fmtSigned(signal.conviction, 3) : "--"}</div></div>
        <div class="mini-metric"><div class="mini-label">Flow</div><div class="mini-value ${clsOf(signal?.flow)}">${signal ? fmtSigned(signal.flow, 3) : "--"}</div></div>
        <div class="mini-metric"><div class="mini-label">Regime</div><div class="mini-value ${clsOf(signal?.regime)}">${signal ? fmtSigned(signal.regime, 3) : "--"}</div></div>
        <div class="mini-metric"><div class="mini-label">Held / PnL</div><div class="mini-value ${clsOf(position?.pnl_pct)}">${position ? `${position.held_min}m / ${pnlText}` : "--"}</div></div>
      </div>
      <div class="signal-detail">${signal?.detail || position?.detail || "No trigger detail available."}</div>
      <div class="signal-meta small" style="margin-top:10px">last update ${toSGT(position?.ts || signal?.ts)}${position?.leverage ? ` · leverage ${position.leverage}x` : ""}</div>
    </div>
  `;
}

function renderSnapshot() {
  const signals = decisionData?.latest_signal_by_symbol || {};
  const positions = decisionData?.latest_position_by_symbol || {};
  const symbols = Array.from(new Set([...Object.keys(signals), ...Object.keys(positions)])).sort();
  $("snapshot-updated").textContent = toSGT(liveData?.summary?.updated_at);

  if (!symbols.length) {
    $("signal-grid").innerHTML = '<div class="muted">No parsed signal snapshots yet.</div>';
    return;
  }

  $("signal-grid").innerHTML = symbols.map((symbol) => signalCard(symbol, signals[symbol], positions[symbol])).join("");
}

function renderDecisionJournal() {
  let events = decisionData?.events || [];
  if (activeStrategy !== "all") {
    events = events.filter((e) => !e.portfolio_id || e.portfolio_id === activeStrategy || activeStrategy === "elite_flow");
  }

  if (!events.length) {
    $("decision-tbody").innerHTML = '<tr><td colspan="7" class="muted">No decision events parsed yet.</td></tr>';
    return;
  }

  $("decision-tbody").innerHTML = events.slice().reverse().map((e) => {
    const side = e.side ? String(e.side).toUpperCase() : "--";
    const pnlState = e.pnl_pct != null ? `PnL ${fmtSigned(e.pnl_pct)}% · held ${e.held_min || 0}m` : (e.title || "--");
    const eventBadge = {
      signal: "badge-yellow",
      entry: "badge-green",
      exit: "badge-red",
      config: "badge-blue",
      position_update: "badge-blue",
    }[e.type] || "badge-blue";

    return `
      <tr>
        <td class="nowrap small">${toSGT(e.ts)}</td>
        <td><span class="badge ${eventBadge}">${e.type}</span></td>
        <td><strong>${e.symbol || "--"}</strong></td>
        <td>${side}</td>
        <td>${e.leverage ? `${e.leverage}x` : "--"}</td>
        <td>${pnlState}</td>
        <td class="small">${e.detail || "--"}</td>
      </tr>
    `;
  }).join("");
}

function renderTradeStats() {
  const metrics = tradeSummary?.metrics || {};
  const stats = [
    ["Closed trades", metrics.closed_trades ?? 0],
    ["Win rate", `${fmt(metrics.win_rate_pct ?? 0, 1)}%`],
    ["Avg trade", `${fmtSigned(metrics.avg_pnl_pct ?? 0, 3)}%`],
    ["Best / Worst", `${fmtSigned(metrics.best_pnl_pct ?? 0, 2)}% / ${fmtSigned(metrics.worst_pnl_pct ?? 0, 2)}%`],
    ["Avg hold", `${fmt(metrics.avg_hold_min ?? 0, 1)} min`],
    ["Realized / Fees", `${fmtSigned(metrics.gross_realized_pnl ?? 0, 4)} / ${fmt(metrics.total_fees ?? 0, 4)}`],
  ];
  $("trade-stats").innerHTML = stats.map(([label, value]) => `
    <div class="stat-card">
      <div class="stat-label">${label}</div>
      <div class="stat-value" style="font-size:22px">${value}</div>
    </div>
  `).join("");
}

function renderTradeCards() {
  const trades = tradeSummary?.recent_trades || [];
  if (!trades.length) {
    $("trade-cards").innerHTML = '<div class="muted">No closed trades parsed yet.</div>';
    return;
  }
  $("trade-cards").innerHTML = trades.map((t) => `
    <div class="signal-card">
      <div class="signal-top">
        <div>
          <div class="signal-title">${t.symbol}</div>
          <div class="signal-meta">${(t.side || "--").toUpperCase()} · leverage ${t.leverage || "--"}x · held ${t.held_min || 0}m</div>
        </div>
        <span class="badge ${Number(t.pnl_pct) >= 0 ? "badge-green" : "badge-red"}">${fmtSigned(t.pnl_pct || 0, 2)}%</span>
      </div>
      <div class="signal-detail">${t.trigger || "No trigger detail available."}</div>
      <div class="signal-meta small" style="margin-top:10px">entry ${toSGT(t.entry_ts)} · exit ${toSGT(t.exit_ts)}</div>
      <div class="signal-meta small" style="margin-top:6px">${t.exit_detail || ""}</div>
    </div>
  `).join("");
}

function renderPositions() {
  let positions = accountData?.positions_view || [];
  if (!positions.length) {
    const snapPositions = liveData?.summary?.portfolios?.elite_flow?.positions || {};
    positions = Object.entries(snapPositions).map(([instId, pos]) => ({
      instId,
      side: pos.side,
      contracts: pos.qty,
      avgPx: pos.entry,
      markPx: pos.mark,
      lever: "--",
      upl: pos.upnl,
    }));
  }

  if (!positions.length) {
    $("pos-tbody").innerHTML = '<tr><td colspan="7" class="muted">No open positions.</td></tr>';
    return;
  }

  $("pos-tbody").innerHTML = positions.map((p) => `
    <tr>
      <td><strong>${p.instId}</strong></td>
      <td><span class="badge ${String(p.side).includes("short") ? "badge-red" : "badge-green"}">${String(p.side).toUpperCase()}</span></td>
      <td>${fmt(p.contracts, 4)}</td>
      <td>${fmt(p.avgPx, 2)}</td>
      <td>${fmt(p.markPx, 2)}</td>
      <td>${p.lever ? `${p.lever}x` : "--"}</td>
      <td class="${clsOf(p.upl)}">${fmtSigned(p.upl)}</td>
    </tr>
  `).join("");
}

function renderBills() {
  let bills = billsData || [];
  if (!bills.length) {
    $("bills-tbody").innerHTML = '<tr><td colspan="8" class="muted">No fills yet.</td></tr>';
    return;
  }

  if (activeStrategy !== "all" && activeStrategy === "elite_flow") {
    const allowed = new Set(["BTC-USDT-SWAP", "ETH-USDT-SWAP"]);
    bills = bills.filter((b) => allowed.has(b.instId));
  }

  $("bills-tbody").innerHTML = bills.slice(0, 120).map((b) => `
    <tr>
      <td class="nowrap small">${toSGT(b.time)}</td>
      <td><strong>${b.instId || "--"}</strong></td>
      <td><span class="badge ${b.side === "sell" ? "badge-red" : "badge-green"}">${(b.side || "--").toUpperCase()}</span></td>
      <td>${fmt(b.fillSz, 4)}</td>
      <td>${fmt(b.fillPx, 2)}</td>
      <td>$${fmt(b.notional)}</td>
      <td class="negative">${fmt(Number(b.fee) || 0, 4)}</td>
      <td class="${clsOf(b.pnl)}">${Number(b.pnl) ? fmtSigned(b.pnl, 4) : "--"}</td>
    </tr>
  `).join("");
}

function render() {
  const summary = liveData?.summary || {};
  const portfolios = Object.keys(summary.portfolios || {});
  updateBadge(liveData?.daemon_alive, summary.pid);
  makeTabs(portfolios);
  renderStats();
  renderNAVChart();
  renderSnapshot();
  renderDecisionJournal();
  renderTradeStats();
  renderTradeCards();
  renderPositions();
  renderBills();
}

async function loadAll() {
  const essential = await Promise.allSettled([
    APIWithTimeout("/api/live", 2500),
    APIWithTimeout("/api/nav_history", 2500),
  ]);

  liveData = essential[0].status === "fulfilled" ? essential[0].value : (liveData || { summary: { portfolios: {} }, daemon_alive: false });
  navHistory = essential[1].status === "fulfilled" ? essential[1].value : (navHistory || []);
  render();

  const background = await Promise.allSettled([
    APIWithTimeout("/api/account", 4500),
    APIWithTimeout("/api/decision_journal?limit=120", 4500),
    APIWithTimeout("/api/trade_summary?limit=8", 4500),
    APIWithTimeout("/api/bills", 4500),
  ]);

  accountData = background[0].status === "fulfilled" ? background[0].value : (accountData || { balance_view: {}, positions_view: [], custom_strategies: {} });
  decisionData = background[1].status === "fulfilled" ? background[1].value : (decisionData || { events: [], latest_signal_by_symbol: {}, latest_position_by_symbol: {} });
  tradeSummary = background[2].status === "fulfilled" ? background[2].value : (tradeSummary || { metrics: {}, recent_trades: [], recent_orders: [] });
  billsData = background[3].status === "fulfilled" ? background[3].value : (billsData || []);

  render();
}

loadAll();
setInterval(loadAll, 30000);
