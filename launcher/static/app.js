const state = {
  page: localStorage.getItem('launcher.page.v3') || 'strategies',
  readiness: null,
  accounts: null,
  statuses: {},
  runtimeStatuses: {},
  strategyPerformance: null,
  inFlight: new Set(),
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const { allowNotOk = false, ...fetchOptions } = options;
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...fetchOptions,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || (data.ok === false && !allowNotOk)) {
    const err = new Error(data.error || data.message || `HTTP ${response.status}`);
    err.data = data;
    throw err;
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function fmtMoney(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toFixed(2)} U` : '--';
}

function fmtPct(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--';
}

function fmtPrice(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '--';
  if (Math.abs(n) >= 1) return n.toFixed(4);
  return n.toPrecision(6);
}

function shortTime(value) {
  return value ? String(value).replace(' 北京时间', '') : '--';
}

function accountTruthLabel(verification = {}) {
  const account = verification.account || verification;
  const positions = Number(account.position_count ?? 0);
  const orders = Number(account.open_order_count ?? 0);
  const algos = Number(account.algo_order_count ?? 0);
  return `持仓 ${positions} · 挂单 ${orders} · algo ${algos}`;
}

function setPage(page) {
  state.page = page;
  localStorage.setItem('launcher.page.v3', page);
  document.querySelectorAll('.main-nav button').forEach((button) => {
    button.classList.toggle('active', button.dataset.page === page);
  });
  document.querySelectorAll('.app-page').forEach((pageNode) => {
    pageNode.classList.toggle('active', pageNode.id === `page-${page}`);
  });
}

function setButtonBusy(button, busy, text) {
  if (!button) return;
  button.disabled = busy;
  if (text) button.textContent = text;
  button.classList.toggle('busy', busy);
}

async function refreshReadiness() {
  const data = await api('/api/data-readiness');
  state.readiness = data;
  renderReadiness(data);
  syncStartButtons();
}

async function startDataRefresh() {
  const button = $('dataRefreshStartBtn');
  setButtonBusy(button, true, '刷新中...');
  $('systemStatus').textContent = '数据刷新请求已发送';
  try {
    const result = await api('/api/data-refresh-start', { method: 'POST', body: '{}' });
    $('systemStatus').textContent = result.already_running ? '数据刷新已在运行' : `数据刷新已启动 PID ${result.pid || '--'}`;
    await refreshReadiness();
    await refreshOperations();
  } catch (err) {
    $('systemStatus').textContent = err.message;
  } finally {
    setButtonBusy(button, false, '启动/刷新数据');
  }
}

function renderReadiness(data) {
  const pill = $('readinessPill');
  pill.textContent = data.ready ? 'ready' : 'waiting';
  pill.className = `status-pill ${data.ready ? 'good' : 'warn'}`;
  $('systemStatus').textContent = data.ready
    ? `数据 ready，目标 1h：${shortTime(data.required_1h_target_bj)}`
    : `数据未 ready：${(data.blocking_reasons || []).slice(0, 2).join(' / ') || '等待刷新'}`;

  const rows = data.categories || [];
  const required = rows.filter((row) => row.required_for_start);
  const readyRequired = required.filter((row) => row.ready).length;
  const blockingReasons = (data.blocking_reasons || []).slice(0, 4);
  $('dataReadinessDetails').innerHTML = `
    <div><span>必需项</span><strong>${readyRequired}/${required.length || rows.length}</strong></div>
    <div><span>1h 目标</span><strong>${escapeHtml(shortTime(data.required_1h_target_bj || data.required_1h_target))}</strong></div>
    <div><span>状态</span><strong>${data.ready ? 'ready' : 'blocked'}</strong></div>
    <div><span>阻塞</span><strong>${escapeHtml(blockingReasons.join(' / ') || 'none')}</strong></div>
  `;
  $('dataCategories').innerHTML = rows.map((row) => `
    <div class="data-row ${row.ready ? 'good' : 'warn'}">
      <div>
        <strong>${escapeHtml(row.category)}</strong>
        <span>${row.required_for_start ? '启动必需' : '辅助'}</span>
      </div>
      <div>${escapeHtml(shortTime(row.latest_data_ts_bj))}</div>
      <div>${row.symbol_count ?? 0} symbols</div>
      <div>${row.ready ? 'ready' : escapeHtml((row.reasons || []).join(', ') || 'waiting')}</div>
    </div>
  `).join('') || '<div class="empty">等待数据刷新状态</div>';

  const log = (data.log_tail || []).slice(-16).map((row) => {
    const status = row.status || (row.fresh ? 'ok' : 'wait');
    return `${shortTime(row.ts_bj)} ${status} ${row.kind || 'ohlcv'} ${row.symbol || ''} ${row.timeframe || ''} -> ${shortTime(row.target_end_bj)}`;
  }).join('\n');
  $('dataLog').textContent = log || '暂无刷新日志';
}

async function refreshAccounts() {
  const data = await api('/api/accounts');
  state.accounts = data.accounts || {};
  $('accountsUpdated').textContent = shortTime(data.updated_at_bj || data.updated_at);
  renderAccountCards();
  renderAccountPage('personal');
  renderAccountPage('competition');
}

function renderAccountCards() {
  const accounts = state.accounts || {};
  $('accountCards').innerHTML = ['personal', 'competition'].map((env) => {
    const account = accounts[env] || {};
    const balance = account.balance || {};
    return `
      <div class="account-card">
        <div class="card-title">${env === 'competition' ? '比赛' : '个人'}</div>
        <div class="big">${fmtMoney(balance.total_eq)}</div>
        <div class="muted">可用 ${fmtMoney(balance.usdt_avail)} · UPL ${fmtMoney(balance.upl)}</div>
        <div class="muted">持仓 ${account.position_count ?? 0} · ${account.ok ? 'OK' : escapeHtml(account.balance_error || account.positions_error || 'error')}</div>
      </div>
    `;
  }).join('');
}

async function refreshRuntime(env) {
  const [data, runtime] = await Promise.all([
    api(`/api/c-auto-v2-micro-live?env=${encodeURIComponent(env)}`),
    api(`/api/runtime-status?env=${encodeURIComponent(env)}`),
  ]);
  state.statuses[env] = data;
  state.runtimeStatuses[env] = runtime;
  renderAccountPage(env);
}

function renderAccountPage(env) {
  const account = state.accounts?.[env] || {};
  const status = state.statuses?.[env] || {};
  const runtime = state.runtimeStatuses?.[env] || {};
  const balance = account.balance || {};
  const prefix = env;
  $(`${prefix}Metrics`).innerHTML = [
    ['权益', fmtMoney(balance.total_eq)],
    ['可用', fmtMoney(balance.usdt_avail)],
    ['已用保证金', fmtMoney(balance.imr)],
    ['未实现', fmtMoney(balance.upl)],
    ['持仓', String(account.position_count ?? 0)],
    ['策略NAV', fmtMoney(status.nav)],
  ].map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join('');

  const positions = account.positions || [];
  $(`${prefix}Positions`).innerHTML = renderPositionsByStrategy(env, positions);

  const freshness = status.freshness || {};
  const scheduler = status.scheduler || {};
  const envProcesses = status.environment_processes || [];
  const sources = [...new Set(envProcesses.map((proc) => proc.source).filter(Boolean))];
  const blocked = Number(runtime.blocked_count ?? 0);
  const planned = Number(runtime.planned_count ?? 0);
  const running = Number(runtime.running_count ?? 0);
  const runtimeStrategies = runtime.strategies || [];
  const staleSchedulers = runtimeStrategies.filter((row) => row.scheduler?.stale_without_process).length;
  const runtimeErrors = runtimeStrategies
    .flatMap((row) => row.readiness?.errors || [])
    .slice(0, 2)
    .join(' / ');
  const accountingRows = runtimeStrategies
    .map((row) => row.accounting)
    .filter(Boolean);
  const unreconciled = accountingRows.filter((row) => row.ownership_reconciled === false).length;
  const ownedPositions = accountingRows.reduce((sum, row) => sum + Number(row.owned_positions || 0), 0);
  const exchangePositions = accountingRows.reduce((sum, row) => sum + Number(row.exchange_positions || 0), 0);
  const accountingErrors = accountingRows
    .flatMap((row) => row.errors || [])
    .filter(Boolean)
    .slice(0, 2)
    .join(' / ');
  const accountingTruth = accountingRows.length
    ? `${unreconciled ? `mismatch ${unreconciled}` : 'ok'} · owned ${ownedPositions} / ex ${exchangePositions}`
    : '--';
  const schedulerTruth = staleSchedulers > 0 ? `stale ${staleSchedulers}` : 'ok';
  $(`${prefix}Runtime`).innerHTML = `
    <div class="runtime-line"><span>运行</span><strong>${status.running ? 'running' : 'stopped'}</strong></div>
    <div class="runtime-line"><span>runner</span><strong>${running}/${planned} · blocked ${blocked}</strong></div>
    <div class="runtime-line"><span>进程</span><strong>${status.environment_process_count ?? envProcesses.length} · ${escapeHtml(sources.join(', ') || '--')}</strong></div>
    <div class="runtime-line"><span>周期</span><strong>${scheduler.cycles ?? '--'} · ${schedulerTruth}</strong></div>
    <div class="runtime-line"><span>freshness</span><strong>${freshness.passed ? 'pass' : 'wait'} / ${freshness.fresh_symbols ?? '--'}</strong></div>
    <div class="runtime-line"><span>最新市场</span><strong>${shortTime(freshness.latest_market_ts_bj || freshness.latest_market_ts)}</strong></div>
    <div class="runtime-line"><span>readiness</span><strong>${escapeHtml(runtimeErrors || 'ready')}</strong></div>
    <div class="runtime-line"><span>ownership</span><strong>${escapeHtml(accountingErrors || accountingTruth)}</strong></div>
    <div class="runtime-line"><span>last error</span><strong>${escapeHtml(scheduler.last_error || status.last_error || 'none')}</strong></div>
  `;
  renderAccountHistory(env, status);
}

function strategyLookup() {
  const map = new Map();
  (state.strategyPerformance?.strategies || []).forEach((row) => {
    if (!row.strategy_id) return;
    map.set(String(row.strategy_id), row);
  });
  return map;
}

function renderPositionsByStrategy(env, positions) {
  if (!positions.length) return '<div class="empty">当前无持仓</div>';
  const lookup = strategyLookup();
  const groups = new Map();
  positions.forEach((pos) => {
    const strategyId = pos.strategy_id || 'unknown';
    const perf = lookup.get(strategyId) || {};
    const label = pos.strategy_display_name || perf.display_name || strategyId;
    if (!groups.has(strategyId)) {
      groups.set(strategyId, {
        strategyId,
        label,
        source: pos.strategy_source || '',
        strategyPnl: perf.pnl,
        winRate: perf.win_rate,
        positions: [],
        upl: 0,
        notional: 0,
      });
    }
    const group = groups.get(strategyId);
    group.positions.push(pos);
    group.upl += Number(pos.upl) || 0;
    group.notional += Math.abs(Number(pos.notionalUsd) || 0);
  });

  return Array.from(groups.values())
    .sort((a, b) => Math.abs(b.notional) - Math.abs(a.notional) || a.label.localeCompare(b.label))
    .map((group) => `
      <div class="position-strategy-group">
        <div class="position-strategy-head">
          <div>
            <strong>${escapeHtml(group.label)}</strong>
            <span>${escapeHtml(group.strategyId)}${group.source ? ` · ${escapeHtml(group.source)}` : ''}</span>
          </div>
          <div>持仓 ${group.positions.length}</div>
          <div>名义 ${fmtMoney(group.notional)}</div>
          <div class="${group.upl >= 0 ? 'pos' : 'neg'}">持仓浮盈 ${fmtMoney(group.upl)}</div>
          <div class="${Number(group.strategyPnl) >= 0 ? 'pos' : 'neg'}">策略累计 ${group.strategyPnl == null ? '--' : fmtMoney(group.strategyPnl)}</div>
          <div>胜率 ${group.winRate == null ? '--' : fmtPct(group.winRate)}</div>
        </div>
        <div class="position-strategy-positions">
          ${group.positions.map((pos) => `
            <div class="position-row">
              <div>
                <strong>${escapeHtml(pos.instId)}</strong>
                <span>${escapeHtml(pos.side)} · ${pos.contracts} · ${pos.lever || '--'}x</span>
              </div>
              <div>
                <strong>mark ${pos.markPx || '--'}</strong>
                <span>entry ${pos.entry_price || pos.avgPx || '--'}</span>
              </div>
              <div class="${Number(pos.upl) >= 0 ? 'pos' : 'neg'}">${fmtMoney(pos.upl)}</div>
              <button data-close-symbol="${escapeHtml(pos.instId)}" data-env="${env}" type="button">一键平仓</button>
            </div>
          `).join('')}
        </div>
      </div>
    `).join('');
}

function renderAccountHistory(env, status) {
  const events = (status.ledger_tail || [])
    .filter((event) => ['entry', 'exit', 'forced_exit', 'manual_exit', 'entry_rejected', 'skip', 'committee_note'].includes(String(event.event || '')))
    .slice(-30)
    .reverse();
  $(`${env}History`).innerHTML = events.length ? events.map((event) => {
    const name = String(event.event || '');
    const actionClass = name.includes('exit') ? 'exit' : name === 'entry' ? 'entry' : name.includes('rejected') ? 'reject' : 'note';
    const price = event.exit_price ?? event.entry_price ?? event.exchange_fill_px ?? event.signal_entry_price;
    const thesis = event.thesis?.severity || event.thesis_contract?.contract_id || '';
    return `
      <div class="history-row ${actionClass}">
        <div>
          <strong>${escapeHtml(name)}</strong>
          <span>${shortTime(event.ts_bj || event.ts)} · ${escapeHtml(event.symbol || '--')} · ${escapeHtml(event.side || '--')}</span>
        </div>
        <div>${escapeHtml(event.reason || '--')}</div>
        <div>${price == null ? '--' : fmtPrice(price)}</div>
        <div class="${Number(event.pnl) >= 0 ? 'pos' : 'neg'}">${event.pnl == null ? '--' : fmtMoney(event.pnl)}</div>
        <div>${escapeHtml(thesis || '')}</div>
      </div>
    `;
  }).join('') : '<div class="empty">暂无进出场记录</div>';
}

function syncStartButtons() {
  for (const env of ['personal', 'competition']) {
    const button = $(`${env}StartBtn`);
    if (!button || state.inFlight.has(`start:${env}`)) continue;
    button.disabled = false;
    button.textContent = `启动${env === 'competition' ? '比赛' : '个人'}`;
  }
}

async function refreshStrategies() {
  const data = await api('/api/strategy-performance');
  state.strategyPerformance = data;
  $('strategyUpdated').textContent = shortTime(data.updated_at_bj || data.updated_at);
  renderStrategies(data);
}

function renderStrategies(data) {
  const rows = data.strategies || [];
  $('strategySummary').textContent = `运行中 paper 策略 ${data.running_paper_strategies ?? 0} 个 · 总策略 ${rows.length} 个`;
  $('strategyList').innerHTML = rows.map((row) => {
    const accounting = row.accounting || null;
    const accountingLabel = accounting
      ? `accounting ${accounting.environment} · fills ${accounting.exchange_fills ?? 0} · bills ${accounting.exchange_bills ?? 0}`
      : '';
    return `
      <div class="strategy-row">
        <div>
          <strong>${escapeHtml(row.display_name || row.strategy_id)}</strong>
          <span>${escapeHtml((row.sources || []).join(', ') || row.paper_role || '--')}</span>
          ${accountingLabel ? `<span>${escapeHtml(accountingLabel)}</span>` : ''}
          ${row.runtime_rule ? `<span class="strategy-rule">${escapeHtml(row.runtime_rule)}</span>` : ''}
        </div>
        <div>胜率 ${row.win_rate == null ? '--' : fmtPct(row.win_rate)}</div>
        <div class="${Number(row.pnl) >= 0 ? 'pos' : 'neg'}">${fmtMoney(row.pnl)}</div>
        <button data-stop-strategy="${escapeHtml(row.strategy_id)}" data-sources="${escapeHtml(JSON.stringify(row.sources || []))}" type="button">停止</button>
      </div>
    `;
  }).join('') || '<div class="empty">暂无策略数据</div>';
  drawStrategyChart(rows);
}

function drawStrategyChart(rows) {
  const canvas = $('strategyChart');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#f7f8fa';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const sourceLabel = {
    micro_live_personal: '个人',
    paper_competition: '比赛',
    legacy_paper: '旧paper',
    monster_paper: 'monster',
  };
  const series = [];
  rows.forEach((row) => {
    const bySource = {};
    (row.series || []).forEach((point) => {
      const source = point.source || 'unknown';
      bySource[source] = bySource[source] || [];
      bySource[source].push(point);
    });
    Object.entries(bySource).forEach(([source, points]) => {
      if (!points.length) return;
      series.push({
        label: `${row.display_name || row.strategy_id} · ${sourceLabel[source] || source}`,
        source,
        points,
      });
    });
  });
  series.sort((a, b) => {
    const order = { micro_live_personal: 0, paper_competition: 1, legacy_paper: 2, monster_paper: 3 };
    return (order[a.source] ?? 9) - (order[b.source] ?? 9) || a.label.localeCompare(b.label);
  });
  const visibleSeries = series.slice(0, 10);
  if (!visibleSeries.length) {
    ctx.fillStyle = '#6b7280';
    ctx.fillText('等待策略曲线', 24, 36);
    return;
  }
  const pointValue = (point) => Number(point.value ?? point.nav ?? point.pnl ?? 0);
  const values = visibleSeries.flatMap((row) => row.points.map(pointValue).filter(Number.isFinite));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = Math.max(1e-9, max - min);
  const colors = {
    micro_live_personal: '#0f766e',
    paper_competition: '#2563eb',
    legacy_paper: '#6b7280',
    monster_paper: '#b45309',
    unknown: '#374151',
  };
  visibleSeries.forEach((row, idx) => {
    const points = row.points || [];
    const color = colors[row.source] || colors.unknown;
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    points.forEach((point, i) => {
      const x = 30 + (canvas.width - 60) * (i / Math.max(1, points.length - 1));
      const value = pointValue(point);
      if (!Number.isFinite(value)) return;
      const y = canvas.height - 28 - (canvas.height - 56) * ((value - min) / span);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    const last = points[points.length - 1];
    const lastValue = last ? pointValue(last) : NaN;
    if (Number.isFinite(lastValue)) {
      const x = canvas.width - 24;
      const y = canvas.height - 28 - (canvas.height - 56) * ((lastValue - min) / span);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillText(row.label, 36, 22 + idx * 18);
    }
  });
}

async function refreshCommittee() {
  const data = await api('/api/committee-decisions');
  $('committeeUpdated').textContent = shortTime(data.updated_at_bj || data.updated_at);
  $('committeeLog').innerHTML = (data.decisions || []).map((row) => `
    <div class="decision-row ${row.decision === 'accepted' ? 'accepted' : row.decision === 'rejected' ? 'rejected' : ''}">
      <div>
        <strong>${escapeHtml(row.strategy_id || row.event || '--')}</strong>
        <span>${escapeHtml(row.environment)} · ${escapeHtml(row.symbol || '--')} · ${escapeHtml(row.side || '--')} · ${shortTime(row.ts_bj)}</span>
      </div>
      <div>${escapeHtml(row.decision)}</div>
      <div>${row.margin_usdt ? `投入 ${fmtMoney(row.margin_usdt)} · 杠杆 ${row.leverage || '--'}x` : escapeHtml(row.reason || '--')}</div>
      <div>${row.stop_price ? `止损 ${row.stop_price} · 止盈 ${row.tp1_price || '--'}` : ''}</div>
    </div>
  `).join('') || '<div class="empty">暂无委员会日志</div>';
}

async function refreshOperations() {
  const data = await api('/api/operations');
  const latest = (data.operations || []).slice(-5).reverse();
  $('operationLog').innerHTML = latest.map((row) => `
    <div><strong>${escapeHtml(row.action)}</strong> ${escapeHtml(row.environment || 'all')} · ${escapeHtml(row.result)} · ${shortTime(row.ts_bj)}</div>
  `).join('') || '等待操作';
}

async function startEnv(env) {
  const key = `start:${env}`;
  if (state.inFlight.has(key)) return;
  state.inFlight.add(key);
  setButtonBusy($(`${env}StartBtn`), true, '启动中...');
  $(`${env}Feedback`).textContent = '环境 runner 请求已发送，等待策略管理模块确认';
  try {
    const result = await api('/api/environment-start', {
      method: 'POST',
      body: JSON.stringify({
        environment: env,
        confirm_real: true,
        confirm_competition: env === 'competition',
      }),
    });
    const started = (result.started || []).map((row) => row.strategy || row.strategy_id).filter(Boolean);
    const errors = (result.errors || []).map((row) => `${row.strategy_id}: ${row.error}`);
    if (errors.length) {
      $(`${env}Feedback`).textContent = `部分失败：${errors.join(' / ')}`;
    } else if (started.length) {
      $(`${env}Feedback`).textContent = `已启动 ${started.length} 个策略：${started.join(' / ')}`;
    } else if (result.already_running) {
      $(`${env}Feedback`).textContent = '已经在运行';
    } else {
      $(`${env}Feedback`).textContent = '环境 runner 已完成';
    }
    await refreshRuntime(env);
    await refreshOperations();
  } catch (err) {
    $(`${env}Feedback`).textContent = err.message;
  } finally {
    state.inFlight.delete(key);
    setButtonBusy($(`${env}StartBtn`), false);
    syncStartButtons();
  }
}

async function stopEnv(env) {
  const key = `stop:${env}`;
  if (state.inFlight.has(key)) return;
  state.inFlight.add(key);
  setButtonBusy($(`${env}StopBtn`), true, '暂停中...');
  $(`${env}Feedback`).textContent = '暂停请求已发送';
  try {
    const result = await api('/api/environment-stop', {
      method: 'POST',
      body: JSON.stringify({ environment: env }),
      allowNotOk: true,
    });
    const cancel = result.order_cancel || {};
    const verification = result.verification || {};
    const stillRunning = Number(verification.running_processes ?? 0);
    const algoCancelled = Number(cancel.algo_orders_cancelled ?? 0);
    const failed = Number(cancel.orders_failed ?? 0) + Number(cancel.algo_orders_failed ?? 0);
    const truth = accountTruthLabel(verification);
    if (verification.ok) {
      $(`${env}Feedback`).textContent = `已暂停 · 撤单 ${cancel.orders_cancelled ?? 0} · 撤algo ${algoCancelled} · ${truth}`;
    } else {
      $(`${env}Feedback`).textContent = `暂停未完成 · 进程 ${stillRunning} · 失败 ${failed} · ${truth}`;
    }
    await refreshAccounts();
    await refreshRuntime(env);
    await refreshStrategies();
    await refreshOperations();
  } catch (err) {
    $(`${env}Feedback`).textContent = err.message;
  } finally {
    state.inFlight.delete(key);
    setButtonBusy($(`${env}StopBtn`), false, `暂停${env === 'competition' ? '比赛' : '个人'}`);
  }
}

async function closePosition(env, instId) {
  const ok = window.confirm(`确认${env === 'competition' ? '比赛' : '个人'}账户一键平仓 ${instId}？`);
  if (!ok) return;
  $(`${env}Feedback`).textContent = `平仓 ${instId}...`;
  const result = await api('/api/account/close-symbol', {
    method: 'POST',
    body: JSON.stringify({
      instId,
      environment: env,
      confirm_live_close: true,
    }),
    allowNotOk: true,
  });
  const truth = accountTruthLabel(result.verification || {});
  $(`${env}Feedback`).textContent = result.ok
    ? `已平仓 ${instId} · ${truth}`
    : `平仓需复查 ${instId} · ${truth}`;
  await refreshAccounts();
  await refreshRuntime(env);
}

async function closeAllPositions(env) {
  const positions = state.accounts?.[env]?.positions || [];
  const countLabel = positions.length ? `${positions.length} 个持仓` : '当前账户全部持仓和保护单';
  const ok = window.confirm(`确认${env === 'competition' ? '比赛' : '个人'}账户一键清仓 ${countLabel}？`);
  if (!ok) return;
  const key = `close-all:${env}`;
  if (state.inFlight.has(key)) return;
  state.inFlight.add(key);
  setButtonBusy($(`${env}CloseAllBtn`), true, '清仓中...');
  try {
    const result = await api('/api/account/close-all', {
      method: 'POST',
      body: JSON.stringify({
        environment: env,
        confirm_live_close: true,
      }),
      allowNotOk: true,
    });
    const closed = result.positions_closed ?? 0;
    const failed = Array.isArray(result.errors) ? result.errors.length : 0;
    const found = result.positions_found ?? positions.length;
    const truth = accountTruthLabel(result.verification || {});
    $(`${env}Feedback`).textContent = result.ok
      ? `一键清仓完成 · 已处理 ${closed}/${found} · ${truth}`
      : `一键清仓未完成 · 已处理 ${closed}/${found} · 失败 ${failed} · ${truth}`;
    await refreshAccounts();
    await refreshRuntime(env);
    await refreshStrategies();
    await refreshOperations();
  } catch (err) {
    $(`${env}Feedback`).textContent = err.message;
  } finally {
    state.inFlight.delete(key);
    setButtonBusy($(`${env}CloseAllBtn`), false, '一键清仓');
  }
}

async function stopStrategy(strategyId, sources) {
  const ok = window.confirm(`确认停止 ${strategyId}？`);
  if (!ok) return;
  await api('/api/strategy-stop', {
    method: 'POST',
    body: JSON.stringify({ strategy_id: strategyId, sources }),
  });
  await refreshStrategies();
  await refreshRuntime('personal');
  await refreshRuntime('competition');
}

async function refreshAll() {
  await Promise.allSettled([
    refreshReadiness(),
    refreshAccounts(),
    refreshRuntime('personal'),
    refreshRuntime('competition'),
    refreshStrategies(),
    refreshCommittee(),
    refreshOperations(),
  ]);
}

document.querySelectorAll('.main-nav button').forEach((button) => {
  button.addEventListener('click', () => setPage(button.dataset.page));
});

$('dataRefreshStartBtn').addEventListener('click', () => startDataRefresh());
$('personalStartBtn').addEventListener('click', () => startEnv('personal'));
$('competitionStartBtn').addEventListener('click', () => startEnv('competition'));
$('personalStopBtn').addEventListener('click', () => stopEnv('personal'));
$('competitionStopBtn').addEventListener('click', () => stopEnv('competition'));
$('personalCloseAllBtn').addEventListener('click', () => closeAllPositions('personal'));
$('competitionCloseAllBtn').addEventListener('click', () => closeAllPositions('competition'));

document.addEventListener('click', (event) => {
  const closeTarget = event.target.closest('[data-close-symbol]');
  if (closeTarget) {
    closePosition(closeTarget.dataset.env, closeTarget.dataset.closeSymbol).catch((err) => {
      $(`${closeTarget.dataset.env}Feedback`).textContent = err.message;
    });
    return;
  }
  const stopTarget = event.target.closest('[data-stop-strategy]');
  if (stopTarget) {
    let sources = [];
    try {
      sources = JSON.parse(stopTarget.dataset.sources || '[]');
    } catch (err) {
      sources = [];
    }
    stopStrategy(stopTarget.dataset.stopStrategy, sources).catch((err) => {
      $('strategySummary').textContent = err.message;
    });
  }
});

setPage(state.page);
refreshAll();
setInterval(refreshAll, 10000);
