# Decisions

Why this project is shaped the way it is. Each entry records what was decided, what it
rules out, and the evidence. Add to the bottom; do not rewrite history.

---

## 2026-09-02 — No subscription-credential routing, ever

**Decided:** the original idea, a router that accepts API-shaped calls and serves them
from a Claude Max or ChatGPT subscription, is abandoned. This library only ever uses
paid API credentials.

**Why:** all three major vendors prohibit it explicitly, and all three have enforced it
with account-level bans during 2026.

- Anthropic: OAuth "is intended exclusively for purchasers of Claude Free, Pro, Max, Team,
  and Enterprise subscription plans and is designed to support ordinary use of Claude Code
  and other native Anthropic applications", and developers "may not collect, store, or
  intermediate Claude.ai credentials or session tokens".
  <https://code.claude.com/docs/en/legal-and-compliance>
- Anthropic Agent SDK: "Anthropic does not allow third party developers to offer claude.ai
  login or rate limits for their products, including agents built on the Claude Agent SDK."
  <https://code.claude.com/docs/en/agent-sdk/overview>
- Anthropic Consumer ToS section 3(7): no access "through automated or non-human means,
  whether through a bot, script, or otherwise" except via an API key.
  <https://www.anthropic.com/legal/consumer-terms>
- Google: "using Gemini CLI oAuth with third-party software" named as policy-violating,
  with 403s and account bans.
  <https://github.com/google-gemini/gemini-cli/discussions/22970>
- OpenAI Terms of Use: prohibits "automatically or programmatically extract data or
  Output" and circumventing rate limits.
  <https://openai.com/policies/row-terms-of-use/>

**Rules out:** any future suggestion to reduce cost by using a subscription seat
programmatically. The saving is real and the risk is losing the account the rest of the
business runs on.

**What is permitted instead:** routing work by *type*. Interactive and agentic coding
happens inside Claude Code on the Max plan, which is ordinary individual use. Programmatic
and production traffic uses the API through this library.

---

## 2026-09-02 — A library, not a LiteLLM proxy

**Decided:** LiteLLM is used as a Python SDK inside consuming applications. No proxy
server, no Postgres, no dedicated host.

**Why:** the plan was a LiteLLM proxy on the existing Hetzner box alongside web-auditor.
A survey of that box on 2026-09-02 ruled it out:

- 3.7Gi total RAM, 2.6Gi available, **no swap**
- 38G disk, 26G used, 9.8G free (73%)
- 2 vCPU
- Postgres 17 runs as `web-auditor-db-1`, in the `web-auditor_default` bridge network

LiteLLM's own production guidance is 1 vCPU and 4Gi of memory per pod as both request and
limit, because "Prisma's query engine retains memory as a high-water mark" and going below
risks OOM kills under load. <https://docs.litellm.ai/docs/proxy/prod>

Their recommended floor for one container exceeds the whole server. With no swap, a spike
would OOM-kill by score, and the victim could as easily be a web-auditor worker as the
gateway. LiteLLM also writes a spend-log row per request, against a catalogue in the
hundreds of thousands, on a volume with 9.8G free that a live product depends on.

**Rules out:** co-tenancy on the web-auditor box. Revisit only with a separate host of at
least 8GB.

**Accepted losses:** no central dashboard, no per-project virtual keys, provider keys stay
in each consuming application's environment. Acceptable at two applications; revisit if
the number of consumers grows.

**Open:** the Python floor is set to 3.10 without confirming what web-auditor and the SEO
pipeline actually run. Check both and tighten the CI matrix to match.

---

## 2026-09-02 — Cost instrumentation: sourced rates, and null rather than wrong

**Decided:** `complete()` wraps `litellm.completion`, records one JSON object per call to
`LLM_GATEWAY_COST_LOG`, and reports no cost at all rather than a cost it cannot stand
behind.

**Rates are transcribed from provider documentation, with the URL and the date read.** No
figure in `pricing.py` comes from memory. Checked 2026-09-02:

- Anthropic, all Claude models including the caching multipliers.
  <https://platform.claude.com/docs/en/about-claude/pricing>
- OpenAI, input / cached input / output.
  <https://developers.openai.com/api/docs/pricing>
