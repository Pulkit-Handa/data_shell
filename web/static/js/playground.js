const form = document.getElementById('enforceForm');
const out = document.getElementById('playgroundOutput');

if (form) {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const btn = form.querySelector('button');
    btn.disabled = true;
    btn.textContent = 'Running...';
    out.textContent = 'Running enforcement...';
    try {
      const res = await fetch('/api/enforce', { method: 'POST', body: fd });
      const data = await res.json();
      out.textContent = JSON.stringify(data, null, 2);
    } catch (e) {
      out.textContent = 'Error: ' + e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Run Enforcement';
    }
  });
}
