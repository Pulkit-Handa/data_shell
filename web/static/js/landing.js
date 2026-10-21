/* Landing page behavior: code tabs, one-shot reveals, and a quiet scroll
   progress line. No scroll hijacking and no layout dependence — with
   reduced motion the content is fully visible and static. */

(function () {
  'use strict';

  // ---------------------------------------------------------------- tabs
  document.querySelectorAll('[data-tabs]').forEach(function (group) {
    var buttons = Array.prototype.slice.call(group.querySelectorAll('[data-tab]'));
    var panels = Array.prototype.slice.call(group.querySelectorAll('[data-panel]'));

    function activate(button) {
      buttons.forEach(function (candidate) {
        var active = candidate === button;
        candidate.classList.toggle('is-active', active);
        candidate.setAttribute('aria-selected', String(active));
        candidate.tabIndex = active ? 0 : -1;
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.dataset.panel !== button.dataset.tab;
      });
    }

    buttons.forEach(function (button, index) {
      button.tabIndex = button.classList.contains('is-active') ? 0 : -1;
      button.addEventListener('click', function () { activate(button); });
      button.addEventListener('keydown', function (event) {
        var delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
        if (!delta) return;
        event.preventDefault();
        var next = buttons[(index + delta + buttons.length) % buttons.length];
        next.focus();
        activate(next);
      });
    });
  });

  // -------------------------------------------------------------- reveal
  var reveals = Array.prototype.slice.call(document.querySelectorAll('.reveal'));
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  if (reduced || !('IntersectionObserver' in window)) {
    reveals.forEach(function (element) { element.classList.add('in'); });
  } else {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('in');
        observer.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -6% 0px', threshold: 0.06 });

    reveals.forEach(function (element) { observer.observe(element); });
  }

  // The reveal layer is live; the inline <head> failsafe can stand down.
  if (window.__dsRevealFailsafe) window.clearTimeout(window.__dsRevealFailsafe);

  // ------------------------------------------------------ scroll progress
  var bar = document.getElementById('progressBar');
  if (bar && !reduced) {
    var ticking = false;
    var update = function () {
      var doc = document.documentElement;
      var scrollable = doc.scrollHeight - window.innerHeight;
      var progress = scrollable > 0 ? window.scrollY / scrollable : 0;
      bar.style.transform = 'scaleX(' + Math.min(1, Math.max(0, progress)) + ')';
      ticking = false;
    };
    window.addEventListener('scroll', function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(update);
    }, { passive: true });
    window.addEventListener('resize', update, { passive: true });
    update();
  }
})();
