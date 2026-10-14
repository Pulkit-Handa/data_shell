const runBtn = document.getElementById('runDemo');
const out = document.getElementById('demoOutput');

if (runBtn) {
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    runBtn.textContent = 'Running...';
    out.textContent = 'Running datashell demo --json...';
    try {
      const res = await fetch('/api/demo/run', { method: 'POST' });
      const data = await res.json();
      out.textContent = JSON.stringify(data, null, 2);
    } catch (e) {
      out.textContent = 'Error: ' + e.message;
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = 'Run 60-Second Demo';
    }
  });
}
