/* GSAP + ScrollTrigger motion layer.
 *
 * Loaded only when the vendored GSAP is present and the visitor has not asked
 * for reduced motion. Everything degrades to fully visible, static content
 * otherwise — no scroll hijacking, no pinned sections, no layout dependence.
 *
 * What it adds:
 *   1. Hero entrance (once, on load)
 *   2. Scroll-triggered reveals — one clean staggered pass per group
 *   3. Subtle hero-console parallax (scrubbed, desktop only)
 *   4. Metrics count-up when the numbers table enters view
 *   5. A quiet 2px scroll-progress line
 */

(function () {
  const gsap = window.gsap;
  if (!gsap || !window.ScrollTrigger) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  // Safety: if the motion layer fails to initialize, no element may stay
  // hidden behind the CSS reveal state. Belt and braces for a measured-facts
  // page — the content must always end up readable.
  const unhideAll = () => {
    document.querySelectorAll('.reveal').forEach((el) => {
      el.style.transition = 'none';
      el.style.opacity = '1';
      el.style.transform = 'none';
    });
  };

  try {
    initMotion();
  } catch (err) {
    unhideAll();
  }

  function initMotion() {
    gsap.registerPlugin(ScrollTrigger);

    // The class-level CSS transition (used by the no-GSAP fallback path)
    // would fight GSAP's own tweening — drop it once GSAP owns these elements.
    document.querySelectorAll('.reveal').forEach((el) => (el.style.transition = 'none'));

    const isMobile = window.matchMedia('(max-width: 720px)').matches;

    /* 1 · Hero entrance — a single timeline, plays once. */
    const hero = document.querySelector('.hero');
    if (hero) {
      const tl = gsap.timeline({ defaults: { ease: 'power2.out' } });
      tl.from('.hero__eyebrow', { y: 14, opacity: 0, duration: 0.5 })
        .from('.hero h1', { y: 24, opacity: 0, duration: 0.7 }, '-=0.25')
        .from('.hero__lede', { y: 18, opacity: 0, duration: 0.55 }, '-=0.4')
        .from('.hero__actions', { y: 14, opacity: 0, duration: 0.45 }, '-=0.35')
        .from('.hero__meta', { y: 12, opacity: 0, duration: 0.4 }, '-=0.3')
        .from('.hero .console', { y: 30, opacity: 0, scale: 0.985, duration: 0.75 }, '-=0.4')
        .from('.specstrip__inner', { opacity: 0, y: 8, duration: 0.45 }, '-=0.4');
    }

    /* 2 · Scroll-triggered reveals — batched, staggered, fire once.
       The hidden start state comes from CSS (.js .reveal), so there is no
       flash before ScrollTrigger fires; GSAP only animates elements in. */
    ScrollTrigger.batch('.reveal', {
      start: 'top 88%',
      once: true,
      onEnter: (els) =>
        gsap.to(els, { opacity: 1, y: 0, duration: 0.7, stagger: 0.07, ease: 'power2.out', overwrite: true }),
    });

    /* 3 · Hero console parallax — a gentle drift as the hero scrolls away. */
    const heroConsole = document.querySelector('.hero .console');
    if (heroConsole && !isMobile) {
      gsap.to(heroConsole, {
        y: 48,
        ease: 'none',
        scrollTrigger: { trigger: '.hero', start: 'top top', end: 'bottom top', scrub: true },
      });
    }

    /* 4 · Metrics count-up — numbers settle into place when the table appears.
       The HTML carries the final values, so nothing depends on this running;
       it is purely presentation. */
    document.querySelectorAll('[data-count]').forEach((el) => {
      const target = Number(el.dataset.count || 0);
      const suffix = el.dataset.suffix || '';
      const state = { value: 0 };
      gsap.to(state, {
        value: target,
        duration: 1.4,
        ease: 'power1.out',
        scrollTrigger: { trigger: el, start: 'top 92%', once: true },
        onUpdate: () => {
          el.textContent = Math.round(state.value).toLocaleString() + suffix;
        },
      });
    });

    /* 5 · Scroll progress — a quiet line under the navigation. */
    const progressBar = document.querySelector('.scroll-progress__bar');
    if (progressBar) {
      gsap.to(progressBar, {
        scaleX: 1,
        ease: 'none',
        scrollTrigger: { start: 0, end: 'max', scrub: 0.3 },
      });
    }
  }
})();
