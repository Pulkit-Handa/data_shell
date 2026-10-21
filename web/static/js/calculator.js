/* Local-only operational-burden calculator.
   Every input is visitor-supplied; nothing is transmitted or stored.
   The output estimates the CURRENT burden and a visitor-chosen scenario —
   it does not claim a measured Data Shell benefit. */

(function () {
  const fields = {
    deliveries: document.getElementById('calcDeliveries'),
    hours: document.getElementById('calcHours'),
    people: document.getElementById('calcPeople'),
    rate: document.getElementById('calcRate'),
  };
  const sliders = {
    problemRate: document.getElementById('calcProblemRate'),
    reduction: document.getElementById('calcReduction'),
  };
  if (!fields.deliveries) return;

  const money = (v) => '$' + Math.round(v).toLocaleString();
  const num = (el) => Math.max(0, Number(el.value) || 0);
  const out = (id) => document.getElementById(id);

  function update() {
    const deliveries = num(fields.deliveries);
    const rate = num(fields.rate);
    const hours = num(fields.hours);
    const people = num(fields.people);
    const problemRate = num(sliders.problemRate);
    const reduction = num(sliders.reduction);

    out('outDeliveries').textContent = String(deliveries);
    out('outProblemRate').textContent = problemRate + '%';
    out('outHours').textContent = hours.toFixed(1);
    out('outPeople').textContent = people.toFixed(1);
    out('outRate').textContent = money(rate);
    out('outReduction').textContent = reduction + '%';

    // Transparent formula: deliveries/week × 52 × defect share × hours × people.
    const annualHours = deliveries * 52 * (problemRate / 100) * hours * people;
    const burden = annualHours * rate;
    const savings = burden * (reduction / 100);

    out('figHours').textContent = Math.round(annualHours).toLocaleString();
    out('figBurden').textContent = money(burden);
    out('figSavings').textContent = money(savings);
  }

  Object.values(fields).forEach((el) => el.addEventListener('input', update));
  Object.values(sliders).forEach((el) => el.addEventListener('input', update));
  update();
})();
