# Vendored front-end libraries

Kept locally so the site stays self-contained: no third-party runtime
requests, works offline, nothing is fetched from a CDN on page load.

| File | Library | Version | Source |
| --- | --- | --- | --- |
| `gsap.min.js` | GSAP core | 3.12.5 | `npm/gsap@3.12.5/dist/gsap.min.js` |
| `ScrollTrigger.min.js` | GSAP scroll plugin | 3.12.5 | `npm/gsap@3.12.5/dist/ScrollTrigger.min.js` |

Both files carry their upstream license header (GreenSock standard
license for GSAP 3.x: free to use, including commercially, per
<https://gsap.com>). Update in place — bump the version column, drop the
new minified files over these, and re-run the site smoke test
(`node --check web/static/js/*.js` plus the page checks in the release
gates).

Consumed only by `web/static/js/motion.js`. If GSAP fails to load or the
visitor prefers reduced motion, the page renders fully without it — see
the reveal fallback in `web/static/js/site.js`.
