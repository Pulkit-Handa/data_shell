/* Live demo: three scenarios, each a real CLI run of the engine.
   No canned output — every panel comes from the subprocess the visitor started. */

(function () {
  const runBtn = document.getElementById('runDemo');
  const resultEl = document.getElementById('demoResult');
  const outEl = document.getElementById('demoOutput');
  const rawWrap = document.getElementById('demoRawWrap');
  const noteText = document.getElementById('demoNoteText');
  const tabs = Array.from(document.querySelectorAll('.demo__sc'));
  if (!runBtn || !tabs.length) return;

  const NOTES = {
    clean:
      'With a clean feed, delivery happens only after the footing equation balances — and a signed certificate binds the artifact, so downstream never has to trust that “someone checked.”',
    quarantine:
      'Without the boundary, the 40 unparseable and 60 rule-breaking rows land downstream or get silently coerced — someone finds out later. With it: refused rows never enter the delivered file, held rows sit in quarantine with the exact rule that held them, and the footing still balances.',
    tamper:
      'Without the boundary, a corrupted delivered file looks identical to a good one until a downstream job fails or money moves. With it: one flipped byte fails offline verification; restoring the authentic bytes recovers the verdict.',
  };
  const LABELS = {
    clean: 'Scenario A — clean feed',
    quarantine: 'Scenario B — defective feed',
    tamper: 'Scenario C — tamper & recovery',
  };

  let active = tabs[0];
  const setActive = (tab) => {
    active = tab;
    tabs.forEach((t) => {
      const on = t === tab;
      t.classList.toggle('is-active', on);
      t.setAttribute('aria-selected', String(on));
    });
    if (noteText) noteText.textContent = NOTES[tab.dataset.focus] || NOTES.clean;
  };
  tabs.forEach((t) => t.addEventListener('click', () => setActive(t)));
  // Arrow-key navigation between tabs (tablist convention).
  const bar = tabs[0].parentElement;
  if (bar) {
    bar.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const i = tabs.indexOf(active);
      const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      setActive(next);
      next.focus();
    });
  }

  const loading = (label) =>
    `<div class="loading"><span class="spinner"></span> ${label}: running the real engine in a fresh subprocess — typically 2–5 s…</div>`;

  runBtn.addEventListener('click', async () => {
    const scenario = active.dataset.scenario;
    const focus = active.dataset.focus;
    runBtn.disabled = true;
    runBtn.textContent = 'Running…';
    resultEl.innerHTML = loading(LABELS[focus] || 'Scenario');
    rawWrap.hidden = true;
    try {
      const res = await fetch(`/api/demo/run?scenario=${encodeURIComponent(scenario)}`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        resultEl.innerHTML = `<div class="run__head"><span class="status status--refuse">ERROR</span></div>
          <p class="run__meta" style="margin-top:14px;color:var(--c-text)">${(data && data.detail) || 'The demo failed.'}</p>`;
        return;
      }
      window.DataShellUI.render(resultEl, data);
      resultEl.classList.remove('focus-quarantine', 'focus-tamper');
      if (focus !== 'clean') resultEl.classList.add(`focus-${focus}`);
      outEl.textContent = JSON.stringify(data, null, 2);
      rawWrap.hidden = false;
    } catch (e) {
      resultEl.innerHTML = `<div class="run__head"><span class="status status--refuse">ERROR</span></div>
        <p class="run__meta" style="margin-top:14px;color:var(--c-text)">${e.message}</p>`;
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = 'Run scenario';
    }
  });
})();
