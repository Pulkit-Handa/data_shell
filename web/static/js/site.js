/* Site chrome: code tabs, and a reveal fallback for when GSAP is absent. */

(function () {
  // Tabs
  document.querySelectorAll('[data-tabs]').forEach((group) => {
    const buttons = group.querySelectorAll('[data-tab]');
    const panels = group.querySelectorAll('[data-panel]');
    buttons.forEach((btn) => {
      btn.addEventListener('click', () => {
        buttons.forEach((b) => b.classList.toggle('is-active', b === btn));
        panels.forEach((p) => (p.hidden = p.dataset.panel !== btn.dataset.tab));
      });
    });
  });

  // Reveal — hidden by CSS (.js .reveal), then shown either by the GSAP
  // motion layer (preferred) or, when GSAP is absent, a plain
  // IntersectionObserver fallback. Reduced-motion is handled in CSS.
  document.documentElement.classList.add('js');

  if (window.gsap) return;

  const els = document.querySelectorAll('.reveal');
  if (!('IntersectionObserver' in window)) {
    els.forEach((e) => e.classList.add('in'));
    return;
  }
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('in');
          io.unobserve(entry.target);
        }
      });
    },
    { rootMargin: '0px 0px -8% 0px', threshold: 0.05 }
  );
  els.forEach((e) => io.observe(e));
})();
