/* Data Shell playground: build a contract from the form, run the real engine,
   and render the same verdict the CLI prints. */

const schemaRows = document.getElementById('schemaRows');
const addSchema = document.getElementById('addSchema');
const ruleRows = document.getElementById('ruleRows');
const addRule = document.getElementById('addRule');
const batchRows = document.getElementById('batchRows');
const addBatch = document.getElementById('addBatch');
const pgForm = document.getElementById('pgForm');
const pgResult = document.getElementById('pgResult');
const pgOut = document.getElementById('pgOut');
const pgRawWrap = document.getElementById('pgRawWrap');
const loadSample = document.getElementById('loadSample');
const fileHint = document.getElementById('fileHint');

/* A generated file we keep in memory when the user has no data of their own. */
let sampleFile = null;

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === 'html') n.innerHTML = v;
    else if (k === 'checked' && v === true) n.checked = true;
    else n.setAttribute(k, v);
  });
  children.forEach((c) => c && (typeof c === 'string' ? n.appendChild(document.createTextNode(c)) : n.appendChild(c)));
  return n;
}

/* Turn a form value into the right JSON type: numbers, booleans, and lists
   (e.g. ["USD","EUR"]) become typed values; anything else stays a string. */
function coerceValue(raw) {
  const t = String(raw == null ? '' : raw).trim();
  if (t === '') return '';
  try { return JSON.parse(t); } catch (e) { /* not JSON */ }
  if (!isNaN(t)) return Number(t);
  return t;
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ------------------------------------------------------------- row builders */

function schemaRow(data = {}) {
  const typeSel = el('select', { name: 'col_type' },
    ...['Utf8', 'Int64', 'Float64', 'Boolean', 'Date', 'Datetime'].map((t) => el('option', { value: t }, t)));
  if (data.type) typeSel.value = data.type;
  const nameIn = el('input', { name: 'col_name', placeholder: 'column name (e.g. amount)' });
  if (data.name) nameIn.value = data.name;
  const nullable = el('input', { type: 'checkbox', name: 'nullable' });
  nullable.checked = data.nullable !== false;
  const row = el('div', { class: 'schema-row' },
    nameIn, typeSel,
    el('label', { class: 'check-inline' }, nullable, ' nullable'),
    el('button', { type: 'button', class: 'btn ghost small' }, 'Remove'));
  row.querySelector('button').onclick = () => row.remove();
  schemaRows.appendChild(row);
  return row;
}

function ruleRow(data = {}) {
  const checkSel = el('select', { name: 'check' },
    ...['NOT_NULL', 'GREATER_THAN', 'LESS_THAN', 'EQUAL', 'NOT_EQUAL', 'ALLOWED_VALUES', 'CONTAINS'].map((c) => el('option', { value: c }, c)));
  if (data.check) checkSel.value = data.check;
  const idIn = el('input', { name: 'rule_id', placeholder: 'rule_id (e.g. amount-positive)' });
  if (data.id) idIn.value = data.id;
  const colIn = el('input', { name: 'column', placeholder: 'column' });
  if (data.column) colIn.value = data.column;
  const valIn = el('input', { name: 'value', placeholder: 'value (or ["USD","EUR"] for a list)' });
  if (data.value !== undefined) valIn.value = data.value;
  const descIn = el('input', { name: 'description', placeholder: 'description' });
  if (data.description) descIn.value = data.description;
  const row = el('div', { class: 'rule-row' },
    idIn, checkSel, colIn, valIn, descIn,
    el('button', { type: 'button', class: 'btn ghost small' }, 'Remove'));
  row.querySelector('button').onclick = () => row.remove();
  ruleRows.appendChild(row);
  return row;
}

function batchRow(data = {}) {
  const checkSel = el('select', { name: 'check' },
    ...['MIN_ROW_COUNT', 'MAX_ROW_COUNT', 'SUM_GREATER_THAN', 'AVG_GREATER_THAN'].map((c) => el('option', { value: c }, c)));
  if (data.check) checkSel.value = data.check;
  const idIn = el('input', { name: 'rule_id', placeholder: 'rule_id' });
  if (data.id) idIn.value = data.id;
  const valIn = el('input', { name: 'value', placeholder: 'value (number/json)' });
  if (data.value !== undefined) valIn.value = data.value;
  const descIn = el('input', { name: 'description', placeholder: 'description' });
  if (data.description) descIn.value = data.description;
  const row = el('div', { class: 'batch-row' },
    idIn, checkSel, valIn, descIn,
    el('button', { type: 'button', class: 'btn ghost small' }, 'Remove'));
  row.querySelector('button').onclick = () => row.remove();
  batchRows.appendChild(row);
  return row;
}

addSchema.onclick = () => schemaRow();
addRule.onclick = () => ruleRow();
addBatch.onclick = () => batchRow();

/* Start with one of each so the builder is never a blank page. */
schemaRow();
ruleRow();

/* -------------------------------------------------------------- sample data */

function sampleRecords() {
  const rows = [];
  for (let i = 1; i <= 900; i++) {
    rows.push({ settlement_id: `SETTLE-${1000 + i}`, amount: Math.round((10 + i * 1.5) * 100) / 100, currency: i % 2 === 0 ? 'USD' : 'EUR' });
  }
  for (let i = 1; i <= 20; i++) rows.push({ settlement_id: `SETTLE-BAD-AMT-${i}`, amount: 'NOT_A_FLOAT', currency: 'USD' });
  for (let i = 21; i <= 40; i++) rows.push({ settlement_id: null, amount: 250.0, currency: 'GBP' });
  for (let i = 1; i <= 35; i++) rows.push({ settlement_id: `SETTLE-NEG-${i}`, amount: -50.0, currency: 'USD' });
  for (let i = 36; i <= 60; i++) rows.push({ settlement_id: `SETTLE-CURR-${i}`, amount: 120.0, currency: 'JPY' });
  return rows;
}

if (loadSample) {
  loadSample.addEventListener('click', () => {
    const jsonl = sampleRecords().map((r) => JSON.stringify(r)).join('\n') + '\n';
    sampleFile = new File([jsonl], 'vendor_settlements_sample.jsonl', { type: 'application/x-ndjson' });
    if (fileHint) fileHint.innerHTML = 'Using the generated <strong>vendor_settlements_sample.jsonl</strong> (1,000 rows, 100 seeded defects).';

    schemaRows.innerHTML = '';
    ruleRows.innerHTML = '';
    batchRows.innerHTML = '';
    schemaRow({ name: 'settlement_id', type: 'Utf8', nullable: false });
    schemaRow({ name: 'amount', type: 'Float64', nullable: false });
    schemaRow({ name: 'currency', type: 'Utf8', nullable: false });
    ruleRow({ id: 'amount-positive', check: 'GREATER_THAN', column: 'amount', value: '0', description: 'Amount must be strictly greater than 0' });
    ruleRow({ id: 'currency-settled', check: 'ALLOWED_VALUES', column: 'currency', value: '["USD","EUR","GBP"]', description: 'Currency must be an authorized settlement currency' });
    batchRow({ id: 'not-partial', check: 'MIN_ROW_COUNT', value: '3', description: 'Feed must contain at least 3 surviving records' });
    pgResult.innerHTML = '<p class="muted small">Sample loaded. Click <strong>Enforce the boundary</strong> to run it.</p>';
    pgRawWrap.hidden = true;
  });
}

/* --------------------------------------------------------------- submission */

function collectSchema() {
  return Array.from(schemaRows.querySelectorAll('.schema-row'))
    .map((r) => ({
      name: r.querySelector('[name=col_name]').value.trim(),
      polars_type: r.querySelector('[name=col_type]').value,
      nullable: r.querySelector('[name=nullable]').checked,
    }))
    .filter((s) => s.name);
}

function collectRules() {
  return Array.from(ruleRows.querySelectorAll('.rule-row'))
    .map((r) => ({
      rule_id: r.querySelector('[name=rule_id]').value.trim() || 'rule',
      check: r.querySelector('[name=check]').value,
      column: r.querySelector('[name=column]').value.trim(),
      value: coerceValue(r.querySelector('[name=value]').value),
      description: r.querySelector('[name=description]').value.trim() || undefined,
    }))
    .filter((r) => r.column);
}

function collectBatch() {
  return Array.from(batchRows.querySelectorAll('.batch-row'))
    .map((r) => ({
      rule_id: r.querySelector('[name=rule_id]').value.trim() || 'batch',
      check: r.querySelector('[name=check]').value,
      value: coerceValue(r.querySelector('[name=value]').value),
      description: r.querySelector('[name=description]').value.trim() || undefined,
    }))
    .filter((r) => r.check && r.value !== '');
}

/* --------------------------------------------------------- result rendering */

const STATUS_BADGE = {
  DELIVERED: 'badge--ok', LOADED: 'badge--ok',
  REJECTED: 'badge--warn', FAILED: 'badge--bad',
};

function statusBadge(status) {
  const s = String(status || 'UNKNOWN').toUpperCase();
  const cls = STATUS_BADGE[s] || 'badge--neutral';
  const glyph = cls === 'badge--ok' ? '●' : cls === 'badge--warn' ? '▲' : cls === 'badge--bad' ? '■' : '·';
  return `<span class="badge ${cls}">${glyph} ${esc(s)}</span>`;
}

function bars(obj) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return '<p class="muted small">None.</p>';
  const max = Math.max(...entries.map(([, v]) => Number(v) || 0), 1);
  return `<div class="breakdown">${entries.map(([k, v]) => {
    const n = Number(v) || 0;
    const pct = Math.max(4, Math.round((n / max) * 100));
    return `<div class="bar-row">
      <span class="bar-label" title="${esc(k)}">${esc(k)}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${pct}%"></span></span>
      <span class="bar-count">${n}</span>
    </div>`;
  }).join('')}</div>`;
}

