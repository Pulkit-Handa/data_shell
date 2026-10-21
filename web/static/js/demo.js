/* Live demo: three scenarios, each a real CLI run of the engine.
   Nothing is canned — every panel is rendered from the subprocess the visitor
   started. Failures are surfaced as failures; no fallback results are faked. */

(function () {
  'use strict';

  var runBtn = document.getElementById('runDemo');
  var resetBtn = document.getElementById('resetDemo');
  var resultEl = document.getElementById('demoResult');
  var outEl = document.getElementById('demoOutput');
  var rawWrap = document.getElementById('demoRawWrap');
  var noteText = document.getElementById('demoNoteText');
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.demo__sc'));
  if (!runBtn || !resultEl || !tabs.length) return;

  var IDLE =
    '<p class="console-label" style="margin:0">awaiting run — select a scenario and run it; typically 2–5 s per run</p>';

  var NOTES = {
    clean:
      'With a clean feed, delivery happens only after the footing equation balances — and a signed certificate binds the artifact, so downstream never has to trust that “someone checked.”',
    quarantine:
      'Without the boundary, the 40 unparseable and 60 rule-breaking rows land downstream or get silently coerced — someone finds out later. With it: refused rows never enter the delivered file, held rows sit in quarantine with the exact rule that held them, and the footing still balances.',
    tamper:
      'This runs the same seeded-defect walkthrough and focuses on its integrity evidence. Without the boundary, a corrupted delivered file looks identical to a good one until a downstream job fails or money moves. With it: one flipped byte fails offline verification; restoring the authentic bytes recovers the verdict.'
  };
  var LABELS = {
    clean: 'Scenario A — clean feed',
    quarantine: 'Scenario B — defective feed',
    tamper: 'Scenario C — integrity verification'
  };

  var active = tabs[0];

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (character) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
    });
  }

  function setActive(tab) {
    active = tab;
    tabs.forEach(function (candidate) {
      var on = candidate === tab;
      candidate.classList.toggle('is-active', on);
      candidate.setAttribute('aria-pressed', String(on));
    });
    if (noteText) noteText.textContent = NOTES[tab.dataset.focus] || NOTES.clean;
  }

  setActive(active);
  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () { setActive(tab); });
  });

  function setBusy(busy) {
    runBtn.disabled = busy;
    runBtn.textContent = busy ? 'Running…' : 'Run scenario';
    resetBtn.disabled = busy || rawWrap.hidden;
    resultEl.setAttribute('aria-busy', String(busy));
  }

  function showError(status, detail, hint) {
    var title = 'ERROR';
    if (status === 422) title = 'NOT ACCEPTED';
    else if (status === 429) title = 'RATE LIMITED';
    else if (status === 503) title = 'AT CAPACITY';
    else if (status === 504) title = 'TIMED OUT';

    var tone = status === 422 ? 'status--hold' : status === 429 || status === 503 ? 'status--hold' : 'status--refuse';
    var message = detail || 'The demo could not complete this run.';
    var hintHtml = hint ? '<p class="console-label" style="margin:14px 0 6px">next step</p><p class="run__meta" style="color:var(--console-text)">' + esc(hint) + '</p>' : '';

    resultEl.classList.remove('focus-quarantine', 'focus-tamper');
    resultEl.innerHTML =
      '<div class="run__head"><span class="status ' + tone + '">' + title + '</span>' +
      (status ? '<span class="run__meta">HTTP ' + esc(status) + '</span>' : '') +
      '</div>' +
      '<p class="console-label" style="margin:16px 0 6px">detail</p>' +
      '<p class="run__meta" style="color:var(--console-text)">' + esc(message) + '</p>' +
      hintHtml;
  }

  function hintFor(status) {
    if (status === 429) return 'The public demo is rate-limited. Wait a few seconds, then run the scenario again.';
    if (status === 503) return 'The demo is at its concurrent-run limit. Retry shortly.';
    if (status === 504) return 'The engine run exceeded its time budget. Retry, or evaluate the same contract locally with the quickstart.';
    if (status === 422) return 'The gateway rejected the request before or during enforcement — no rows were published. Read the detail above.';
    return 'Retry the run. If it keeps failing, the public demo service may be unavailable; the CLI quickstart runs the same engine locally.';
  }

  runBtn.addEventListener('click', async function () {
    var scenario = active.dataset.scenario;
    var focus = active.dataset.focus;
    setBusy(true);
    resultEl.innerHTML =
      '<div class="loading"><span class="spinner" aria-hidden="true"></span> ' +
      esc(LABELS[focus] || 'Scenario') +
      ': running the real engine in a fresh subprocess — typically 2–5 s…</div>';
    rawWrap.hidden = true;

    try {
      var response = await fetch('/api/demo/run?scenario=' + encodeURIComponent(scenario), {
        method: 'POST',
        headers: { Accept: 'application/json' }
      });

      var data = null;
      try { data = await response.json(); } catch (err) { data = null; }

      if (!response.ok) {
        showError(response.status, data && data.detail, hintFor(response.status));
        return;
      }

      if (!data || typeof data !== 'object') {
        showError(0, 'The demo service returned an unreadable response.', hintFor(0));
        return;
      }

      if (!window.DataShellUI || typeof window.DataShellUI.render !== 'function') {
        showError(0, 'The result renderer failed to load. Reload the page and try again.', hintFor(0));
        return;
      }

      window.DataShellUI.render(resultEl, data);
      resultEl.classList.remove('focus-quarantine', 'focus-tamper');
      if (focus !== 'clean') resultEl.classList.add('focus-' + focus);
      outEl.textContent = JSON.stringify(data, null, 2);
      rawWrap.hidden = false;
      if (noteText) noteText.textContent = NOTES[focus] || NOTES.clean;
    } catch (err) {
      showError(
        0,
        'The demo service could not be reached from this browser.',
        'Check your connection and retry. The CLI quickstart runs the same engine locally if the hosted demo is unavailable.'
      );
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = 'Run scenario';
      resetBtn.disabled = rawWrap.hidden;
      resultEl.setAttribute('aria-busy', 'false');
    }
  });

  resetBtn.addEventListener('click', function () {
    resultEl.innerHTML = IDLE;
    resultEl.classList.remove('focus-quarantine', 'focus-tamper');
    resultEl.setAttribute('aria-busy', 'false');
    outEl.textContent = '';
    rawWrap.hidden = true;
    rawWrap.open = false;
    resetBtn.disabled = true;
    if (noteText) noteText.textContent = NOTES[active.dataset.focus] || NOTES.clean;
  });
})();
