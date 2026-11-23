---
description: Security gaps a red-team pass actually found; loaded when backend or web source is opened.
paths: ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.php"]
---

# Security-Sensitive Code

Derived from a red-team pass on web-auditor (2026-07-21) that found a working app with no deliberate security defects still accumulating this many gaps by default. Apply these proactively on any backend/web project, not just after being asked to review one:
- Any outbound request built from user-controlled input (a submitted URL, a webhook payload, a redirect target) must validate the resolved IP at actual connect time, not just the request URL string — a hostname-only check misses redirects and DNS rebinding. Block loopback/private/link-local/multicast/metadata ranges by default.
- A metered/paid external API call (LLM, SMS, a priced data API) needs its spend cap checked *before* the call, not logged after it. If a budget setting exists in an admin UI, confirm it actually gates the call — a control that's visible but unenforced is worse than no control, since it reads as handled.
- State-changing actions must never ride on a GET request, even a "just show a confirmation" link — a mail-security-gateway prefetch or link-preview will trigger it silently. Split into GET (show) / POST (apply).
- One-time-use credentials (magic links, password-reset tokens, email-verification links) must be tracked as consumed, not just time-limited — a valid-but-already-used token still passes a bare expiry check.
- A credential-rotation event (password change, admin-forced logout) should invalidate existing sessions, not just block new logins with the old credential.
- Never apply a privileged effect (account merge, consent recorded, cross-account write) on the strength of unverified client input (a typed email, a form field) — require proof of the claimed identity first (a signed link actually clicked, a side-channel confirmation).
- Any user-controlled string that reaches an HTTP response header (a filename, a redirect target) needs validating where it's *accepted*, not hoped-safe where it's later used.
- Fetching a remote resource whose size isn't controlled by us needs a streamed byte cap, not a truncation applied after the full body is already in memory.
- When adding a security control (rate limit, auth check, input validation) to one route, grep for every other route of the same shape and apply it there too — a check present on 5 of 6 similar endpoints is a gap, not a win.
- When a pipeline's upstream step returns nothing or fails, the downstream layer needs an explicit "no data" path — don't let a scoring/presentation layer silently produce plausible-looking output from empty input.
