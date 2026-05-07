const $ = (id) => document.getElementById(id);

const state = {
  selectedRunId: null,
  timer: null,
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function renderDownloadStatus(data) {
  if (!data.available) {
    $('downloadRunId').textContent = 'no run';
    $('downloadState').textContent = 'idle';
    $('downloadTotal').textContent = '--';
    $('downloadDone').textContent = '--';
    $('downloadFailed').textContent = '--';
    $('downloadRemaining').textContent = '--';
    $('downloadCurrent').textContent = data.message || '--';
    $('downloadNext').textContent = '--';
    $('downloadUpdated').textContent = '--';
    $('downloadDir').textContent = '--';
    $('catalogState').textContent = '--';
    $('qualityState').textContent = '--';
    $('qualityRows').textContent = '--';
    $('qualityCoverage').textContent = '--';
    $('downloadBar').style.width = '0%';
    $('downloadTail').textContent = '';
    return;
  }

  state.selectedRunId = data.run_id;
  $('downloadRunId').textContent = data.run_id;
  const typeLabel = data.dataset_type ? `${data.dataset_type} / ` : '';
  $('downloadState').textContent = `${typeLabel}${data.running ? 'running' : (data.remaining === 0 ? 'completed' : 'paused')}`;
  $('downloadTotal').textContent = String(data.total_jobs ?? '--');
  $('downloadDone').textContent = String(data.downloaded ?? '--');
  $('downloadFailed').textContent = String(data.failed_latest ?? '--');
  $('downloadRemaining').textContent = String(data.remaining ?? '--');
  $('downloadUpdated').textContent = formatTime(data.updated_at);
  $('downloadDir').textContent = data.run_dir || '--';
  $('catalogState').textContent = data.catalog_record?.dataset_id ? 'registered' : '--';
  $('qualityState').textContent = data.quality?.validation_status || '--';
  $('qualityRows').textContent = formatNumber(data.quality?.rows);
  $('qualityCoverage').textContent = data.quality?.coverage_median === undefined || data.quality?.coverage_median === null
    ? '--'
    : `${(Number(data.quality.coverage_median) * 100).toFixed(1)}%`;

  const current = data.current_symbol || data.last_record?.symbol || '--';
  const currentTimeframe = data.current_timeframe || data.last_record?.timeframe || '';
  const currentKind = data.current_kind || data.last_record?.kind || '';
  $('downloadCurrent').textContent = [current, currentTimeframe, currentKind].filter(Boolean).join(' / ');
  $('downloadNext').textContent = data.next_symbol
    ? [data.next_symbol, data.next_timeframe, data.next_kind].filter(Boolean).join(' / ')
    : (data.remaining === 0 ? '完成' : '--');
  $('downloadBar').style.width = `${Math.max(0, Math.min(100, Number(data.percent || 0)))}%`;

  const tail = (data.progress_tail || []).slice(-14).map((item) => {
    const status = item.status || '?';
    const rows = item.rows === undefined ? '' : ` rows=${item.rows}`;
    const coverage = item.coverage === undefined ? '' : ` coverage=${Number(item.coverage).toFixed(3)}`;
    const err = item.error ? ` ${item.error}` : '';
    const kind = item.kind ? ` ${item.kind}` : '';
    return `${item.symbol}${kind} ${item.timeframe} ${status}${rows}${coverage}${err}`;
  });
  $('downloadTail').textContent = tail.join('\n') || '等待下载记录';
}

function renderRuns(payload) {
  const runs = payload.runs || [];
  if (!runs.length) {
    $('runsList').innerHTML = '<button type="button" class="run-row stale">暂无下载记录</button>';
    return;
  }
  $('runsList').innerHTML = runs.slice(0, 12).map((run) => `
    <button type="button" class="run-row ${run.running ? 'running' : ''}" data-run-id="${escapeHtml(run.run_id)}">
      <span>${escapeHtml(run.run_id)}</span>
      <b>${escapeHtml(run.status || (run.running ? 'running' : 'unknown'))}</b>
      <small>${escapeHtml(run.dataset_type || '--')} · ${escapeHtml(run.quality_status || 'no-quality')} · ${run.catalog_registered ? 'catalog' : 'not-cataloged'} · ${escapeHtml(formatTime(run.updated_at))}</small>
    </button>
  `).join('');
  document.querySelectorAll('[data-run-id]').forEach((node) => {
    node.addEventListener('click', () => {
      state.selectedRunId = node.dataset.runId;
      refreshDownloadStatus();
    });
  });
}

async function refreshDownloadStatus() {
  try {
    const suffix = state.selectedRunId ? `?run_id=${encodeURIComponent(state.selectedRunId)}` : '';
    const data = await api(`/api/download/status${suffix}`);
    renderDownloadStatus(data);
    $('serviceStatus').textContent = 'online';
  } catch (err) {
    $('downloadState').textContent = 'error';
    $('downloadTail').textContent = err.message;
    $('serviceStatus').textContent = 'error';
  }
}

async function refreshRuns() {
  try {
    renderRuns(await api('/api/download/runs'));
  } catch (err) {
    $('runsList').innerHTML = `<button type="button" class="run-row stale">${escapeHtml(err.message)}</button>`;
  }
}

async function refreshAll() {
  await refreshDownloadStatus();
  await refreshRuns();
}

async function pauseDownload() {
  $('downloadState').textContent = 'pausing';
  await api('/api/download/pause', {
    method: 'POST',
    body: JSON.stringify({ run_id: state.selectedRunId }),
  });
  setTimeout(refreshAll, 900);
}

async function resumeDownload() {
  $('downloadState').textContent = 'resuming';
  await api('/api/download/resume', {
    method: 'POST',
    body: JSON.stringify({ run_id: state.selectedRunId }),
  });
  setTimeout(refreshAll, 1300);
}

async function startDownload(event) {
  event.preventDefault();
  $('downloadState').textContent = 'starting';
  const payload = {
    dataset_type: valueOf('datasetTypeInput') || 'training_history',
    run_id: valueOf('runIdInput'),
    symbols: valueOf('symbolsInput'),
    symbols_manifest: valueOf('manifestInput'),
    start: valueOf('startInput') || '2024-01-01',
    end: valueOf('endInput'),
    timeframes: valueOf('timeframesInput') || '1h',
    timeframe: valueOf('timeframeInput') || '5m',
    kinds: valueOf('kindsInput') || 'funding,open_interest,long_short',
    limit: Number(valueOf('limitInput') || 100),
    min_volume_usd: Number(valueOf('minVolumeInput') || 30000000),
    max_symbols: Number(valueOf('maxSymbolsInput') || 250),
    sleep_sec: Number(valueOf('sleepInput') || 0.5),
    workers: Number(valueOf('workersInput') || 1),
    min_rows: Number(valueOf('minRowsInput') || 100),
    skip_funding: $('skipFundingInput').checked,
    refresh_universe: $('refreshUniverseInput').checked,
    discover_only: $('discoverOnlyInput').checked,
  };
  const result = await api('/api/download/start', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  state.selectedRunId = result.run_id;
  setTimeout(refreshAll, 1300);
}

function valueOf(id) {
  const value = $(id).value.trim();
  return value || null;
}

function formatTime(value) {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function formatNumber(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return new Intl.NumberFormat('zh-CN').format(num);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

$('refreshBtn').addEventListener('click', refreshAll);
$('pauseDownloadBtn').addEventListener('click', pauseDownload);
$('resumeDownloadBtn').addEventListener('click', resumeDownload);
$('startForm').addEventListener('submit', startDownload);

refreshAll();
state.timer = setInterval(refreshAll, 5000);
