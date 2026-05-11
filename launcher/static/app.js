const state = {
  env: localStorage.getItem('launcher.env') || 'personal',
  mode: localStorage.getItem('launcher.mode') || 'paper',
  strategy: localStorage.getItem('launcher.strategy') || 'core_c_auto_h24_regression_v1',
  port: Number(localStorage.getItem('launcher.port') || 8080),
  page: localStorage.getItem('launcher.page') || 'overview',
};

let launchOptions = { strategies: [] };
let cAutoPaperAvailable = false;
let latestCAutoStatus = null;
let latestMicroLiveStatus = null;
let latestMonsterPaperStatus = null;
let latestPipelineStatus = null;
let stopInFlight = false;
const DOWNLOAD_COLLAPSED_KEY = 'launcher.downloadCollapsed.v2';
const storedDownloadCollapsed = localStorage.getItem(DOWNLOAD_COLLAPSED_KEY);
let downloadCollapsed = storedDownloadCollapsed === null ? true : storedDownloadCollapsed === 'true';

const $ = (id) => document.getElementById(id);

function setActive(groupId, value) {
  document.querySelectorAll(`#${groupId} [data-value]`).forEach((node) => {
    node.classList.toggle('active', node.dataset.value === value);
  });
}

function setActivePage(page) {
  state.page = page;
  document.querySelectorAll('.main-nav [data-page]').forEach((node) => {
    node.classList.toggle('active', node.dataset.page === page);
  });
  document.querySelectorAll('.app-page').forEach((node) => {
    node.classList.toggle('active', node.id === `page-${page}`);
  });
  localStorage.setItem('launcher.page', page);
  requestAnimationFrame(() => {
    if (latestCAutoStatus) renderCAutoChart(latestCAutoStatus);
    if (latestMonsterPaperStatus) renderMonsterPaperBlock(latestMonsterPaperStatus);
    if (latestCAutoStatus) renderPaperPanel(latestCAutoStatus);
    if (latestMicroLiveStatus) renderMicroLivePanel(latestMicroLiveStatus);
  });
}

function applyDownloadCollapsed() {
  $('downloadWidget').classList.toggle('collapsed', downloadCollapsed);
  $('toggleDownloadBtn').textContent = downloadCollapsed ? '展开' : '收起';
  $('toggleDownloadBtn').setAttribute('aria-expanded', downloadCollapsed ? 'false' : 'true');
  localStorage.setItem(DOWNLOAD_COLLAPSED_KEY, String(downloadCollapsed));
}

