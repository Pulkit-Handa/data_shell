# Security Policy

## Static security gates

Two automated gates run in CI on every push (see `.github/workflows/ci.yml`):

* **bandit** — static security lint with a **zero-findings policy**. Every
  suppression is an inline `# nosec BXXX` comment with a written reason,
  reviewer-visible in the diff. The only suppressed families are reviewed SQL
  builders (B608) that bind values through the driver (`?` placeholders),
  validate identifiers through a quote/enum gate, and are covered by exact
  compile/behavior tests; and benign identifier labels (`B105`), cleanup
  bookkeeping (`B110`), and a tolerant point-in-time parser (`B112`).
* **pip-audit** — audits `requirements.lock`, `requirements-dev.lock`, and the
  generated Python client's `sdks/python-client/requirements.txt` against the
  OSV database. Any known GHSA/OSV vulnerability fails the build.

## Supported Versions

The following versions of Data Shell receive security updates:

| Version | Supported |
| --- | --- |
| 2.0.1 | ✅ Supported |
| 2.0.0 | ✅ Supported |
| < 2.0.0 | ❌ No longer supported |

## Reporting a Vulnerability

If you discover a security vulnerability in Data Shell, please report it responsibly.

**Preferred method:** Email [security@datashell.dev](mailto:security@datashell.dev)

**What to include:**
- Description of the vulnerability and potential impact
- Steps to reproduce the issue
- Affected version(s)
- Any suggested mitigation or fix (optional)

**Response timeline:**
- We will acknowledge receipt within 48 hours
- We will provide an initial assessment within 7 days
- We aim to release a fix for confirmed vulnerabilities within 30 days

**Guidelines:**
- Do not disclose the vulnerability publicly until we have issued a fix or coordinated disclosure
- Do not exploit the vulnerability beyond what is necessary to demonstrate it
- Avoid accessing or modifying data that does not belong to you

Thank you for helping keep Data Shell and its users safe.