function pathRow(label, value) {
  if (!value) return '';
  return `<div class="bar-row" style="grid-template-columns:190px 1fr"><span class="bar-label">${esc(label)}</span>
    <span class="muted small" style="word-break:break-all;font-family:var(--mono)">${esc(value)}</span></div>`;
}

function renderResult(data) {
  if (data && data.detail) {
    pgResult.innerHTML = `<div class="callout"><h3>The engine refused the request</h3><p>${esc(data.detail)}</p></div>`;
    pgOut.textContent = JSON.stringify(data, null, 2);
    pgRawWrap.hidden = false;
    return;
  }
  if (!data || typeof data !== 'object' || data.rows_read === undefined) {
    pgResult.innerHTML = '<div class="callout"><h3>Unexpected output</h3><p>See the raw engine output below.</p></div>';
    pgOut.textContent = JSON.stringify(data, null, 2);
    pgRawWrap.hidden = false;
    return;
  }

  const footingOk = data.footing_matched === true;
  const mode = data.execution_mode === 'out_of_core_streaming' ? 'out-of-core streaming' : 'in-memory vectorized';
  const dur = data.duration_seconds != null ? `${Number(data.duration_seconds).toFixed(2)}s` : '—';

  pgResult.innerHTML = `
    <div class="result-row">
      ${statusBadge(data.status)}
      <span class="badge badge--neutral">${esc(mode)}</span>
      <span class="badge badge--neutral">${esc(dur)}</span>
      ${footingOk ? '<span class="badge badge--ok">✓ footing matched</span>' : '<span class="badge badge--bad">✗ footing mismatch</span>'}
    </div>

    <div class="result-grid">
      <div class="tile"><div class="tile-value">${Number(data.rows_read || 0).toLocaleString()}</div><div class="tile-label">rows read</div></div>
      <div class="tile refused"><div class="tile-value">${Number(data.refused_structural || 0).toLocaleString()}</div><div class="tile-label">refused (structural)</div></div>
      <div class="tile held"><div class="tile-value">${Number(data.held_quality || 0).toLocaleString()}</div><div class="tile-label">held (rules)</div></div>
      <div class="tile delivered"><div class="tile-value">${Number(data.rows_delivered || 0).toLocaleString()}</div><div class="tile-label">delivered &amp; certified</div></div>
    </div>

    <div class="footing">
      <span class="term">read&nbsp;${Number(data.rows_read || 0).toLocaleString()}</span>
      <span class="op">=</span>
      <span class="term">refused&nbsp;${Number(data.refused_structural || 0).toLocaleString()}</span>
      <span class="op">+</span>
      <span class="term">held&nbsp;${Number(data.held_quality || 0).toLocaleString()}</span>
      <span class="op">+</span>
      <span class="term">delivered&nbsp;${Number(data.rows_delivered || 0).toLocaleString()}</span>
      <span class="eq">→ ${footingOk ? 'balances exactly' : 'does not balance'}</span>
    </div>

    <p class="subhead">Held by row rules</p>
    ${bars(data.rule_breakdown)}
    <p class="subhead" style="margin-top:14px">Refused at ingestion</p>
    ${bars(data.structural_reasons)}

    <p class="subhead" style="margin-top:20px">Artifacts</p>
    ${pathRow('target', data.target_path)}
    ${pathRow('quarantine', data.quarantine_path)}
    ${pathRow('certificate', data.audit_path)}
  `;

  pgOut.textContent = JSON.stringify(data, null, 2);
  pgRawWrap.hidden = false;
}

