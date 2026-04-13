import { execSync } from "child_process";
import { readFileSync, existsSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORK_DIR = join(__dirname, "engine");
const LOGS_DIR = join(WORK_DIR, "logs");
const CONTROL_DIR = join(WORK_DIR, "control");
const STRATEGIES_FILE = join(WORK_DIR, "config", "strategies.json");

// Production default — dual profile, $5k each
const DEFAULT_CONFIG = [
  { id: "daily_combined",  strategy: "combined_portfolio", profile: "daily",  capital: 5000 },
  { id: "hourly_combined", strategy: "combined_portfolio", profile: "hourly", capital: 5000 },
];

function run(cmd: string, timeout = 300_000): string {
  try {
    return execSync(cmd, {
      cwd: WORK_DIR,
      timeout,
      encoding: "utf-8",
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    }).trim();
  } catch (err: any) {
    return `Error: ${err.stderr || err.message}`.slice(0, 3000);
  }
}

function readFile(path: string): string | null {
  try {
    if (existsSync(path)) {
      return readFileSync(path, "utf-8");
    }
  } catch {}
  return null;
}

function isRunning(): { running: boolean; pid?: number } {
  const pidFile = join(CONTROL_DIR, "trading.pid");
  const pidRaw = readFile(pidFile);
  if (!pidRaw) return { running: false };
  const pid = parseInt(pidRaw.trim());
  try {
    process.kill(pid, 0);
    return { running: true, pid };
  } catch {
    return { running: false };
  }
}

// ---------------------------------------------------------------------------
// start_trading
// ---------------------------------------------------------------------------

async function executeStartTrading(
  _toolCallId: string,
  params: {
    portfolios?: Array<{ id: string; strategy: string; profile?: string; capital: number }>;
    preset?: string;
    paper?: boolean;
  }
) {
  const { running, pid } = isRunning();
  if (running) {
    return {
      content: [{
        type: "text" as const,
        text: `Trading engine is already running (PID=${pid}). Use trading_status to check, or stop_trading to restart.`,
      }],
    };
  }

  // Resolve config from preset name, explicit portfolios, or default
  let config = DEFAULT_CONFIG;

  if (params.preset) {
    const raw = readFile(STRATEGIES_FILE);
    if (raw) {
      const data = JSON.parse(raw);
      const preset = (data.portfolio_presets || {})[params.preset];
      if (preset) {
        config = preset.config;
      } else {
        const available = Object.keys(data.portfolio_presets || {}).join(", ");
        return {
          content: [{
            type: "text" as const,
            text: `Unknown preset '${params.preset}'. Available: ${available}`,
          }],
        };
      }
    }
  } else if (params.portfolios && params.portfolios.length > 0) {
    config = params.portfolios.map((p) => ({
      id: p.id,
      strategy: p.strategy,
      profile: p.profile || "daily",
      capital: p.capital,
    }));
  }

  const paperFlag = params.paper ? " --paper" : "";
  const configJson = JSON.stringify(config);
  const output = run(`python3 main.py start${paperFlag} --config '${configJson}'`, 30_000);

  const startup = readFile(join(CONTROL_DIR, "startup.txt"));
  const text = startup || output || "Trading daemon started.";
  return { content: [{ type: "text" as const, text }] };
}

// ---------------------------------------------------------------------------
// stop_trading
// ---------------------------------------------------------------------------

async function executeStopTrading() {
  const output = run("python3 main.py stop", 60_000);
  return { content: [{ type: "text" as const, text: output }] };
}

// ---------------------------------------------------------------------------
// restart_trading — stop then start with production defaults
// ---------------------------------------------------------------------------

async function executeRestartTrading(
  _toolCallId: string,
  params: { preset?: string; paper?: boolean }
) {
  const { running } = isRunning();
  if (running) {
    run("python3 main.py stop", 60_000);
  }
  // Brief pause for graceful shutdown
  await new Promise((r) => setTimeout(r, 3000));
  return executeStartTrading(_toolCallId, params);
}

// ---------------------------------------------------------------------------
// trading_status — reads logs (no Python)
// ---------------------------------------------------------------------------

async function executeTradingStatus() {
  const heartbeatPath = join(LOGS_DIR, "heartbeat.json");
  const summaryPath = join(LOGS_DIR, "summary.json");
  const rawHb = readFile(heartbeatPath);
  const rawSummary = readFile(summaryPath);

  if (!rawHb && !rawSummary) {
    return {
      content: [{
        type: "text" as const,
        text: "No trading data available. Engine may not have started yet.",
      }],
    };
  }

  if (rawHb) {
    return formatHeartbeatStatus(JSON.parse(rawHb));
  }
  return formatHeartbeatFromSummary(JSON.parse(rawSummary));
}

function formatHeartbeatStatus(hb: any) {
  const lines = [
    "=".repeat(60),
    `TRADING STATUS — ${hb.heartbeat_at || "?"}`,
    `Engine: ${hb.engine_health || "?"}  |  PID: ${hb.daemon_pid || "?"}  |  Alive: ${hb.daemon_alive}`,
    "=".repeat(60),
  ];

  for (const [name, p] of Object.entries(hb.portfolios || {}) as [string, any][]) {
    lines.push(`\n${"─".repeat(60)}`);
    lines.push(`[${name}] ${p.strategy || p.strategy_id}`);
    lines.push(`  Profile: ${p.profile || p.profile_id}`);
    lines.push(`  Direction: ${p.direction}  |  Status: ${(p.status || "?").toUpperCase()}`);
    lines.push(`  Capital: $${fmt(p.initial_capital)}  →  NAV: $${fmt(p.nav)}`);
    lines.push(`  Total PnL: $${fmtSigned(p.pnl)} (${fmtSigned(p.pnl_pct)}%)`);
    lines.push(`  Realized: $${fmtSigned(p.realized_pnl)}  |  Unrealized: $${fmtSigned(p.unrealized_pnl)}`);
    lines.push(`  Fees: $${fmt(p.total_fees)}  |  Drawdown: ${(p.drawdown_pct || 0).toFixed(1)}% (peak $${fmt(p.peak_nav)})`);
    lines.push(`  Exposure: long=$${fmt(p.long_exposure)} short=$${fmt(p.short_exposure)} net=$${fmtSigned(p.net_exposure)}`);
    lines.push(`  Positions: ${p.n_positions} (${p.n_longs} long, ${p.n_shorts} short)`);
    lines.push(`  Risk: CB=${p.risk_cb}  Vol=${p.risk_vol}`);

    if (p.top_longs?.length > 0) {
      lines.push("  Top Longs:");
      for (const pos of p.top_longs) {
        lines.push(`    ${pos.symbol.padEnd(12)}  $${fmt(pos.notional).padStart(8)}  (${pos.weight_pct.toFixed(1)}%)  uPnL=$${fmtSigned(pos.upnl)}`);
      }
    }
    if (p.top_shorts?.length > 0) {
      lines.push("  Top Shorts:");
      for (const pos of p.top_shorts) {
        lines.push(`    ${pos.symbol.padEnd(12)}  $${fmt(pos.notional).padStart(8)}  (${pos.weight_pct.toFixed(1)}%)  uPnL=$${fmtSigned(pos.upnl)}`);
      }
    }
    lines.push(`  Last Rebalance: ${p.last_rebalance || "Never"}`);
  }

  lines.push(`\n${"=".repeat(60)}`);
  lines.push(`TOTAL: NAV=$${fmt(hb.total_nav)}  PnL=$${fmtSigned(hb.total_pnl)} (${fmtSigned(hb.total_pnl_pct)}%)  Capital=$${fmt(hb.total_capital)}`);
  lines.push("=".repeat(60));

  return { content: [{ type: "text" as const, text: lines.join("\n") }] };
}

function formatHeartbeatFromSummary(summary: any) {
  const lines = [
    "=".repeat(55),
    `TRADING STATUS — ${summary.updated_at || "?"}`,
    `Engine: ${(summary.engine_status || "unknown").toUpperCase()} (PID=${summary.pid || "?"})`,
    "=".repeat(55),
  ];

  for (const [name, snap] of Object.entries(summary.portfolios || {}) as [string, any][]) {
    lines.push(`\n--- [${name}] ${snap.strategy_id || "?"} (${snap.profile || "?"}) ---`);
    lines.push(`  NAV: $${fmt(snap.nav)}  (capital: $${fmt(snap.capital)})`);
    lines.push(`  PnL: $${fmtSigned(snap.pnl)} (${fmtSigned(snap.pnl_pct)}%)`);
    lines.push(`  Realized: $${fmtSigned(snap.realized_pnl)}  |  Unrealized: $${fmtSigned(snap.upnl)}`);
    lines.push(`  Drawdown: ${(snap.drawdown_pct || 0).toFixed(1)}%  |  Positions: ${snap.n_positions || 0}`);
    if (snap.risk) {
      lines.push(`  Risk: CB=${snap.risk.cb}  Vol=${snap.risk.vol}`);
    }
  }

  lines.push(`\nTOTAL: NAV=$${fmt(summary.total_nav)} PnL=$${fmtSigned(summary.total_pnl)} (${fmtSigned(summary.total_pnl_pct)}%)`);
  lines.push("=".repeat(55));
  return { content: [{ type: "text" as const, text: lines.join("\n") }] };
}

// ---------------------------------------------------------------------------
// list_strategies — reads strategies.json + presets (no Python)
// ---------------------------------------------------------------------------

async function executeListStrategies() {
  const raw = readFile(STRATEGIES_FILE);
  if (!raw) {
    return { content: [{ type: "text" as const, text: "Error: strategies.json not found" }] };
  }

  const data = JSON.parse(raw);
  const strategies = data.strategies || [];
  const presets = data.portfolio_presets || {};

  const lines = [
    "=".repeat(55),
    "AVAILABLE STRATEGIES",
    "=".repeat(55),
  ];

  for (let i = 0; i < strategies.length; i++) {
    const s = strategies[i];
    lines.push(`\n  ${i + 1}. ${s.name}`);
    lines.push(`     ID: ${s.id}`);
    lines.push(`     Profiles: ${(s.profiles || []).join(", ")}`);
    if (s.description) lines.push(`     ${s.description}`);
  }

  if (Object.keys(presets).length > 0) {
    lines.push("", "=".repeat(55), "PORTFOLIO PRESETS", "=".repeat(55));
    for (const [name, preset] of Object.entries(presets) as [string, any][]) {
      lines.push(`\n  ${name}`);
      lines.push(`     ${preset.description || ""}`);
    }
  }

  lines.push("", "=".repeat(55));
  return { content: [{ type: "text" as const, text: lines.join("\n") }] };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(n: number | undefined): string {
  return (n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtSigned(n: number | undefined): string {
  const v = n || 0;
  return (v >= 0 ? "+" : "") + v.toFixed(2);
}

// ---------------------------------------------------------------------------
// Plugin registration
// ---------------------------------------------------------------------------

const tradingPlugin = {
  id: "okx-quant",
  name: "Quant Trading Bot",
  description: "Multi-portfolio crypto trading engine on OKX (Agent Trade Kit)",

  register(api: any) {

    api.registerTool({
      name: "start_trading",
      label: "Start Trading",
      description:
        `Start the trading engine daemon. ` +
        `Triggers: "start trading", "start the engine", "launch trading", "run the bot", ` +
        `"kick off trading", "get trading going", "begin trading". ` +
        `Defaults to production dual-profile config ($5k daily + $5k hourly combined) — ` +
        `do NOT ask the user for strategy/capital unless they explicitly specify something different. ` +
        `Optional params: preset (named config), portfolios (custom array), paper (paper trading mode). ` +
        `OKX demo account by default — set LIVE_TRADING=true with real keys for live trading.`,
      parameters: {
        type: "object",
        properties: {
          portfolios: {
            type: "array",
            description: "Custom portfolio array (omit to use production defaults)",
            items: {
              type: "object",
              properties: {
                id: { type: "string" },
                strategy: { type: "string" },
                profile: { type: "string", enum: ["daily", "hourly"] },
                capital: { type: "number" },
              },
              required: ["id", "strategy", "capital"],
            },
          },
          preset: {
            type: "string",
            description: "Named preset from strategies.json (e.g. 'multi_strategy', 'research_small')",
          },
          paper: {
            type: "boolean",
            description: "Paper trading mode: real prices, no real orders",
          },
        },
        required: [],
      },
      execute: executeStartTrading,
    } as any);

    api.registerTool({
      name: "stop_trading",
      label: "Stop Trading",
      description:
        `Stop the trading engine gracefully. State is persisted for restart. ` +
        `Triggers: "stop trading", "kill the engine", "stop the bot", "halt trading", ` +
        `"shut down trading", "turn off trading", "pause trading".`,
      parameters: { type: "object", properties: {}, required: [] },
      execute: executeStopTrading,
    } as any);

    api.registerTool({
      name: "restart_trading",
      label: "Restart Trading",
      description:
        `Stop then restart the trading engine with production defaults. ` +
        `Triggers: "restart trading", "restart the engine", "reboot trading", ` +
        `"restart the bot", "cycle the engine". ` +
        `Optional: preset, paper mode.`,
      parameters: {
        type: "object",
        properties: {
          preset: { type: "string", description: "Named preset (default: production dual-profile)" },
          paper: { type: "boolean", description: "Paper trading mode" },
        },
        required: [],
      },
      execute: executeRestartTrading,
    } as any);

    api.registerTool({
      name: "trading_status",
      label: "Trading Status",
      description:
        `Show current trading status: NAV, PnL, open positions, risk state, last rebalance. ` +
        `Reads log files only — instant, no Python. ` +
        `Triggers: "status", "trading status", "how are my positions", "portfolio status", ` +
        `"how is trading going", "trading update", "check positions", "what's my PnL", ` +
        `"how much am I up/down", "show positions", "portfolio performance", "what's the NAV", ` +
        `"monitor", "check trading", "are we making money".`,
      parameters: { type: "object", properties: {}, required: [] },
      execute: executeTradingStatus,
    } as any);

    api.registerTool({
      name: "list_strategies",
      label: "List Strategies",
      description:
        `List available trading strategies, profiles, and portfolio presets. ` +
        `Triggers: "list strategies", "what strategies", "show presets", "what portfolios can I run", ` +
        `"show me the trading options". Also useful before calling start_trading with a custom config.`,
      parameters: { type: "object", properties: {}, required: [] },
      execute: executeListStrategies,
    } as any);
  },
};

export default tradingPlugin;