- OpenAI cache writes, documented as a multiplier rather than a rate: 1.25x uncached input
  on GPT-5.6 and later, no additional charge before that.
  <https://developers.openai.com/api/docs/guides/prompt-caching>
- Google Gemini, including context-cache read rates and the 200k tier split on 2.5 Pro.
  <https://ai.google.dev/gemini-api/docs/pricing>
- USD/GBP 1.3555, the rate for 2026-08-28, from the Federal Reserve H.10 release.
  <https://www.federalreserve.gov/releases/h10/current/>

Every rate that both our table and litellm's catalogue publish was compared and agreed
exactly. `test_the_table_agrees_with_litellms_independent_catalogue` keeps that true.

**litellm normalises cache tokens into `prompt_tokens`, and pricing that figure directly
would double-charge every cached token.** Anthropic's API reports `input_tokens` excluding
cache reads and writes; litellm adds them back in to match OpenAI's convention
(`litellm/llms/anthropic/chat/transformation.py`). Billable uncached input is therefore
`prompt_tokens - cached_tokens - cache_creation_tokens`. On a cache-heavy workload — which
is the point of caching — the naive reading would overstate spend by a large multiple, in a
direction that looks entirely plausible. `test_cache_tokens_are_not_billed_twice` guards it.

**Where the bill cannot be computed honestly, the cost is `null` and a caveat names why.**
That covers an unknown model, a rate the provider does not publish, 1-hour cache writes,
fast mode, non-global `inference_geo`, non-standard service tiers, prompts past a pricing
tier, and streaming. A null is visible and countable in the log. A base-rate figure quietly
standing in for a discounted or premium one is not, and this library only has value if its
numbers are believed.

**Model matching is exact, plus release-date suffix stripping, and nothing looser.** A
future `claude-opus-5-1` reports no price rather than inheriting Opus 5's rates.

**The FX rate is overridable and always recorded.** `LLM_GATEWAY_USD_GBP_RATE` overrides
the built-in default; whichever was used is written into every record with its provenance,
so GBP can be recomputed and a stale-rate run identified afterwards. No network lookup: a
cost logger that makes its own HTTP calls is a new failure mode inside someone else's
process, for no gain.

**Rules out:** adding a rate without a source, and returning a base-rate cost when a known
modifier is active.

**Accepted losses:** Batch API, fast mode, long-context tiers and streaming are unmeasured
for now. Cross-process append interleaving is not guaranteed on Windows; give separate
consumers separate log files.

**Open:** whether to price the modifiers above rather than null them. Worth doing once
there is evidence any consumer actually uses them.

---

## 2026-09-02 — Python floor raised to 3.11, because litellm cannot import on 3.10

**Decided:** `requires-python = ">=3.11"`, CI matrix `["3.11", "3.13"]`. This closes the
open question left by the entry above, which set the floor to 3.10 without checking what
the dependency or the consumers actually needed.

**Why:** litellm 1.99.0 cannot be imported on Python 3.10 at all. `litellm/__init__.py`
imports, unconditionally, a module that does `from typing import ... NotRequired`, which is
3.11 and later:

```
litellm/llms/anthropic/experimental_pass_through/context_management/editors/__init__.py:2
    from typing import TYPE_CHECKING, Any, Final, Literal, NotRequired, ...
ImportError: cannot import name 'NotRequired' from 'typing'
```

This is an upstream packaging bug rather than a choice on our part. litellm declares
`Requires-Python: >=3.10, <3.15`, so pip installs it on 3.10 without complaint and it then
fails at import. Every one of our 83 tests errored on the 3.10 CI job for that reason and
that reason alone; 3.12 was green on the same commit.

**Why 3.11 and not 3.13:** 3.11 is the lowest version that can actually work, so it is the
honest floor and the right thing to test. 3.13 is added to the matrix because it is what the
consumers run — web-auditor pins `>=3.13` in its `pyproject.toml`, its CI and its Dockerfile.
The release job now builds on 3.13 to match the top of the matrix.

**Still open:** the moto SEO pipeline's Python version has not been checked; it was not
available from the machine this was decided on. If it runs 3.10, it cannot use this library
until it moves, and the constraint is litellm's rather than ours.