/* ------------------------------------------------------------------ submit */

pgForm.onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(pgForm);
  const schema = collectSchema();
  const rules = collectRules();
  const batch_rules = collectBatch();

  let file = fd.get('source');
  if (!file || !file.size) file = sampleFile;
  if (!file || !file.size) {
    pgResult.innerHTML = '<div class="callout"><h3>Add a data file</h3><p>Choose a CSV/JSONL file or click <strong>Load the 1,000-row sample</strong>.</p></div>';
    return;
  }
  if (!schema.length) {
    pgResult.innerHTML = '<div class="callout"><h3>Declare at least one schema column</h3><p>Tier-1 casting is the first gate.</p></div>';
    return;
  }

  const contract = {
    apiVersion: 'daas/v1',
    kind: 'DataProduct',
    metadata: {
      name: 'playground-contract', owner: 'demo', version: '1.0.0', status: 'ACTIVE',
      description: 'Contract generated in the Data Shell playground',
    },
    spec: {
      sources: [{ type: 'LOCAL', connectionRef: 'local', path: 'source', format: file.name.endsWith('.csv') ? 'csv' : 'jsonl', schema }],
      quality: { behavior: 'DROP_ROW', rules, batch_rules },
      transformation: { map: [], reduce: [] },
      // Declared so the CLI's --target/--audit overrides apply; the server
      // replaces these with a per-request temp location.
      delivery: {
        refreshSchedule: '0 * * * *',
        targets: [{ type: 'PARQUET', connectionRef: 'playground', path: 'curated/playground.parquet', mode: 'overwrite' }],
        quarantinePath: 'quarantine',
      },
    },
  };

  const cfd = new FormData();
  cfd.append('contract', new Blob([JSON.stringify(contract, null, 2)], { type: 'application/json' }), 'contract.json');
  cfd.append('source', file, file.name || 'source.jsonl');
  cfd.append('tenant', fd.get('tenant') || 'demo-tenant');

  const btn = pgForm.querySelector('button[type=submit]');
  btn.disabled = true;
  const original = btn.textContent;
  btn.innerHTML = '<span class="spinner"></span> Running…';
  pgResult.innerHTML = '<div class="loading"><span class="spinner"></span> Enforcing the boundary — casting, rules, reconciliation, publish…</div>';
  pgRawWrap.hidden = true;

  try {
    const res = await fetch('/api/enforce', { method: 'POST', body: cfd });
    const data = await res.json();
    renderResult(data);
  } catch (err) {
    pgResult.innerHTML = `<div class="callout"><h3>Request failed</h3><p>${esc(err.message)}</p></div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
};

pgForm.addEventListener('reset', () => {
  sampleFile = null;
  if (fileHint) fileHint.textContent = 'A small file (well under the 20 GiB ceiling). Uploaded to a temp dir per run.';
  pgRawWrap.hidden = true;
  setTimeout(() => {
    schemaRows.innerHTML = '';
    ruleRows.innerHTML = '';
    batchRows.innerHTML = '';
    schemaRow();
    ruleRow();
    pgResult.innerHTML = '';
  }, 0);
});