function applySelection() {
  setActive('modeGroup', state.mode);
  $('envSelect').value = state.env;
  $('strategySelect').value = state.strategy;
  renderSelectedStrategyMeta();
  $('portInput').value = state.port;
  $('realConfirmBox').classList.toggle('hidden', state.mode !== 'real');
  $('competitionConfirmLine').classList.toggle('hidden', !(state.mode === 'real' && state.env === 'competition'));
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

function launchPidText(result) {
  if (result.pid) return `pid=${result.pid}`;
  const pid = (result.processes || [])[0]?.pid;
  if (pid) return `pid=${pid}`;
  return result.already_running ? 'already running' : 'submitted';
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
    cache: 'no-store',
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

function formatBeijingTime(value, withDate = false) {
  if (!value) return '--';
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return '--';
  return date.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: withDate ? '2-digit' : undefined,
    day: withDate ? '2-digit' : undefined,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
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
  const killSwitch = data.kill_switch || {};
  $('launcherStatus').textContent = killSwitch.active ? `paused: ${killSwitch.reason || 'kill switch'}` : 'ready';
  $('updatedAt').textContent = `北京时间 ${formatBeijingTime(new Date())}`;

  const dashboard = data.pids?.dashboard || {};
  const strategies = data.pids?.strategies || [];
  const running = strategies.filter((item) => item.alive);
  $('dashboardState').textContent = dashboard.alive ? `running #${dashboard.pid}` : 'stopped';
  $('strategyState').textContent = running.length ? `${running.length} running` : 'stopped';
  const pro = data.pro_paper || {};
  const cauto = data.c_auto_v2_paper || {};
  const microLive = data.c_auto_v2_micro_live || {};
  const dataRefresh = data.data_refresh || {};
  const dataRefreshStatus = dataRefresh.status || {};
  cAutoPaperAvailable = Boolean(cauto.available);
  renderCAutoPanel(cauto, dataRefresh);
  renderMicroLivePanel(microLive);
  const scheduler = pro.scheduler || {};
  $('proPaperState').textContent = pro.running ? `running #${(pro.processes || [])[0]?.pid || '-'}` : (pro.available ? (scheduler.scheduler_status || 'idle') : 'idle');
  $('proPaperCycles').textContent = scheduler.cycles === undefined ? '--' : String(scheduler.cycles);
  $('dataRefreshState').textContent = dataRefresh.running
    ? `running #${(dataRefresh.processes || [])[0]?.pid || '-'}`
    : (dataRefresh.available ? (dataRefreshStatus.scheduler_status || 'idle') : 'idle');
  $('dataRefreshCycle').textContent = dataRefreshStatus.cycle === undefined ? '--' : String(dataRefreshStatus.cycle);
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
  const microRows = (data.pids?.c_auto_v2_micro_live || []).map((item) => `
    <div class="run-row">
      <span>c-auto-v2 / ${item.environment || 'competition'} / micro-live / pid ${item.pid}</span>
      <b>alive</b>
    </div>
  `);
  const dataRows = (data.pids?.data_refresh || []).map((item) => `
    <div class="run-row">
      <span>data-refresh / pid ${item.pid}</span>
      <b>alive</b>
    </div>
  `);
  if (!strategies.length && !proRows.length && !cautoRows.length && !microRows.length && !dataRows.length) {
    list.innerHTML = '<div class="run-row stale"><span>暂无策略 pid 文件</span><b>idle</b></div>';
  } else {
    list.innerHTML = [
      ...dataRows,
      ...microRows,
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

function renderCAutoPanel(data, dataRefresh = {}) {
  latestCAutoStatus = data || null;
  if (!data || !data.available) {
    $('cautoMode').textContent = 'not ready';
    $('cautoRunning').textContent = 'idle';
    $('cautoEnv').textContent = state.env;
    $('cautoFreshness').textContent = '--';
    $('cautoCandidateCount').textContent = '--';
    $('cautoPositionCount').textContent = '--';
    $('cautoLiveGate').textContent = '--';
    $('cautoNav').textContent = '--';
    $('cautoPnl').textContent = '--';
    $('cautoDataset').textContent = '--';
    $('cautoPolicy').textContent = '--';
    $('cautoDataRefresh').textContent = dataRefresh.running ? 'running' : 'idle';
    $('cautoPositions').innerHTML = '<div class="cauto-row stale">暂无持仓</div>';
    $('cautoCandidates').innerHTML = '<div class="cauto-row stale">暂无 C-Auto 状态</div>';
    $('cautoEvents').innerHTML = '<div class="cauto-row stale">暂无事件</div>';
    renderCAutoChart(null);
    return;
  }

  const processes = data.processes || [];
  const positions = data.positions || {};
  const candidates = data.latest_candidates || [];
  const freshness = data.freshness || {};
  const passed = freshness.passed === true;
  const reasons = (freshness.reasons || []).join(', ');
  const freshText = passed
    ? `pass / ${freshness.fresh_symbols ?? '--'}`
    : `wait / ${freshness.fresh_symbols ?? '--'}${reasons ? ` / ${reasons}` : ''}`;
  const scheduler = data.scheduler || {};
  const refreshStatus = dataRefresh.running
    ? `running #${(dataRefresh.processes || [])[0]?.pid || '-'}`
    : ((dataRefresh.status || {}).scheduler_status || 'idle');

  $('cautoMode').textContent = `${data.mode || 'paper'} / ${data.source_mode || 'live'}`;
  $('cautoRunning').textContent = data.running ? `running #${processes[0]?.pid || '-'}` : (data.runner_status || 'idle');
  $('cautoEnv').textContent = data.environment || state.env;
  $('cautoFreshness').textContent = freshText;
  $('cautoCandidateCount').textContent = String(candidates.length || 0);
  $('cautoPositionCount').textContent = String(Object.keys(positions).length);
  $('cautoLiveGate').textContent = data.live_gates_enabled ? `on / ${data.live_gate_pass_count || 0}` : 'off';
  $('cautoNav').textContent = formatMoney(data.nav);
  $('cautoPnl').textContent = formatSignedMoney(data.unrealized_pnl);
  $('cautoDataset').textContent = data.dataset_id || '--';
  $('cautoPolicy').textContent = data.policy_id || '--';
  $('cautoDataRefresh').textContent = `${refreshStatus} / cycle ${(dataRefresh.status || {}).cycle ?? '--'} / paper cycle ${scheduler.cycles ?? '--'}`;
  const positionRows = Object.entries(positions);
  $('cautoPositions').innerHTML = positionRows.length
    ? positionRows.map(([symbol, pos]) => renderCAutoPosition(symbol, pos, data)).join('')
    : '<div class="cauto-row stale">无持仓</div>';

  $('cautoCandidates').innerHTML = candidates.length
    ? candidates.slice(0, 8).map(renderCAutoCandidate).join('')
    : '<div class="cauto-row stale">暂无候选</div>';

  const ledger = data.ledger_tail || [];
  $('cautoEvents').innerHTML = ledger.length
    ? ledger.slice().reverse().slice(0, 8).map(renderCAutoEvent).join('')
    : '<div class="cauto-row stale">暂无事件</div>';
  renderCAutoChart(data);
}

function renderCAutoPosition(symbol, pos, data) {
  const side = pos.side || '--';
  const entry = Number(pos.entry_price);
  const mark = Number(pos.mark_price);
  const pnl = Number(pos.unrealized_pnl);
  const ret = Number(pos.net_return ?? pos.unrealized_pct);
  const risk = Number(pos.risk_budget);
  const stop = Number(pos.stop_price);
  const tp1 = Number(pos.tp1_price);
  const tp2 = Number(pos.tp2_price);
  const score = Number(pos.score);
  const source = pos.source_strategy_id || pos.signal_family || '--';
  const expectedEv = Number(pos.expected_ev);
  const pTarget = Number(pos.p_target);
  const stopDist = priceDistance(mark, stop, side);
  const tp1Dist = priceDistance(mark, tp1, side);
  const tp2Dist = priceDistance(mark, tp2, side);
  const decisionId = pos.decision_id ? String(pos.decision_id).slice(0, 12) : '--';
  const committeeReason = pos.committee_reason || '';
  const entryTime = pos.entry_ts ? formatBeijingTime(pos.entry_ts, true) : '--';
  const exitTime = pos.exit_ts ? formatBeijingTime(pos.exit_ts, true) : '--';
  const pnlClass = Number.isFinite(pnl) && pnl < 0 ? 'loss' : 'gain';
  const mode = data.mode || 'paper';
  return `
    <div class="cauto-position-card">
      <div class="cauto-position-head">
        <div>
          <strong>${escapeHtml(symbol)}</strong>
          <span>${escapeHtml(side)} / ${escapeHtml(pos.regime || '--')} / ${escapeHtml(source)}</span>
        </div>
        <button class="small-danger" data-cauto-close="${escapeAttr(symbol)}" data-mode="${escapeAttr(mode)}">清仓</button>
      </div>
      <div class="cauto-trade-grid">
        <div><span>入场</span><strong>${formatPrice(entry)}</strong></div>
        <div><span>现价</span><strong>${formatPrice(mark)}</strong></div>
        <div><span>未实现</span><strong class="${pnlClass}">${formatSignedMoney(pnl)}</strong></div>
        <div><span>收益率</span><strong class="${pnlClass}">${Number.isFinite(ret) ? pct(ret) : '--'}</strong></div>
        <div><span>风险额</span><strong>${formatMoney(risk)}</strong></div>
        <div><span>Score</span><strong>${Number.isFinite(score) ? score.toFixed(4) : '--'}</strong></div>
        <div><span>目标概率</span><strong>${Number.isFinite(pTarget) ? pct(pTarget) : '--'}</strong></div>
        <div><span>预期EV</span><strong class="${Number.isFinite(expectedEv) && expectedEv < 0 ? 'loss' : 'gain'}">${Number.isFinite(expectedEv) ? pct(expectedEv) : '--'}</strong></div>
        <div><span>止损</span><strong>${formatPrice(stop)}</strong></div>
        <div><span>TP1</span><strong>${formatPrice(tp1)}</strong></div>
        <div><span>TP2</span><strong>${formatPrice(tp2)}</strong></div>
        <div><span>距止损</span><strong>${Number.isFinite(stopDist) ? pct(stopDist) : '--'}</strong></div>
        <div><span>距TP1</span><strong>${Number.isFinite(tp1Dist) ? pct(tp1Dist) : '--'}</strong></div>
        <div><span>距TP2</span><strong>${Number.isFinite(tp2Dist) ? pct(tp2Dist) : '--'}</strong></div>
      </div>
      <div class="cauto-position-foot">
        <span>进场 ${escapeHtml(entryTime)}</span>
        <span>计划退出 ${escapeHtml(exitTime)}</span>
      </div>
      <div class="cauto-position-foot">
        <span>决策 ${escapeHtml(decisionId)}</span>
        <span>${escapeHtml(committeeReason || 'committee accepted')}</span>
      </div>
    </div>
  `;
}

function priceDistance(mark, target, side) {
  if (!Number.isFinite(mark) || !Number.isFinite(target) || mark <= 0 || target <= 0) return NaN;
  const raw = target / mark - 1;
  return side === 'short' ? -raw : raw;
}

function formatPrice(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  if (Math.abs(num) >= 100) return num.toFixed(2);
  if (Math.abs(num) >= 1) return num.toFixed(4);
  return num.toPrecision(6);
}

function renderCAutoCandidate(item) {
  const score = Number(item.score);
  const side = item.side || '--';
  const eligible = item.eligible ? 'eligible' : 'blocked';
  const blocked = item.blocked_by_crowding ? ' / crowding' : '';
  return `
    <div class="cauto-row ${item.eligible ? '' : 'stale'}">
      <div class="paper-row-head">
        <strong>${escapeHtml(item.symbol || '--')}</strong>
        <b>${escapeHtml(side)}</b>
      </div>
      <span>${eligible}${blocked} / regime ${escapeHtml(item.regime || '--')}</span>
      <span>score ${Number.isFinite(score) ? score.toFixed(3) : '--'}</span>
    </div>
  `;
}

function renderCAutoEvent(item) {
  const event = item.event || '?';
  const symbol = item.symbol || '--';
  const ts = item.ts ? formatBeijingTime(item.ts) : '--';
  const reason = item.reason ? ` / ${item.reason}` : '';
  const pnl = item.pnl === undefined || item.pnl === null ? '' : ` / pnl ${formatSignedMoney(item.pnl)}`;
  const source = item.source_strategy_id ? ` / ${item.source_strategy_id}` : '';
  const ev = Number(item.expected_ev);
  const pTarget = Number(item.p_target);
  const committee = item.committee_reason ? ` / ${item.committee_reason}` : '';
  const edge = Number.isFinite(ev) || Number.isFinite(pTarget)
    ? ` / p ${Number.isFinite(pTarget) ? pct(pTarget) : '--'} ev ${Number.isFinite(ev) ? pct(ev) : '--'}`
    : '';
  return `
    <div class="cauto-row ${event.includes('reject') || event === 'skip' ? 'stale' : ''}">
      <div class="paper-row-head">
        <strong>${escapeHtml(symbol)}</strong>
        <b>${escapeHtml(event)}</b>
      </div>
      <span>${escapeHtml(ts + source + reason + pnl + edge + committee)}</span>
    </div>
  `;
}

function renderPipelineStatus(data) {
  latestPipelineStatus = data || null;
  if (!data || data.ok === false) {
    $('pipelineUpdated').textContent = data?.error || 'error';
    $('pipelineCapital').textContent = '--';
    $('pipelineTarget').textContent = '--';
    $('pipelinePassed').textContent = '--';
    $('pipelineBlocked').textContent = '--';
    $('pipelineMissing').textContent = '--';
    $('pipelineLayers').innerHTML = '<div class="pipeline-row block"><strong>8层状态不可用</strong><span>等待 API 返回</span></div>';
    return;
  }
  const capital = data.capital || {};
  const summary = data.summary || {};
  $('pipelineUpdated').textContent = data.generated_at ? `北京时间 ${formatBeijingTime(data.generated_at)}` : '--';
  $('pipelineCapital').textContent = `${formatMoney(capital.base_capital_usdt)}U`;
  $('pipelineTarget').textContent = Number.isFinite(Number(capital.monthly_return_target_pct))
    ? pct(capital.monthly_return_target_pct)
    : '--';
  $('pipelinePassed').textContent = String(summary.passed_layers ?? '--');
  $('pipelineBlocked').textContent = String(summary.blocked_layers ?? '--');
  $('pipelineMissing').textContent = String(summary.missing_layers ?? '--');
  $('pipelineLayers').innerHTML = (data.layers || []).map(renderPipelineLayer).join('');
}

function renderPipelineLayer(layer) {
  const status = layer.status || 'missing';
  const evidence = layer.evidence || {};
  const paperGate = evidence.paper_gate || null;
  const failed = paperGate?.failed_checks || [];
  const stats = paperGate?.stats || {};
  const detail = paperGate
    ? [
        `days ${formatNumber(stats.calendar_days, 2)}`,
        `closed ${stats.closed_trades ?? 0}`,
        `stop ${Number.isFinite(Number(stats.stop_execution_rate)) ? pct(stats.stop_execution_rate) : '--'}`,
        `gross ${Number.isFinite(Number(stats.gross_leverage)) ? pct(stats.gross_leverage) : '--'}`,
      ].join(' / ')
    : (layer.next_action || 'ok');
  const failedText = failed.length ? `<span class="pipeline-failed">${failed.map(escapeHtml).join(', ')}</span>` : '';
  return `
    <div class="pipeline-row ${escapeAttr(status)}">
      <div>
        <strong>${layer.id}. ${escapeHtml(layer.name || '--')}</strong>
        <span>${escapeHtml(detail)}</span>
        ${failedText}
      </div>
      <b>${escapeHtml(status)}</b>
    </div>
  `;
}

function formatNumber(value, digits = 2) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return num.toFixed(digits);
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
    $('monsterPaperNav').textContent = '--';
    $('monsterPaperPnl').textContent = '--';
    $('monsterUpdated').textContent = data.message || '--';
    $('monsterList').innerHTML = '<div class="monster-row stale"><span>暂无扫描结果</span></div>';
    renderMonsterPaperBlock(null);
    return;
  }

  $('monsterRunId').textContent = data.run_id || '--';
  $('monsterFresh').textContent = String(data.fresh_top_count ?? '--');
  $('monsterLiquidity').textContent = String(data.liquidity_top_count ?? '--');
  $('monsterCandidates').textContent = String(data.trade_candidate_count ?? '--');
  const running = (data.processes || []).length > 0;
  $('monsterUpdated').textContent = running
    ? `running #${data.processes[0].pid}`
    : (data.updated_at ? `北京时间 ${formatBeijingTime(data.updated_at)}` : '--');
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
  latestMonsterPaperStatus = data || null;
  renderMonsterPaperBlock(data || null);
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

function renderMonsterPaperBlock(data) {
  if (!data || !data.available) {
    $('monsterPaperNav').textContent = '--';
    $('monsterPaperPnl').textContent = '--';
    $('monsterPaperPositions').innerHTML = '<div class="paper-row stale">暂无妖币纸面持仓</div>';
    $('monsterPaperLedger').innerHTML = '<div class="paper-row stale">暂无交易事件</div>';
    drawEquityChart('monsterChart', [], [], { emptyText: '等待妖币纸面权益历史' });
    return;
  }
  $('monsterPaperNav').textContent = formatMoney(data.nav);
  $('monsterPaperPnl').textContent = formatSignedMoney(data.unrealized_pnl);
  const positions = data.positions || {};
  const rows = Object.entries(positions);
  $('monsterPaperPositions').innerHTML = rows.length
    ? rows.map(([symbol, pos]) => renderPaperPosition(symbol, pos, { closeAttr: 'data-monster-close' })).join('')
    : '<div class="paper-row stale">无持仓</div>';
  const ledger = data.ledger_tail || [];
  $('monsterPaperLedger').innerHTML = ledger.length
    ? ledger.slice().reverse().slice(0, 20).map(renderPaperEvent).join('')
    : '<div class="paper-row stale">暂无事件</div>';
  drawEquityChart('monsterChart', data.equity || [], ledger, { emptyText: '等待妖币纸面权益历史' });
}

function renderPaperPanel(data) {
  if (!data.available) {
    $('paperUpdated').textContent = data.message || '--';
    ['paperNav', 'paperCash', 'paperUnrealized', 'paperRealized', 'paperRisk', 'paperReturn', 'paperDrawdown', 'paperGate'].forEach((id) => {
      $(id).textContent = '--';
    });
    $('paperPositions').innerHTML = '<div class="paper-row stale">暂无纸面状态</div>';
    $('paperLedger').innerHTML = '<div class="paper-row stale">暂无事件</div>';
    drawEquityChart('paperChart', [], [], { emptyText: '等待纸面权益历史' });
    return;
  }
  const metrics = data.metrics || {};
  $('paperUpdated').textContent = data.updated_at ? `北京时间 ${formatBeijingTime(data.updated_at)}` : '--';
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
  $('paperPositions').innerHTML = rows.length ? rows.map(([symbol, pos]) => renderPaperPosition(symbol, pos, { closeAttr: 'data-paper-close' })).join('') : '<div class="paper-row stale">无持仓</div>';
  const ledger = data.ledger_tail || [];
  $('paperLedger').innerHTML = ledger.length ? ledger.slice().reverse().map(renderPaperEvent).join('') : '<div class="paper-row stale">暂无事件</div>';
  drawEquityChart('paperChart', data.equity || [], ledger, { emptyText: '等待纸面权益历史' });
}

function renderMicroLivePanel(data) {
  latestMicroLiveStatus = data || null;
  if (!data || !data.available) {
    $('microLiveUpdated').textContent = data?.message || '--';
    ['microLiveRunning', 'microLiveBudget', 'microLiveNav', 'microLiveUnrealized', 'microLiveRealized', 'microLivePositionsCount', 'microLiveRisk', 'microLiveGate'].forEach((id) => {
      $(id).textContent = '--';
    });
    $('microLivePositions').innerHTML = '<div class="paper-row stale">暂无 micro-live 状态</div>';
    $('microLiveLedger').innerHTML = '<div class="paper-row stale">暂无事件</div>';
    drawEquityChart('microLiveChart', [], [], { emptyText: '等待 Micro Live 权益历史' });
    return;
  }
  const processes = data.processes || [];
  const positions = data.positions || {};
  const daily = data.daily_risk || {};
  $('microLiveUpdated').textContent = data.updated_at ? `北京时间 ${formatBeijingTime(data.updated_at)}` : '--';
  $('microLiveRunning').textContent = data.running ? `running #${processes[0]?.pid || '-'}` : ((data.scheduler || {}).scheduler_status || data.runner_status || 'idle');
  $('microLiveBudget').textContent = `${formatMoney(data.daily_budget_usdt)} / ${formatMoney(data.per_symbol_margin_usdt)} each`;
  $('microLiveNav').textContent = formatMoney(data.nav);
  $('microLiveUnrealized').textContent = formatSignedMoney(data.unrealized_pnl);
  $('microLiveRealized').textContent = formatSignedMoney(data.realized_pnl);
  $('microLivePositionsCount').textContent = `${Object.keys(positions).length} / ${data.max_positions ?? '--'}`;
  $('microLiveRisk').textContent = daily.allow_new_entries === false
    ? `blocked / ${daily.block_reason || 'risk'}`
    : `${formatSignedMoney(daily.realized_pnl_usdt)} today`;
  $('microLiveGate').textContent = data.live_gates_enabled ? `on / ${data.live_gate_pass_count || 0}` : 'off';
  const rows = Object.entries(positions);
  $('microLivePositions').innerHTML = rows.length ? rows.map(([symbol, pos]) => renderMicroLivePosition(symbol, pos)).join('') : '<div class="paper-row stale">无真实小仓</div>';
  const ledger = data.ledger_tail || [];
  $('microLiveLedger').innerHTML = ledger.length ? ledger.slice().reverse().slice(0, 20).map(renderPaperEvent).join('') : '<div class="paper-row stale">暂无事件</div>';
  drawEquityChart('microLiveChart', data.equity || [], ledger, { emptyText: '等待 Micro Live 权益历史' });
}

function renderMicroLivePosition(symbol, pos) {
  const side = pos.side || '--';
  const margin = Number(pos.margin_usdt);
  const notional = Number(pos.notional_usdt);
  const lev = Number(pos.leverage);
  const pnl = Number(pos.unrealized_pnl);
  const contracts = Number(pos.contracts);
  const stop = Number(pos.stop_price);
  const tp1 = Number(pos.tp1_price);
  const mark = Number(pos.mark_price);
  const exitTime = pos.exit_ts ? formatBeijingTime(pos.exit_ts, true) : '--';
  const cls = Number.isFinite(pnl) && pnl < 0 ? 'loss' : 'gain';
  return `
    <div class="paper-row">
      <div class="paper-row-head">
        <strong>${escapeHtml(symbol)}</strong>
        <span class="row-actions">
          <b>${escapeHtml(side)} · ${Number.isFinite(lev) ? lev.toFixed(1) : '--'}x</b>
          <button class="small-danger" data-micro-live-close="${escapeAttr(symbol)}">清仓</button>
        </span>
      </div>
      <span>margin ${formatMoney(margin)} / notional ${formatMoney(notional)} / contracts ${Number.isFinite(contracts) ? contracts : '--'}</span>
      <span>mark ${formatPrice(mark)} / SL ${formatPrice(stop)} / TP ref ${formatPrice(tp1)} / stop attached ${pos.exchange_stop_attached ? 'yes' : 'no'}</span>
      <span>计划退出 ${escapeHtml(exitTime)} / 交易所止盈 ${pos.exchange_tp_attached ? 'yes' : 'no'}</span>
      <span>unrealized <strong class="${cls}">${formatSignedMoney(pnl)}</strong> / decision ${escapeHtml(String(pos.decision_id || '').slice(0, 12) || '--')}</span>
    </div>
  `;
}

function formatSignedMoney(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  const sign = num > 0 ? '+' : '';
  return `${sign}${num.toFixed(2)}`;
}

function renderPaperPosition(symbol, pos, options = {}) {
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
  const pnl = Number(pos.unrealized_pnl);
  const closeAttr = options.closeAttr || '';
  const closeButton = closeAttr ? `<button class="small-danger" ${closeAttr}="${escapeAttr(symbol)}">清仓</button>` : '';
  return `
    <div class="paper-row">
      <div class="paper-row-head">
        <strong>${escapeHtml(symbol)}</strong>
        <span class="row-actions">
          <b>${escapeHtml(side)}</b>
          ${closeButton}
        </span>
      </div>
      <span>score ${Number.isFinite(score) ? score.toFixed(3) : '--'} / risk ${Number.isFinite(risk) ? risk.toFixed(0) : '--'}${regime}</span>
      <span>entry ${Number.isFinite(entry) ? entry.toPrecision(6) : '--'} / stop ${Number.isFinite(stop) ? stop.toPrecision(6) : '--'}</span>
      <span>tp1 ${Number.isFinite(tp1) ? tp1.toPrecision(6) : '--'} / tp2 ${Number.isFinite(tp2) ? tp2.toPrecision(6) : '--'}</span>
      <span>unrealized <strong class="${Number.isFinite(pnl) && pnl < 0 ? 'loss' : 'gain'}">${formatSignedMoney(pnl)}</strong></span>
      <span>oi ${compactNumber(oi)} / funding ${Number.isFinite(funding) ? (funding * 100).toFixed(4) + '%' : '--'} / l/s ${Number.isFinite(lsr) ? lsr.toFixed(2) : '--'}</span>
    </div>
  `;
}

function renderPaperEvent(item) {
  const event = item.event || '?';
  const symbol = item.symbol || '--';
  const pnl = item.pnl === undefined ? '' : ` / pnl ${formatSignedMoney(item.pnl)}`;
  const reason = item.reason ? ` / ${item.reason}` : '';
  const ts = item.ts ? formatBeijingTime(item.ts) : '--';
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

function renderCAutoChart(data) {
  drawEquityChart('cautoChart', data?.equity || [], data?.ledger_tail || [], { emptyText: '等待 C-Auto 权益历史' });
}

function eventIsEntry(event) {
  return ['entry', 'manual_entry', 'buy'].includes(String(event || '').toLowerCase());
}

function eventIsExit(event) {
  const name = String(event || '').toLowerCase();
  return name.includes('exit') || name.includes('flatten') || name.includes('stop');
}

function drawEquityChart(canvasId, points, events = [], options = {}) {
  const canvas = $(canvasId);
  if (!canvas) return;
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
  const cleanPoints = (points || [])
    .map((p, index) => ({
      index,
      ts: p.ts ? new Date(p.ts).getTime() : index,
      nav: Number(p.nav),
    }))
    .filter((p) => Number.isFinite(p.nav));
  const navs = cleanPoints.map((p) => p.nav);
  if (navs.length < 1) {
    ctx.fillStyle = '#687385';
    ctx.font = '13px system-ui';
    ctx.fillText(options.emptyText || '等待权益历史', 14, 28);
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
  cleanPoints.forEach((point, i) => {
    const nav = point.nav;
    const x = cleanPoints.length === 1 ? width - 18 : (i / (cleanPoints.length - 1)) * (width - 24) + 12;
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

  const tradeEvents = (events || []).filter((item) => eventIsEntry(item.event) || eventIsExit(item.event));
  tradeEvents.slice(-30).forEach((item) => {
    const rawTs = item.ts ? new Date(item.ts).getTime() : NaN;
    const idx = Number.isFinite(rawTs)
      ? nearestPointIndex(cleanPoints, rawTs)
      : cleanPoints.length - 1;
    const point = cleanPoints[idx] || cleanPoints[cleanPoints.length - 1];
    if (!point) return;
    const x = cleanPoints.length === 1 ? width - 18 : (idx / (cleanPoints.length - 1)) * (width - 24) + 12;
    const y = height - 18 - ((point.nav - lo) / (hi - lo)) * (height - 36);
    const isEntry = eventIsEntry(item.event);
    const label = isEntry ? (item.side === 'short' ? '开空' : '买入') : '退出';
    ctx.fillStyle = isEntry ? '#1d5fd1' : '#b42318';
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.font = '11px system-ui';
    const text = `${label}${item.symbol ? ` ${String(item.symbol).replace('/USDT', '')}` : ''}`;
    const textWidth = ctx.measureText(text).width + 8;
    const boxX = Math.max(6, Math.min(width - textWidth - 6, x - textWidth / 2));
    const boxY = isEntry ? Math.max(24, y - 24) : Math.min(height - 28, y + 10);
    ctx.fillStyle = isEntry ? 'rgba(29, 95, 209, 0.12)' : 'rgba(180, 35, 24, 0.12)';
    ctx.fillRect(boxX, boxY, textWidth, 17);
    ctx.fillStyle = isEntry ? '#1d5fd1' : '#b42318';
    ctx.fillText(text, boxX + 4, boxY + 12);
  });

  ctx.fillStyle = '#17202a';
  ctx.font = '12px system-ui';
  ctx.fillText(`NAV ${navs[navs.length - 1].toFixed(2)}`, 14, 20);
  ctx.fillStyle = '#687385';
  ctx.fillText(`min ${min.toFixed(2)} / max ${max.toFixed(2)}`, 14, height - 10);
}

function nearestPointIndex(points, ts) {
  let best = 0;
  let bestDist = Infinity;
  points.forEach((point, index) => {
    const dist = Math.abs((Number.isFinite(point.ts) ? point.ts : index) - ts);
    if (dist < bestDist) {
      best = index;
      bestDist = dist;
    }
  });
  return best;
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

async function refreshPipelineStatus() {
  try {
    const data = await api('/api/8-layer-pipeline');
    renderPipelineStatus(data);
  } catch (err) {
    renderPipelineStatus({ ok: false, error: err.message });
  }
}

async function closeCAutoSymbol(symbol, mode) {
  const data = latestCAutoStatus || {};
  const liveMode = ['real', 'live', 'production'].includes(String(mode || data.mode || '').toLowerCase());
  if (liveMode) {
    const ok = window.confirm(`确认真实清仓 ${symbol}？这会撤该标的挂单并调用 OKX close position。`);
    if (!ok) return;
  }
  $('lastAction').textContent = `清仓 ${symbol}...`;
  const result = await api('/api/c-auto-v2/close-symbol', {
    method: 'POST',
    body: JSON.stringify({
      symbol,
      state_id: data.state_id || 'fixed1000_conservative',
      environment: data.environment || state.env,
      mode: data.mode || 'paper',
      confirm_live_close: liveMode,
    }),
  });
  $('lastAction').textContent = result.closed ? `已清仓 ${result.symbol}` : `${symbol} 未持仓`;
  if (result.status) renderCAutoPanel(result.status);
  setTimeout(refreshStatus, 800);
}

async function closeMicroLiveSymbol(symbol) {
  const ok = window.confirm(`确认真实 Micro Live 清仓 ${symbol}？这会撤该标的挂单并调用 OKX close position。`);
  if (!ok) return;
  const data = latestMicroLiveStatus || {};
  $('lastAction').textContent = `Micro Live 清仓 ${symbol}...`;
  const result = await api('/api/c-auto-v2-micro-live/close-symbol', {
    method: 'POST',
    body: JSON.stringify({
      symbol,
      state_id: data.state_id || 'micro_live_competition',
      environment: data.environment || 'competition',
      confirm_live_close: true,
    }),
  });
  $('lastAction').textContent = result.closed ? `Micro Live 已清仓 ${result.symbol}` : `${symbol} 未持仓`;
  if (result.status) renderMicroLivePanel(result.status);
  setTimeout(refreshStatus, 1000);
}

async function closeMonsterSymbol(symbol) {
  const data = latestMonsterPaperStatus || {};
  $('lastAction').textContent = `妖币纸面清仓 ${symbol}...`;
  const result = await api('/api/monster-paper/close-symbol', {
    method: 'POST',
    body: JSON.stringify({
      symbol,
      state_id: data.state_id || 'lottery_live',
    }),
  });
  $('lastAction').textContent = result.closed ? `妖币纸面已清仓 ${result.symbol}` : `${symbol} 未持仓`;
  if (result.status) renderMonsterPaperBlock(result.status);
  setTimeout(refreshMonsterStatus, 800);
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
      fresh_start: true,
    }),
  });
  const archived = result.archived_session?.session_id ? ` · 已归档 ${result.archived_session.session_id}` : '';
  $('lastAction').textContent = `全新启动已提交 ${launchPidText(result)}${archived}`;
  setTimeout(refreshStatus, 1500);
}

async function stopSystem() {
  if (stopInFlight) return;
  stopInFlight = true;
  $('stopBtn').disabled = true;
  $('lastAction').textContent = '暂停中...';
  try {
    const result = await api('/api/stop', { method: 'POST', body: '{}' });
    const status = await api('/api/status');
    renderStatus(status);
    const cancel = result.order_cancel || {};
    const paperRunning = (status.pids?.c_auto_v2_paper || []).length;
    const microRunning = (status.pids?.c_auto_v2_micro_live || []).length;
    const refreshRunning = (status.pids?.data_refresh || []).length;
    const strategyRunning = (status.pids?.strategies || []).filter((item) => item.alive).length;
    if (paperRunning || microRunning || refreshRunning || strategyRunning) {
      $('lastAction').textContent = `暂停异常 · paper ${paperRunning} · micro ${microRunning} · refresh ${refreshRunning} · strategy ${strategyRunning}`;
      throw new Error('暂停请求已发送，但后台仍有进程运行');
    }
    $('lastAction').textContent = `已暂停 · 撤单 ${cancel.orders_cancelled ?? 0} · 失败 ${cancel.orders_failed ?? 0}`;
  } finally {
    stopInFlight = false;
    $('stopBtn').disabled = false;
  }
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
  $('lastAction').textContent = `重新开始已提交 ${launchPidText(result)}`;
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

document.querySelectorAll('.main-nav [data-page]').forEach((node) => {
  node.addEventListener('click', () => setActivePage(node.dataset.page));
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

$('stopBtn').addEventListener('click', handleStopClick);

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
$('toggleDownloadBtn').addEventListener('click', () => {
  downloadCollapsed = !downloadCollapsed;
  applyDownloadCollapsed();
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
document.addEventListener('click', (event) => {
  if (event.target.closest('#stopBtn')) {
    handleStopClick(event);
    return;
  }
  const cautoTarget = event.target.closest('[data-cauto-close]');
  if (cautoTarget) {
    closeCAutoSymbol(cautoTarget.dataset.cautoClose, cautoTarget.dataset.mode).catch((err) => {
      $('lastAction').textContent = err.message;
    });
    return;
  }
  const paperTarget = event.target.closest('[data-paper-close]');
  if (paperTarget) {
    closeCAutoSymbol(paperTarget.dataset.paperClose, 'paper').catch((err) => {
      $('lastAction').textContent = err.message;
    });
    return;
  }
  const microTarget = event.target.closest('[data-micro-live-close]');
  if (microTarget) {
    closeMicroLiveSymbol(microTarget.dataset.microLiveClose).catch((err) => {
      $('lastAction').textContent = err.message;
    });
    return;
  }
  const monsterTarget = event.target.closest('[data-monster-close]');
  if (!monsterTarget) return;
  closeMonsterSymbol(monsterTarget.dataset.monsterClose).catch((err) => {
    $('lastAction').textContent = err.message;
  });
}, true);

function handleStopClick(event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  stopSystem().catch((err) => {
    $('lastAction').textContent = err.message;
    refreshStatus();
  });
}

applySelection();
setActivePage(state.page);
applyDownloadCollapsed();
refreshLaunchOptions().catch((err) => {
  $('lastAction').textContent = err.message;
});
refreshStatus();
refreshPipelineStatus();
refreshDownloadStatus();
refreshMonsterStatus();
setInterval(refreshStatus, 5000);
setInterval(refreshPipelineStatus, 15000);
setInterval(refreshDownloadStatus, 5000);
setInterval(refreshMonsterStatus, 10000);
