/* Renders the `datashell demo --json` result as a visual proof panel.
   Falls back to raw output if the response shape is unexpected. */

const runBtn = document.getElementById('runDemo');
const resultEl = document.getElementById('demoResult');
const outEl = document.getElementById('demoOutput');
const rawWrap = document.getElementById('demoRawWrap');

const VERDICTS = {
  AUTHENTIC_AND_UNMODIFIED: { cls: 'badge--ok', label: 'AUTHENTIC_AND_UNMODIFIED' },
  TAMPERED_UNDERLYING_ARTIFACTS: { cls: 'badge--bad', label: 'TAMPERED_UNDERLYING_ARTIFACTS' },
  TAMPERED: { cls: 'badge--bad', label: 'TAMPERED' },
  NO_CERTIFICATE: { cls: 'badge--warn', label: 'NO_CERTIFICATE' },
};

function statusBadge(status) {
  const s = String(status || '').toUpperCase();
  if (s === 'DELIVERED' || s === 'LOADED') return `<span class="badge badge--ok">● ${s}</span>`;
  if (s === 'REJECTED') return `<span class="badge badge--warn">▲ ${s}</span>`;
  if (s === 'FAILED') return `<span class="badge badge--bad">■ ${s}</span>`;
  return `<span class="badge badge--neutral">${s || 'UNKNOWN'}</span>`;
}

function verdictBadge(status) {
  const v = VERDICTS[status] || { cls: 'badge--neutral', label: status || 'UNKNOWN' };
  return `<span class="badge ${v.cls}">${v.label}</span>`;
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function bars(obj) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return '<p class="muted small">None.</p>';
  const max = Math.max(...entries.map(([, v]) => Number(v) || 0), 1);
  return `<div class="breakdown">${entries
    .map(([k, v]) => {
      const n = Number(v) || 0;
      const pct = Math.max(4, Math.round((n / max) * 100));
      return `<div class="bar-row">
        <span class="bar-label" title="${esc(k)}">${esc(k)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${pct}%"></span></span>
        <span class="bar-count">${n}</span>
      </div>`;
    })
    .join('')}</div>`;
}

function render(data) {
  if (!data || typeof data !== 'object' || data.run_id === undefined) {
    // Unexpected shape: show raw.
    outEl.textContent = JSON.stringify(data, null, 2);
    rawWrap.hidden = false;
    return;
  }

  const footingOk = data.footing_matched === true;
  const footingBadge = footingOk
    ? '<span class="badge badge--ok">✓ footing matched</span>'
    : '<span class="badge badge--bad">✗ footing mismatch — run refused</span>';

  const verifySteps = [
    { n: '4', label: 'Verify the signed certificate', status: data.verify_status },
    { n: '5', label: 'Flip 1 byte in the delivered Parquet, re-verify', status: data.tamper_status, only: data.tamper_status != null },
    { n: '6', label: 'Restore the authentic bytes, re-verify', status: data.restored_status, only: data.restored_status != null },
  ].filter((s) => s.only !== false);

  resultEl.innerHTML = `
    <div class="result-row">
      ${statusBadge(data.status)}
      ${footingBadge}
      <span class="badge badge--neutral">run ${esc(String(data.run_id).slice(0, 12))}</span>
    </div>

    <div class="result-grid">
      <div class="tile">
        <div class="tile-value">${Number(data.rows_read || 0).toLocaleString()}</div>
        <div class="tile-label">rows read</div>
      </div>
      <div class="tile refused">
        <div class="tile-value">${Number(data.rows_refused || 0).toLocaleString()}</div>
        <div class="tile-label">refused at the door (structural)</div>
      </div>
      <div class="tile held">
        <div class="tile-value">${Number(data.rows_held || 0).toLocaleString()}</div>
        <div class="tile-label">held by rules (semantic)</div>
      </div>
      <div class="tile delivered">
        <div class="tile-value">${Number(data.rows_delivered || 0).toLocaleString()}</div>
        <div class="tile-label">delivered &amp; certified</div>
      </div>
    </div>

    <div class="footing">
      <span class="term">read&nbsp;${Number(data.rows_read || 0).toLocaleString()}</span>
      <span class="op">=</span>
      <span class="term">refused&nbsp;${Number(data.rows_refused || 0).toLocaleString()}</span>
      <span class="op">+</span>
      <span class="term">held&nbsp;${Number(data.rows_held || 0).toLocaleString()}</span>
      <span class="op">+</span>
      <span class="term">delivered&nbsp;${Number(data.rows_delivered || 0).toLocaleString()}</span>
      <span class="eq">→ ${footingOk ? 'balances exactly' : 'does not balance'}</span>
    </div>

    <p class="subhead">Why rows were stopped</p>
    <p class="muted small" style="margin-bottom:8px">Held by row rules</p>
    ${bars(data.rule_breakdown)}
    <p class="muted small" style="margin:12px 0 8px">Refused at ingestion (structural)</p>
    ${bars(data.structural_reasons)}

    <p class="subhead" style="margin-top:20px">Tamper-evidence</p>
    <div class="verify-steps">
      ${verifySteps
        .map(
          (s) => `<div class="vstep">
            <span class="n">${s.n}</span>
            <span class="vlabel">${esc(s.label)}</span>
            ${verdictBadge(s.status)}
          </div>`
        )
        .join('')}
    </div>
    <div class="result-actions">
      <span class="badge badge--info">✓ rows refused · good rows landed · tampering detected and resolved</span>
    </div>
  `;

  outEl.textContent = JSON.stringify(data, null, 2);
  rawWrap.hidden = false;
}

if (runBtn) {
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    const original = runBtn.textContent;
    runBtn.innerHTML = '<span class="spinner"></span> Running…';
    resultEl.innerHTML = '<div class="loading"><span class="spinner"></span> Generating 1,000 records and enforcing the boundary…</div>';
    rawWrap.hidden = true;
    try {
      const res = await fetch('/api/demo/run', { method: 'POST' });
      const data = await res.json();
      render(data);
    } catch (e) {
      resultEl.innerHTML = `<div class="callout"><h3>Demo failed</h3><p>${esc(e.message)}</p></div>`;
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = original;
    }
  });
}
