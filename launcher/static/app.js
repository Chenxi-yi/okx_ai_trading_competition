const state = {
  env: localStorage.getItem('launcher.env') || 'personal',
  mode: localStorage.getItem('launcher.mode') || 'paper',
  strategy: localStorage.getItem('launcher.strategy') || 'core_c_auto_h24_regression_v1',
  port: Number(localStorage.getItem('launcher.port') || 8080),
};

let launchOptions = { strategies: [] };
let cAutoPaperAvailable = false;

const $ = (id) => document.getElementById(id);

function setActive(groupId, value) {
  document.querySelectorAll(`#${groupId} [data-value]`).forEach((node) => {
    node.classList.toggle('active', node.dataset.value === value);
  });
}

function yoloUrl() {
  return `http://127.0.0.1:${state.port}/yolo`;
}

function dashboardUrl() {
  return `http://127.0.0.1:${state.port}/`;
}

function applySelection() {
  setActive('modeGroup', state.mode);
  $('envSelect').value = state.env;
  $('strategySelect').value = state.strategy;
  renderSelectedStrategyMeta();
  $('portInput').value = state.port;
  $('realConfirmBox').classList.toggle('hidden', state.mode !== 'real');
  $('competitionConfirmLine').classList.toggle('hidden', !(state.mode === 'real' && state.env === 'competition'));
  $('openYolo').href = yoloUrl();
  if ($('yoloFrame').src === 'about:blank' || $('yoloFrame').dataset.port !== String(state.port)) {
    $('yoloFrame').src = yoloUrl();
    $('yoloFrame').dataset.port = String(state.port);
  }
  localStorage.setItem('launcher.env', state.env);
  localStorage.setItem('launcher.mode', state.mode);
  localStorage.setItem('launcher.strategy', state.strategy);
  localStorage.setItem('launcher.port', String(state.port));
}

function renderStrategyOptions(data) {
  launchOptions = data || { strategies: [] };
  const strategies = launchOptions.strategies || [];
  if (!strategies.some((item) => item.strategy_id === state.strategy)) {
    state.strategy = launchOptions.primary_strategy_id || strategies[0]?.strategy_id || state.strategy;
  }
  $('strategySelect').innerHTML = strategies.map((item) => {
    const caps = [
      item.paper_supported ? 'paper' : null,
      item.real_supported ? 'real' : null,
    ].filter(Boolean).join('+') || 'view only';
    const label = `${item.name || item.strategy_id} (${item.book || '-'} / ${item.status || '-'} / ${caps})`;
    return `<option value="${escapeAttr(item.strategy_id)}">${escapeHtml(label)}</option>`;
  }).join('');
  applySelection();
}

function strategyMeta(strategyId = state.strategy) {
  return (launchOptions.strategies || []).find((item) => item.strategy_id === strategyId) || null;
}

