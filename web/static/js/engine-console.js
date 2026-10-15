/* Shared engine-console renderer.
   Turns `datashell demo --json` and `run --json` output into the product
   surface. Exposed as window.DataShellUI. */

(function () {
  const esc = (v) =>
    String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const num = (v) => Number(v || 0).toLocaleString();

  function statusClass(s) {
    const u = String(s || '').toUpperCase();
    if (u === 'DELIVERED' || u === 'LOADED') return 'status status--pass';
    if (u === 'REJECTED') return 'status status--hold';
    if (u === 'FAILED') return 'status status--refuse';
    return 'status status--neutral';
  }

  function verdictClass(s) {
    const u = String(s || '').toUpperCase();
    if (u.startsWith('AUTHENTIC')) return 'status status--pass';
    if (u.startsWith('TAMPER')) return 'status status--refuse';
    if (u.startsWith('NO_CERT')) return 'status status--hold';
    return 'status status--neutral';
  }

  function bars(obj, variant) {
    const entries = Object.entries(obj || {});
    if (!entries.length) return '<p class="console-label" style="margin:0">none</p>';
    const max = Math.max(...entries.map(([, v]) => Number(v) || 0), 1);
    return `<div class="bars ${variant === 'refuse' ? 'bars--refuse' : ''}">${entries
      .map(([k, v]) => {
        const n = Number(v) || 0;
        const pct = Math.max(3, Math.round((n / max) * 100));
        return `<div class="bar">
          <span class="bar__label" title="${esc(k)}">${esc(k)}</span>
          <span class="bar__track"><span class="bar__fill" style="width:${pct}%"></span></span>
          <span class="bar__n">${n}</span>
        </div>`;
      })
      .join('')}</div>`;
  }

  function render(target, data) {
    if (!target) return;

    if (data && data.detail) {
      target.innerHTML = `<div class="run__head"><span class="status status--refuse">REFUSED</span>
        <span class="run__meta">the engine rejected the request</span></div>
        <p class="console-label" style="margin:16px 0 6px">detail</p>
        <p class="run__meta" style="color:var(--c-text)">${esc(data.detail)}</p>`;
      return;
    }

    if (!data || typeof data !== 'object' || (data.rows_read === undefined && data.run_id === undefined)) {
      target.innerHTML = '<p class="console-label" style="margin:0">unexpected output</p>';
      return;
    }

    const isDemo = data.verify_status !== undefined || data.rows_refused !== undefined;
    const read = data.rows_read || 0;
    const refused = data.rows_refused !== undefined ? data.rows_refused : data.refused_structural || 0;
    const held = data.rows_held !== undefined ? data.rows_held : data.held_quality || 0;
    const delivered = data.rows_delivered || 0;
    const ok = data.footing_matched === true;
    const runId = String(data.run_id || '');
    const shortId = runId.length > 16 ? runId.slice(0, 16) : runId;

    const mode = data.execution_mode
      ? (data.execution_mode === 'out_of_core_streaming' ? 'out-of-core streaming' : 'in-memory')
      : 'streaming';
    const dur = data.duration_seconds != null ? `${Number(data.duration_seconds).toFixed(2)}s` : null;

    let html = `
      <div class="run__head">
        <span class="${statusClass(data.status)}">${esc(String(data.status || 'UNKNOWN').toUpperCase())}</span>
        <span class="run__meta">${esc(shortId)}</span>
        <span class="run__meta">${esc(mode)}${dur ? ' · ' + esc(dur) : ''}</span>
        ${ok ? '<span class="status status--pass">footing matched</span>' : '<span class="status status--refuse">footing mismatch</span>'}
      </div>

      <div class="run__metrics">
        <div class="stat"><span class="stat__v">${num(read)}</span><span class="stat__l">rows read</span></div>
        <div class="stat stat--refuse"><span class="stat__v">${num(refused)}</span><span class="stat__l">refused</span></div>
        <div class="stat stat--hold"><span class="stat__v">${num(held)}</span><span class="stat__l">held</span></div>
        <div class="stat stat--pass"><span class="stat__v">${num(delivered)}</span><span class="stat__l">delivered</span></div>
      </div>

      <div class="equation">
        <span>read <b>${num(read)}</b></span><span>=</span>
        <span>refused <b>${num(refused)}</b></span><span>+</span>
        <span>held <b>${num(held)}</b></span><span>+</span>
        <span>delivered <b>${num(delivered)}</b></span>
        <span class="${ok ? 'ok' : 'bad'}">→ ${ok ? 'balances exactly' : 'does not balance'}</span>
      </div>

      <div class="run__cols">
        <div>
          <p class="console-label">held by row rule</p>
          ${bars(data.rule_breakdown)}
        </div>
        <div>
          <p class="console-label">refused at ingestion</p>
          ${bars(data.structural_reasons, 'refuse')}
        </div>
      </div>
    `;

    if (isDemo) {
      const steps = [
        { n: '01', label: 'Verify the signed certificate', status: data.verify_status, show: data.verify_status != null },
        { n: '02', label: 'Flip one byte in the delivered Parquet, re-verify', status: data.tamper_status, show: data.tamper_status != null },
        { n: '03', label: 'Restore the authentic bytes, re-verify', status: data.restored_status, show: data.restored_status != null },
      ].filter((s) => s.show);
      if (steps.length) {
        html += `<p class="console-label">tamper-evidence</p>
        <ol class="vsteps">${steps
          .map(
            (s) => `<li class="vstep">
              <span class="vstep__n">${s.n}</span>
              <span class="vstep__label">${esc(s.label)}</span>
              <span class="${verdictClass(s.status)}">${esc(s.status)}</span>
            </li>`
          )
          .join('')}</ol>`;
      }
    }

    const paths = [
      ['target', data.target_path],
      ['quarantine', data.quarantine_path],
      ['certificate', data.audit_path],
    ].filter(([, v]) => v);
    if (paths.length) {
      html += `<div class="run__paths">${paths
        .map(([k, v]) => `<div class="path"><span class="path__k">${esc(k)}</span><span class="path__v">${esc(v)}</span></div>`)
        .join('')}</div>`;
    }

    target.innerHTML = html;
  }

  window.DataShellUI = { render };
})();
