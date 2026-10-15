/* Live demo: calls the same engine the CLI runs and renders the proof. */

const runBtn = document.getElementById('runDemo');
const resultEl = document.getElementById('demoResult');
const outEl = document.getElementById('demoOutput');
const rawWrap = document.getElementById('demoRawWrap');

if (runBtn) {
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    const original = runBtn.textContent;
    runBtn.textContent = 'Running…';
    resultEl.innerHTML = '<div class="loading"><span class="spinner"></span> generating 1,000 settlement records and enforcing the contract…</div>';
    rawWrap.hidden = true;
    try {
      const res = await fetch('/api/demo/run', { method: 'POST' });
      const data = await res.json();
      window.DataShellUI.render(resultEl, data);
      outEl.textContent = JSON.stringify(data, null, 2);
      rawWrap.hidden = false;
    } catch (e) {
      resultEl.innerHTML = `<div class="run__head"><span class="status status--refuse">ERROR</span></div>
        <p class="run__meta" style="margin-top:14px;color:var(--c-text)">${e.message}</p>`;
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = original;
    }
  });
}