function renderSelectedStrategyMeta() {
  const item = strategyMeta();
  if (!item) {
    $('strategyMeta').textContent = '未加载策略元数据';
    return;
  }
  const caps = [
    item.kind || 'strategy',
    item.book || '-',
    item.status || '-',
    item.paper_supported ? 'paper ok' : 'no paper',
    item.real_supported ? 'real ok' : 'real locked',
  ].join(' / ');
  $('strategyMeta').innerHTML = `
    <strong>${escapeHtml(item.name || item.strategy_id)}</strong>
    <span>${escapeHtml(caps)}</span>
    <small>${escapeHtml(item.description || '')}</small>
  `;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function escapeAttr(value) {
  return escapeHtml(value);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = await res.json();
  if (!res.ok || body.ok === false) {
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return body;
}

function formatMoney(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return num.toFixed(2);
}

function summaryNav(summary) {
  if (!summary) return null;
  if (typeof summary.nav === 'number') return summary.nav;
  const portfolios = summary.portfolios || {};
  const first = Object.values(portfolios)[0];
  return first && typeof first.nav === 'number' ? first.nav : null;
}

function summaryPnl(summary) {
  if (!summary) return null;
  if (typeof summary.pnl_pct === 'number') return summary.pnl_pct;
  const portfolios = summary.portfolios || {};
  const first = Object.values(portfolios)[0];
  return first && typeof first.pnl_pct === 'number' ? first.pnl_pct : null;
}

function renderStatus(data) {
  $('rootPath').textContent = data.root || '';
  $('launcherStatus').textContent = 'ready';
  $('updatedAt').textContent = new Date().toLocaleTimeString();

  const dashboard = data.pids?.dashboard || {};
  const strategies = data.pids?.strategies || [];
  const running = strategies.filter((item) => item.alive);
  $('dashboardState').textContent = dashboard.alive ? `running #${dashboard.pid}` : 'stopped';
  $('strategyState').textContent = running.length ? `${running.length} running` : 'stopped';
  const pro = data.pro_paper || {};
  const cauto = data.c_auto_v2_paper || {};
  cAutoPaperAvailable = Boolean(cauto.available);
  const scheduler = pro.scheduler || {};
  $('proPaperState').textContent = pro.running ? `running #${(pro.processes || [])[0]?.pid || '-'}` : (pro.available ? (scheduler.scheduler_status || 'idle') : 'idle');
  $('proPaperCycles').textContent = scheduler.cycles === undefined ? '--' : String(scheduler.cycles);
  if (cAutoPaperAvailable) renderPaperPanel(cauto);

  const nav = summaryNav(data.summary);
  const pnl = summaryPnl(data.summary);
  $('navState').textContent = nav === null ? '--' : formatMoney(nav);
  $('pnlState').textContent = pnl === null ? '--' : `${Number(pnl).toFixed(2)}%`;

  const list = $('runningList');
  const proRows = (data.pids?.pro_paper || []).map((item) => `
    <div class="run-row">
      <span>${item.strategy_id || 'pro_paper'} / ${item.environment || 'personal'} / paper / pid ${item.pid}</span>
      <b>alive</b>
    </div>
  `);
  const cautoRows = (data.pids?.c_auto_v2_paper || []).map((item) => `
    <div class="run-row">
      <span>c-auto-v2 / ${item.environment || 'personal'} / paper / pid ${item.pid}</span>
      <b>alive</b>
    </div>
  `);
  if (!strategies.length && !proRows.length && !cautoRows.length) {
    list.innerHTML = '<div class="run-row stale"><span>暂无策略 pid 文件</span><b>idle</b></div>';
  } else {
    list.innerHTML = [
      ...cautoRows,
      ...proRows,
      ...strategies.map((item) => `
      <div class="run-row ${item.alive ? '' : 'stale'}">
        <span>${item.strategy} / ${item.env} / pid ${item.pid}</span>
        <b>${item.alive ? 'alive' : 'stale'}</b>
      </div>
    `),
    ].join('');
  }

  const latestLog = (data.launcher_logs || [])[0];
  $('logTail').textContent = latestLog ? latestLog.tail.join('\n') : 'launcher logs will appear here';
}

function renderDownloadStatus(data) {
  if (!data.available) {
    $('downloadRunId').textContent = 'no run';
    $('downloadState').textContent = 'idle';
    $('downloadTotal').textContent = '--';
    $('downloadDone').textContent = '--';
    $('downloadFailed').textContent = '--';
    $('downloadCurrent').textContent = data.message || '--';
    $('downloadNext').textContent = '--';
    $('downloadBar').style.width = '0%';
    $('downloadTail').textContent = '';
    return;
  }

  $('downloadRunId').textContent = data.run_id;
  $('downloadState').textContent = data.running ? 'running' : 'paused';
  $('downloadTotal').textContent = String(data.total_jobs ?? '--');
  $('downloadDone').textContent = String(data.downloaded ?? '--');
  $('downloadFailed').textContent = String(data.failed_latest ?? '--');
  const current = data.current_symbol || data.last_record?.symbol || '--';
  const currentKind = data.current_kind || data.last_record?.kind || '';
  $('downloadCurrent').textContent = currentKind ? `${current} / ${currentKind}` : current;
  $('downloadNext').textContent = data.next_symbol
    ? `${data.next_symbol}${data.next_kind ? ` / ${data.next_kind}` : ''}`
    : (data.remaining === 0 ? '完成' : '--');
  $('downloadBar').style.width = `${Math.max(0, Math.min(100, Number(data.percent || 0)))}%`;

  const tail = (data.progress_tail || []).slice(-8).map((item) => {
    const status = item.status || '?';
    const rows = item.rows === undefined ? '' : ` rows=${item.rows}`;
    const err = item.error ? ` ${item.error}` : '';
    const kind = item.kind ? ` ${item.kind}` : '';
    return `${item.symbol}${kind} ${item.timeframe} ${status}${rows}${err}`;
  });
  $('downloadTail').textContent = tail.join('\n') || '等待下载记录';
}

function pct(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return `${(num * 100).toFixed(1)}%`;
}

function compactNumber(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  if (Math.abs(num) >= 1e9) return `${(num / 1e9).toFixed(2)}B`;
  if (Math.abs(num) >= 1e6) return `${(num / 1e6).toFixed(2)}M`;
  if (Math.abs(num) >= 1e3) return `${(num / 1e3).toFixed(1)}K`;
  return num.toFixed(0);
}

function renderMonster(data) {
  if (!data.available) {
    $('monsterRunId').textContent = 'no run';
    $('monsterFresh').textContent = '--';
    $('monsterLiquidity').textContent = '--';
    $('monsterCandidates').textContent = '--';
    $('monsterUpdated').textContent = data.message || '--';
    $('monsterList').innerHTML = '<div class="monster-row stale"><span>暂无扫描结果</span></div>';
    return;
  }

  $('monsterRunId').textContent = data.run_id || '--';
  $('monsterFresh').textContent = String(data.fresh_top_count ?? '--');
  $('monsterLiquidity').textContent = String(data.liquidity_top_count ?? '--');
  $('monsterCandidates').textContent = String(data.trade_candidate_count ?? '--');
  const running = (data.processes || []).length > 0;
  $('monsterUpdated').textContent = running
    ? `running #${data.processes[0].pid}`
    : (data.updated_at ? new Date(data.updated_at).toLocaleTimeString() : '--');
  renderOrderbookStatus(data.orderbook || {});
  renderDerivativesStatus(data.derivatives || {});
  renderPaperStatus(data.paper || {});
  renderAutoRefreshStatus(data.auto_refresh || {});

  const rows = (data.trade_candidates || []).length ? data.trade_candidates : (data.top || []).slice(0, 8);
  $('monsterList').innerHTML = rows.map((item) => {
    const ok = Number(item.trade_candidate_flag || 0) === 1;
    const reason = item.trigger_reasons || '';
    return `
      <div class="monster-row ${ok ? '' : 'stale'}">
        <div class="monster-main">
          <strong>${item.symbol || '--'}</strong>
          <span>${item.sample_ts || '--'}</span>
        </div>
        <div class="monster-values">
          <b>${Number(item.monster_score_adj || 0).toFixed(3)}</b>
          <span>1h ${pct(item.ret_1h)} / 6h ${pct(item.ret_6h)} / 24h ${pct(item.ret_24h)}</span>
          <span>vol ${compactNumber(item.quote_volume_24h)} spread ${Number(item.spread_bps || 0).toFixed(2)}bps depth ${compactNumber(item.depth_1pct_usd)}</span>
        </div>
        <p>${reason}</p>
      </div>
    `;
  }).join('');
}

function renderOrderbookStatus(data) {
  if (!data.available) {
    $('monsterOrderbookStatus').textContent = `盘口采集：${data.message || '--'}`;
    return;
  }
  const last = data.last_record || {};
  const state = data.running ? 'running' : 'idle';
  const sym = last.symbol ? ` last ${last.symbol}` : '';
  const depth = Number(last.depth_1pct_usd);
  const depthText = Number.isFinite(depth) ? ` depth ${compactNumber(depth)}` : '';
  $('monsterOrderbookStatus').textContent = `盘口采集：${state} / ${data.run_id || '--'} / ok ${data.ok || 0} / failed ${data.failed || 0}${sym}${depthText}`;
}

function renderDerivativesStatus(data) {
  if (!data.available) {
    $('monsterDerivativesStatus').textContent = `结构采集：${data.message || '--'}`;
    return;
  }
  const last = data.last_record || {};
  const state = data.running ? 'running' : 'idle';
  const sym = last.symbol ? ` last ${last.symbol}` : '';
  const oi = Number(last.open_interest_value);
  const funding = Number(last.funding_rate);
  const lsr = Number(last.long_short_ratio);
  const oiText = Number.isFinite(oi) ? ` oi ${compactNumber(oi)}` : '';
  const fundingText = Number.isFinite(funding) ? ` funding ${(funding * 100).toFixed(4)}%` : '';
  const lsrText = Number.isFinite(lsr) ? ` l/s ${lsr.toFixed(2)}` : '';
  $('monsterDerivativesStatus').textContent = `结构采集：${state} / ${data.run_id || '--'} / ok ${data.ok || 0} / failed ${data.failed || 0}${sym}${oiText}${fundingText}${lsrText}`;
}

function renderPaperStatus(data) {
  if (!data.available) {
    $('monsterPaperStatus').textContent = `纸面 lottery：${data.message || '--'}`;
    return;
  }
  const positions = data.positions || {};
  const names = Object.keys(positions);
  const running = data.running ? 'running' : 'idle';
  const nav = Number(data.nav);
  const risk = Number(data.open_risk);
  const gate = data.live_gates_enabled ? ` gate ${data.live_gate_pass_count || 0}` : ' gate off';
  const navText = Number.isFinite(nav) ? ` nav ${nav.toFixed(2)}` : '';
  const riskText = Number.isFinite(risk) ? ` risk ${risk.toFixed(0)}` : '';
  const posText = names.length ? ` pos ${names.join(', ')}` : ' pos none';
  $('monsterPaperStatus').textContent = `纸面 lottery：${running} / ${data.state_id || '--'}${navText}${riskText}${gate}${posText}`;
  if (!cAutoPaperAvailable) renderPaperPanel(data);
}

function renderPaperPanel(data) {
  if (!data.available) {
    $('paperUpdated').textContent = data.message || '--';
    ['paperNav', 'paperCash', 'paperUnrealized', 'paperRealized', 'paperRisk', 'paperReturn', 'paperDrawdown', 'paperGate'].forEach((id) => {
      $(id).textContent = '--';
    });
    $('paperPositions').innerHTML = '<div class="paper-row stale">暂无纸面状态</div>';
    $('paperLedger').innerHTML = '<div class="paper-row stale">暂无事件</div>';
    drawPaperChart([]);
    return;
  }
  const metrics = data.metrics || {};
  $('paperUpdated').textContent = data.updated_at ? new Date(data.updated_at).toLocaleTimeString() : '--';
  $('paperNav').textContent = formatMoney(data.nav);
  $('paperCash').textContent = formatMoney(data.cash);
  $('paperUnrealized').textContent = formatSignedMoney(data.unrealized_pnl);
  $('paperRealized').textContent = formatSignedMoney(data.realized_pnl);
  $('paperRisk').textContent = formatMoney(data.open_risk);
  $('paperReturn').textContent = Number.isFinite(Number(metrics.total_return)) ? pct(metrics.total_return) : '--';
  $('paperDrawdown').textContent = Number.isFinite(Number(metrics.max_drawdown)) ? pct(metrics.max_drawdown) : '--';
  $('paperGate').textContent = data.live_gates_enabled ? `on / ${data.live_gate_pass_count || 0}` : 'off';

  const positions = data.positions || {};
  const rows = Object.entries(positions);
  $('paperPositions').innerHTML = rows.length ? rows.map(([symbol, pos]) => renderPaperPosition(symbol, pos)).join('') : '<div class="paper-row stale">无持仓</div>';
  const ledger = data.ledger_tail || [];
  $('paperLedger').innerHTML = ledger.length ? ledger.slice().reverse().map(renderPaperEvent).join('') : '<div class="paper-row stale">暂无事件</div>';
  drawPaperChart(data.equity || []);
}

function formatSignedMoney(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  const sign = num > 0 ? '+' : '';
  return `${sign}${num.toFixed(2)}`;
}

function renderPaperPosition(symbol, pos) {
  const side = pos.side || 'long';
  const score = Number(pos.score);
  const risk = Number(pos.risk_budget);
  const entry = Number(pos.entry_price);
  const stop = pos.stop_price === null || pos.stop_price === undefined ? NaN : Number(pos.stop_price);
  const tp1 = pos.tp1_price === null || pos.tp1_price === undefined ? NaN : Number(pos.tp1_price);
  const tp2 = pos.tp2_price === null || pos.tp2_price === undefined ? NaN : Number(pos.tp2_price);
  const oi = Number(pos.live_open_interest_value);
  const funding = Number(pos.live_funding_rate);
  const lsr = Number(pos.live_long_short_ratio);
  const regime = pos.regime ? ` / ${pos.regime}` : '';
  return `
    <div class="paper-row">
      <div class="paper-row-head">
        <strong>${symbol}</strong>
        <b>${side}</b>
      </div>
      <span>score ${Number.isFinite(score) ? score.toFixed(3) : '--'} / risk ${Number.isFinite(risk) ? risk.toFixed(0) : '--'}${regime}</span>
      <span>entry ${Number.isFinite(entry) ? entry.toPrecision(6) : '--'} / stop ${Number.isFinite(stop) ? stop.toPrecision(6) : '--'}</span>
      <span>tp1 ${Number.isFinite(tp1) ? tp1.toPrecision(6) : '--'} / tp2 ${Number.isFinite(tp2) ? tp2.toPrecision(6) : '--'}</span>
      <span>oi ${compactNumber(oi)} / funding ${Number.isFinite(funding) ? (funding * 100).toFixed(4) + '%' : '--'} / l/s ${Number.isFinite(lsr) ? lsr.toFixed(2) : '--'}</span>
    </div>
  `;
}

function renderPaperEvent(item) {
  const event = item.event || '?';
  const symbol = item.symbol || '--';
  const pnl = item.pnl === undefined ? '' : ` / pnl ${formatSignedMoney(item.pnl)}`;
  const reason = item.reason ? ` / ${item.reason}` : '';
  const ts = item.ts ? new Date(item.ts).toLocaleTimeString() : '--';
  return `
    <div class="paper-row ${event.includes('reject') ? 'stale' : ''}">
      <div class="paper-row-head">
        <strong>${symbol}</strong>
        <b>${event}</b>
      </div>
      <span>${ts}${reason}${pnl}</span>
    </div>
  `;
}

function drawPaperChart(points) {
  const canvas = $('paperChart');
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#fbfcfe';
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = '#d8dee8';
  ctx.lineWidth = 1;
  for (let i = 1; i <= 3; i += 1) {
    const y = (height / 4) * i;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
  const navs = (points || []).map((p) => Number(p.nav)).filter(Number.isFinite);
  if (navs.length < 1) {
    ctx.fillStyle = '#687385';
    ctx.font = '13px system-ui';
    ctx.fillText('等待权益历史', 14, 28);
    return;
  }
  const min = Math.min(...navs);
  const max = Math.max(...navs);
  const pad = Math.max((max - min) * 0.08, 1);
  const lo = min - pad;
  const hi = max + pad;
  ctx.strokeStyle = '#12825c';
  ctx.lineWidth = 2;
  ctx.beginPath();
  navs.forEach((nav, i) => {
    const x = navs.length === 1 ? width - 18 : (i / (navs.length - 1)) * (width - 24) + 12;
    const y = height - 18 - ((nav - lo) / (hi - lo)) * (height - 36);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  if (navs.length === 1) {
    const x = width - 18;
    const y = height - 18 - ((navs[0] - lo) / (hi - lo)) * (height - 36);
    ctx.fillStyle = '#12825c';
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.fillStyle = '#17202a';
  ctx.font = '12px system-ui';
  ctx.fillText(`NAV ${navs[navs.length - 1].toFixed(2)}`, 14, 20);
  ctx.fillStyle = '#687385';
  ctx.fillText(`min ${min.toFixed(2)} / max ${max.toFixed(2)}`, 14, height - 10);
}

function renderAutoRefreshStatus(data) {
  if (!data.available) {
    const running = data.running ? 'running' : 'idle';
    $('monsterAutoRefreshStatus').textContent = `自动刷新：${running} / ${data.message || '--'}`;
    return;
  }
  const running = data.running ? 'running' : 'idle';
  const last = data.last_record || {};
  const lastText = last.status ? ` last ${last.status}#${last.iteration || '-'}` : '';
  $('monsterAutoRefreshStatus').textContent = `自动刷新：${running} / ${data.run_id || '--'} / ok ${data.ok || 0} / failed ${data.failed || 0}${lastText}`;
}

async function refreshMonsterStatus() {
  try {
    const data = await api('/api/monster');
    renderMonster(data);
  } catch (err) {
    $('monsterUpdated').textContent = 'error';
    $('monsterList').innerHTML = `<div class="monster-row stale"><span>${err.message}</span></div>`;
  }
}

async function refreshMonsterData() {
  $('monsterUpdated').textContent = 'starting';
  const result = await api('/api/monster-refresh', { method: 'POST', body: '{}' });
  $('monsterUpdated').textContent = result.already_running ? 'already running' : `started #${result.pid}`;
  setTimeout(refreshMonsterStatus, 1500);
}

async function startMonsterOrderbook() {
  $('monsterOrderbookStatus').textContent = '盘口采集：starting';
  const result = await api('/api/monster-orderbook-start', { method: 'POST', body: '{}' });
  $('monsterOrderbookStatus').textContent = result.already_running
    ? '盘口采集：already running'
    : `盘口采集：started #${result.pid}`;
  setTimeout(refreshMonsterStatus, 1500);
}

async function startMonsterDerivatives() {
  $('monsterDerivativesStatus').textContent = '结构采集：starting';
  const result = await api('/api/monster-derivatives-start', { method: 'POST', body: '{}' });
  $('monsterDerivativesStatus').textContent = result.already_running
    ? '结构采集：already running'
    : `结构采集：started #${result.pid}`;
  setTimeout(refreshMonsterStatus, 1500);
}

async function startMonsterPaper() {
  $('monsterPaperStatus').textContent = '纸面 lottery：starting';
  const result = await api('/api/monster-paper-start', { method: 'POST', body: '{}' });
  $('monsterPaperStatus').textContent = result.already_running
    ? '纸面 lottery：already running'
    : `纸面 lottery：started #${result.pid}`;
  setTimeout(refreshMonsterStatus, 1500);
}

async function startMonsterAutoRefresh() {
  $('monsterAutoRefreshStatus').textContent = '自动刷新：starting';
  const result = await api('/api/monster-auto-refresh-start', { method: 'POST', body: '{}' });
  $('monsterAutoRefreshStatus').textContent = result.already_running
    ? '自动刷新：already running'
    : `自动刷新：started #${result.pid}`;
  setTimeout(refreshMonsterStatus, 1500);
}

async function refreshDownloadStatus() {
  try {
    const data = await api('/api/download-status');
    renderDownloadStatus(data);
  } catch (err) {
    $('downloadState').textContent = 'error';
    $('downloadTail').textContent = err.message;
  }
}

async function refreshStatus() {
  try {
    const data = await api('/api/status');
    renderStatus(data);
  } catch (err) {
    $('launcherStatus').textContent = 'error';
    $('lastAction').textContent = err.message;
  }
}

async function refreshLaunchOptions() {
  const data = await api('/api/launch-options');
  renderStrategyOptions(data);
}

async function startSystem() {
  state.port = Number($('portInput').value || 8080);
  applySelection();
  const meta = strategyMeta();
  if (state.mode === 'paper' && meta && meta.paper_supported === false) {
    throw new Error('该策略没有接入纸面交易模式');
  }
  if (state.mode === 'real' && meta && meta.real_supported === false && meta.kind === 'professional') {
    throw new Error('该 professional 策略尚未通过 live gate');
  }
  const confirmReal = $('realConfirm').checked;
  const confirmCompetition = $('competitionConfirm').checked;
  $('lastAction').textContent = '启动中...';
  const result = await api('/api/start', {
    method: 'POST',
    body: JSON.stringify({
      env: state.env,
      mode: state.mode,
      strategy: state.strategy,
      port: state.port,
      confirm_real: confirmReal,
      confirm_competition: confirmCompetition,
    }),
  });
  $('lastAction').textContent = `启动请求已提交 pid=${result.pid}`;
  $('openYolo').href = result.yolo_url || yoloUrl();
  $('yoloFrame').src = result.yolo_url || yoloUrl();
  setTimeout(refreshStatus, 1500);
}

async function stopSystem() {
  $('lastAction').textContent = '暂停中...';
  const result = await api('/api/stop', { method: 'POST', body: '{}' });
  $('lastAction').textContent = `暂停请求已提交 pid=${result.pid}`;
  setTimeout(refreshStatus, 1200);
}

async function restartSystem() {
  state.port = Number($('portInput').value || 8080);
  applySelection();
  $('lastAction').textContent = '重新开始中...';
  const result = await api('/api/restart', {
    method: 'POST',
    body: JSON.stringify({
      env: state.env,
      mode: state.mode,
      strategy: state.strategy,
      port: state.port,
      confirm_real: $('realConfirm').checked,
      confirm_competition: $('competitionConfirm').checked,
    }),
  });
  $('lastAction').textContent = `重新开始已提交 pid=${result.pid}`;
  $('openYolo').href = result.yolo_url || yoloUrl();
  $('yoloFrame').src = result.yolo_url || yoloUrl();
  setTimeout(refreshStatus, 1500);
}

async function pauseDownload() {
  $('downloadState').textContent = 'pausing';
  await api('/api/download-pause', { method: 'POST', body: '{}' });
  setTimeout(refreshDownloadStatus, 800);
}

async function resumeDownload() {
  $('downloadState').textContent = 'resuming';
  await api('/api/download-resume', { method: 'POST', body: '{}' });
  setTimeout(refreshDownloadStatus, 1200);
}

document.querySelectorAll('#modeGroup [data-value]').forEach((node) => {
  node.addEventListener('click', () => {
    state.mode = node.dataset.value;
    applySelection();
  });
});

$('envSelect').addEventListener('change', () => {
  state.env = $('envSelect').value;
  applySelection();
});

$('strategySelect').addEventListener('change', () => {
  state.strategy = $('strategySelect').value;
  applySelection();
});

$('portInput').addEventListener('change', () => {
  state.port = Number($('portInput').value || 8080);
  applySelection();
});

$('startBtn').addEventListener('click', () => {
  startSystem().catch((err) => {
    $('lastAction').textContent = err.message;
  });
});

$('stopBtn').addEventListener('click', () => {
  stopSystem().catch((err) => {
    $('lastAction').textContent = err.message;
  });
});

$('restartBtn').addEventListener('click', () => {
  restartSystem().catch((err) => {
    $('lastAction').textContent = err.message;
  });
});

$('refreshBtn').addEventListener('click', refreshStatus);
$('pauseDownloadBtn').addEventListener('click', () => {
  pauseDownload().catch((err) => {
    $('downloadState').textContent = 'error';
    $('downloadTail').textContent = err.message;
  });
});
$('resumeDownloadBtn').addEventListener('click', () => {
  resumeDownload().catch((err) => {
    $('downloadState').textContent = 'error';
    $('downloadTail').textContent = err.message;
  });
});
$('monsterRefreshBtn').addEventListener('click', () => {
  refreshMonsterData().catch((err) => {
    $('monsterUpdated').textContent = 'error';
    $('monsterList').innerHTML = `<div class="monster-row stale"><span>${err.message}</span></div>`;
  });
});
$('monsterOrderbookBtn').addEventListener('click', () => {
  startMonsterOrderbook().catch((err) => {
    $('monsterOrderbookStatus').textContent = `盘口采集：${err.message}`;
  });
});
$('monsterDerivativesBtn').addEventListener('click', () => {
  startMonsterDerivatives().catch((err) => {
    $('monsterDerivativesStatus').textContent = `结构采集：${err.message}`;
  });
});
$('monsterPaperBtn').addEventListener('click', () => {
  startMonsterPaper().catch((err) => {
    $('monsterPaperStatus').textContent = `纸面 lottery：${err.message}`;
  });
});
$('monsterAutoRefreshBtn').addEventListener('click', () => {
  startMonsterAutoRefresh().catch((err) => {
    $('monsterAutoRefreshStatus').textContent = `自动刷新：${err.message}`;
  });
});

applySelection();
refreshLaunchOptions().catch((err) => {
  $('lastAction').textContent = err.message;
});
refreshStatus();
refreshDownloadStatus();
refreshMonsterStatus();
setInterval(refreshStatus, 5000);
setInterval(refreshDownloadStatus, 5000);
setInterval(refreshMonsterStatus, 10000);
