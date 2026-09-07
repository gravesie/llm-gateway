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

---

## 2026-09-02 — The spend ceiling: faults fail open, decisions do not

**Decided:** `LLM_GATEWAY_MONTHLY_BUDGET_GBP` is enforced in `budget.py`, checked inside
`complete()` **before** `litellm.completion` is called. Reaching the ceiling raises
`BudgetExceeded` and nothing is sent to a provider.

**The rule this resolves.** Three requirements are in tension:

1. The global operating rules require a spend cap to be checked before the metered call,
   not logged after it.
2. `CLAUDE.md` requires that "a fault in routing, budgeting or cost logging must never stop
   a consuming application from making its call".
3. A cap that fails open is not a cap.

They hold together because (2) and (3) are about different things. A **fault** in the
machinery — an unreadable log, a corrupt line, an I/O error — allows the call, records the
reason in `BudgetStatus.fault`, and warns once. A **decision** by the machinery — a total
computed successfully that reaches the ceiling, or a configuration that makes the ceiling
unenforceable — refuses it.

**Rules out:** "simplifying" that asymmetry in either direction. Making every path fail open
leaves a cap that never caps. Making every path fail closed means a corrupt log line can
take down web-auditor.

---

**A ceiling with no cost log refuses every call.** The cost log is the only record of what
this library has spent, so it is the only thing a ceiling can be enforced against.
`LLM_GATEWAY_MONTHLY_BUDGET_GBP` set without `LLM_GATEWAY_COST_LOG` therefore raises
`BudgetMisconfigured` rather than allowing calls. Setting a budget is a deliberate act; a
control that is visible in configuration and enforces nothing is worse than no control,
because it reads as handled. The same applies to a value that cannot be parsed, or a
negative one — a typo must not leave spend uncapped. `0` is valid and means spend nothing.

**Unpriced calls are counted, never estimated.** `cost_gbp` is null for streaming, unknown
models and every modifier `pricing.py` declines to guess at. That spend is real and
invisible to the total. `BudgetStatus.unpriced_calls` carries the count, and it appears in
the refusal message. **A workload that is entirely streaming will never trip the ceiling** —
stated in the README rather than left to be discovered. Filling the gap with an estimate
would put a guessed number inside the one control that has to be believed.

**Months are UTC**, because that is what `timestamp` is written in. A UK consumer thinking
in local months will see a boundary up to an hour out during BST. Converting per record
would mean parsing every timestamp on a file that can hold a month of bulk work; the
month prefix is read directly for the UTC timestamps this library writes, and only a
foreign-offset timestamp is parsed properly.

**The ledger is read incrementally.** Re-summing the whole log on every call would be
O(file) per provider call, and the moto SEO pipeline is bulk work. A module-level cache per
log path holds a byte offset and per-month totals; each check is one `stat`, then a read of
only the bytes appended since last time. Consequences deliberately accepted:

- Only bytes up to the last newline are consumed, so a read that catches another process
  mid-append leaves the partial line for next time rather than counting a torn record.
- Totals are bucketed by month rather than reset, so month rollover costs nothing.
- A file shorter than the cached offset triggers a full rescan — rotation is detected, not
  assumed away.
- The first read in a process is still a full scan. Once per process, and correct.

**No file locking, so the cap has a race window.** Two processes can each read a total under
the ceiling and both proceed. Re-reading before every call narrows it to one in-flight call
per process. A library that takes locks inside someone else's process is a new failure mode,
and `cost_log.py` already declines to solve cross-process interleaving for the same reason.

**`LLM_GATEWAY_BYPASS` will not switch off the ceiling.** It is unimplemented — there is no
router to bypass — but `test_bypass_does_not_disable_the_ceiling` fixes the decision now.
A variable that silently disables a spend cap gets set during an incident, which is exactly
when the cap matters most.

**Accepted losses:** the cap is a ceiling on what the log says, not on the provider's
invoice; deleting or rotating the log mid-month resets spend to zero. Enforcement is global
and monthly only — there is no per-workload or per-provider ceiling yet.

**Open:** whether the recording-failure warnings (this module and `completion.py`) should
exist at all, given the global rule against unprompted logging. Left consistent with what
`completion.py` already does, and flagged again for review.

---

## 2026-09-04 — Model escalation: our loop, one cost record per attempt

**Decided:** `complete()` takes `ladder=[...]` (an ordered list of models, cheapest first)
and `escalate_when=fn` (a caller-supplied predicate). Each attempt is recorded separately,
attempts of one call share a `chain_id`, and the spend ceiling is re-checked before every
attempt. `SCHEMA_VERSION` moves to 3 for `chain_id`, `attempt` and `ladder_size`.

**Rules out: `litellm.completion(fallbacks=[...])`.** It works — it routes to
`completion_with_fallbacks` without needing a `Router`, so no proxy is involved. It is still
wrong here. litellm's fallback loop is invisible to this wrapper, so three billable attempts
return one response and produce **one** cost record. Per-attempt records are the entire
value of this library; a chain that understates spend is worse than no chain, because the
wrong figure looks completely plausible. The loop is therefore reimplemented in
`completion.py`, and `fallbacks` is never passed through.

**The predicate is the caller's, and it is guarded.** This library cannot judge whether an
answer is good enough — that is the consuming application's domain knowledge, not ours — so
there is no default predicate and no ladder climbs on quality without one. Because it is
caller code running inside our loop, a predicate that raises causes the response it was
asked about to be **accepted**: escalating on the strength of a bug spends real money,
whereas accepting spends nothing more and returns an answer that genuinely arrived. It is
never called on the top rung, where it could only produce a fault.

**Which errors climb.** Everything except `BadRequestError`, which will fail identically on
every rung, with `ContextWindowExceededError` carved back in — it subclasses `BadRequestError`
(verified against litellm 1.99.0) but is the strongest reason there is to move to a larger
model, so the isinstance order matters and is asserted in the tests. Authentication and
rate-limit errors do climb, because a ladder may cross providers and a 401 from Anthropic on
rung 1 says nothing about an OpenAI model on rung 2.

**A paid-for answer is never destroyed.** Two cases, one rule:

- The ceiling refuses attempt 2 after attempt 1 has answered → attempt 1's response is
  returned, and the refusal is recorded. The ceiling exists to stop *further* spend, not to
  throw away what the money already spent bought. A refusal on the *first* attempt still
  raises, because there is nothing to hand back.
- Attempt 2 fails at the provider after attempt 1 has answered → attempt 1's response is
  returned. A ladder means "give me the best you can get".

With no ladder there is never an earlier success, so provider errors propagate exactly as
they did before this change. That is what keeps it backward compatible.

**Streaming is never escalated.** Usage and content do not exist until the generator is
consumed, so a predicate has nothing to judge and pricing has nothing to price. The first
rung is used and the record carries reason `streaming_not_escalated` — distinct from
`streaming_not_instrumented`, so the log says which of the two happened. `ladder_size` still
records the ladder that was *given*, so the log shows one was supplied and ignored rather
than hiding it.

**No field records why a chain climbed**, because it is inferable: an attempt with
`status: "error"` was followed by an error fallback, one with `status: "ok"` by a predicate
escalation. Adding the field would also force the record write to wait on caller code.

**Evidence:** all seventeen load-bearing behaviours were mutation-tested individually —
behaviour removed, the intended test watched to fail, behaviour restored. Two tests did not
fail on the first pass and were wrong rather than the code: one asserted that `**kwargs` was
not mutated, which is unfalsifiable because Python builds a fresh dict per call; the other
was masked by a second defensive fallback in `complete()`. Both were replaced with
assertions against the units themselves. A test that has never been watched to fail is an
assumption.

---

## 2026-09-04 — The fault logger stays, and routing uses it too

**Decided:** the `logging.warning` calls in `completion.py`, `budget.py` and now `routing.py`
stay. Flagged on both previous pull requests without a ruling; ruled on now.

The global operating rules say not to add logging unless it is in the brief. The same rules
say not to swallow exceptions or return silent failures. Both are satisfied by treating this
as a **fault channel rather than instrumentation**: a standard library logger, no handler
attached, so it is invisible until a consuming application decides to look and costs nothing
when it does not. Faults are warned once per distinct fault, not once per call, so a caller
repeating a mistake in a loop does not flood anything.

**Rules out:** general-purpose logging of calls, latencies or outcomes. That is what the cost
log is for. Nothing beyond a fault ever reaches the logger, and no prompt or completion text
reaches either.

---

## 2026-09-04 — `LLM_GATEWAY_BYPASS` switches off routing, and only routing

**Decided:** `LLM_GATEWAY_BYPASS=1` truncates a ladder to its first rung — one attempt, no
escalation, no error fallback. `routing.bypass_enabled()` reads it; `completion.py` applies
it in the same place streaming is applied. Nothing else changes: the spend ceiling is still
enforced before the call and the cost record is still written. Version 0.4.0.

The variable has been documented in `.env.example` since the first commit and unwired ever
since, because until PR #4 there was no router to bypass. Its purpose is diagnostic: when a
consuming application misbehaves, this is how you find out whether the fault is in our
routing or at the provider, without editing their code.

**The cap and the log are not negotiable.** `test_bypass_does_not_disable_the_ceiling` was
written into `test_budget.py` before any of this existed, precisely so that wiring bypass
into the ceiling would fail the suite. It still passes, and `TestBypass` now asserts the
same thing on the laddered path. An environment variable that silently switches off a spend
cap gets set during an incident, which is exactly when the cap matters most.

**Bypass changes how many attempts are made, not which model is called.** `resolve_ladder`
still runs first, so a ladder still wins over an explicit `model=` and still warns when both
are given; bypass then takes rung 1. The alternative — letting `model=` win under bypass —
matched the older wording in `.env.example` ("call the named provider directly", written
before ladders existed) but would mean a diagnostic run called a *different* model from the
one attempt 1 uses in production. A diagnostic that reproduces a different call is not one.
`.env.example` has been reworded rather than the behaviour bent to fit it.

**A suppressed ladder is recorded**, as `reason: "bypass_no_escalation"`, and only when
`ladder_size > 1`. PR #4 decided not to record *why* a chain climbed, on the grounds that it
is inferable from the previous attempt's `status`. This is not inferable: one `ok` attempt
against a two-rung ladder looks identical whether the router was bypassed or the predicate
was satisfied. On a call with no ladder bypass changed nothing, so recording it there would
put a reason on every line that says nothing about that line. Where both apply, the
streaming reason wins — it also explains `measured: false`, which a reader needs first.

**Both halves of the on/off list are spelled out.** `1`, `true`, `yes`, `on` are on; `0`,
`false`, `no`, `off`, empty and unset are off; anything else is off and warns once. The
usual "any non-empty value is on" shortcut would read the `LLM_GATEWAY_BYPASS=0` that
`.env.example` ships as *on*, and the person who set it would have no way to tell. Off is
the safer landing for an unrecognised value because it is the behaviour every other consumer
gets, but it cannot be silent: someone who set the variable believes the router is off.

**`SCHEMA_VERSION` stays at 3.** No field is added or changes shape; `reason` gains a value,
and a reader of the log does not enumerate `reason` the way it counts `status`. Recorded here
because it is a judgement rather than an obvious call — the bar for a bump is "a reader must
know about this to parse correctly", and a new descriptive string does not meet it.

**Evidence:** nine load-bearing behaviours mutation-tested individually — the truncation,
the reason branch, its `ladder_size > 1` condition, the on-list, the off-default for an
unrecognised value, the strip/lowercase, the fail-open guard, and both the ceiling and the
cost-log write under bypass. Each was removed, the intended test watched to fail, then
restored. All nine were killed on the first pass. Suite: 209 tests, ruff clean.

---

## 2026-09-05 — Per-workload ceilings, layered on the global one

**Decided:** `LLM_GATEWAY_WORKLOAD_BUDGETS_GBP` holds a JSON object mapping a workload label
to its own monthly ceiling in GBP. It is layered on `LLM_GATEWAY_MONTHLY_BUDGET_GBP`, not a
replacement: every ceiling that applies to a call is checked before it, and the first to
refuse stops it.

```
LLM_GATEWAY_WORKLOAD_BUDGETS_GBP={"web-auditor":5,"moto:bulk":20}
```

**This supersedes the accepted loss recorded on 2026-09-02** — "Enforcement is global and
monthly only — there is no per-workload or per-provider ceiling yet". Per-workload now
exists; per-provider still does not, and the reason is below rather than left as a gap.

**The problem.** One global pot means one runaway consumer takes down every other one. The
moto SEO pipeline doing bulk catalogue work and web-auditor doing interactive page summaries
share it: when moto exhausts the month, web-auditor starts raising `BudgetExceeded` for
spend it did not make. Every cost record already carries the `workload` label `complete()`
requires, so the attribution needed to separate them was in the log and nothing read it.

**Workload, not provider.** `workload` is given by the caller and is always present.
A provider is inferred from the model id through litellm's `get_llm_provider` and is `None`
for anything it does not recognise, which leaves calls a provider ceiling could not attribute
— and then the choice is between letting them past the cap or refusing them on what is
really a missing lookup. Both readings corrupt the fault/decision split this module is built
on. A cap cannot rest on an attribution that is sometimes absent. Per-provider is *not*
blocked by this design — the ledger and verdict machinery are axis-agnostic — but it needs
that hole closed first, and it should wait until something actually needs it.

**Rules out:** per-provider ceilings as currently shaped; capping on anything derived rather
than supplied.

---

**One JSON object, not one variable per workload.** Labels contain `:` and `-`, so mangling
them into environment-variable names would need an encoding, and an encoding is a second
thing to get wrong. JSON has a standard parser, gives precise errors, and makes the whole
configuration one greppable line. A config *file* was considered and rejected: it puts an
I/O fault path inside a spend cap and creates a second source of truth alongside `.env`.

**Matching is a `:`-boundary prefix, not an exact key.** Labels are documented as
`app:component:operation`, so a ceiling on `web-auditor` caps everything that application
does without the operator enumerating its operations — and, more importantly, a new label a
consumer adds next week is capped by default rather than silently uncapped. Every level that
matches applies at once. Matching on raw string prefixes would make a ceiling on `web`
quietly cap `web-auditor`, so the boundary is by segment, not by characters.

**Rules out:** exact-match-only keys; substring matching; wildcards. A label typo on the
*calling* side is still uncapped by anything but the global ceiling, which is the accepted
cost of matching by prefix rather than requiring registration.

---

**A refusal reports the global ceiling first; otherwise the tightest is reported.** When the
global ceiling and a workload's are both out of money, the global one is what a reader needs
to be told: raising the workload's would not let the call through. When nothing refuses, the
ceiling with the least headroom is returned, because it is the one that bites next and it is
what a consumer sizing a batch has to see. Both orderings are individually mutation-tested,
because either could be flipped without any obvious symptom.

**`BudgetStatus.spent_gbp` is the scope's spend, not always the month's.** `scope` and
`scope_key` say which ceiling is being reported on. The alternative — always reporting the
month's total — would make `remaining_gbp` meaningless for the ceiling it sits next to. This
is a trap if it is not read, so it is stated in the README, in the dataclass docstring and
here.

**`budget_status()` with no workload answers about the global ceiling.** There is no honest
per-workload answer for a label nobody named.

**Refusals carry the deciding status.** `GatewayError.status` is set by `enforce()`, so a
consumer can ask which ceiling stopped it instead of parsing English out of the message,
and `completion.py` uses it to choose the record's `reason`.

---

**The ledger buckets only configured keys.** A caller is free to invent a label per call, and
totalling every label ever logged would make a long-running process's memory a function of
someone else's naming. Attribution therefore happens as lines are folded, against the
configured key set — which means a change to that set makes the buckets *incomplete* rather
than merely stale, and forces a full rescan. Config changes are rare; a wrong total inside a
spend cap is not survivable.

**A rescan publishes only when it succeeds.** It reads into a detached ledger and swaps it in
afterwards. Clearing the cached totals first would mean an I/O fault during a configuration
change fell back to zero spend — a blip re-opening the tap, which is exactly the failure the
2026-09-02 entry rules out.

**`SCHEMA_VERSION` stays at 3.** No field is added or changes shape; `reason` gains
`budget_exceeded_workload`. Same judgement as the bypass work: the bar for a bump is "a
reader must know about this to parse correctly", and a new descriptive string does not meet
it. Which ceiling refused is recorded rather than left inferable, because it genuinely is not
— the label is on the line either way, and only the configuration says whether that label had
a ceiling of its own.

**Accepted losses:** a ceiling is still enforced against what the log says, not the invoice;
the cross-process race is unchanged and now spans two axes rather than one; a workload no key
names is capped only by the global ceiling; and a label typo at the call site produces spend
attributed to a label nothing governs.

**Evidence:** 24 load-bearing behaviours mutation-tested individually — every branch of the
config parser, both halves of the matching rule, label and key normalisation, spend and
unpriced attribution, the bucket-only-configured-keys bound, both orderings in `_decide`, the
rescan trigger, the detached rescan, the "workload ceilings alone switch the feature on"
condition, the no-log message's variable, the refusal reason, and passing the label to
`enforce` at all. Each was removed, the intended test watched to fail, then restored. All 24
were killed. Suite: 249 tests, ruff clean.

## 2026-09-05 — Consumer integration spike: what actually breaks against web-auditor

Nothing in the library was blocked, but nothing had used it either. This is the read-only
spike that answered "what happens when a real consumer picks it up", run against
`web-auditor` (the only git-managed LLM consumer that exists) without integrating anything.
No provider calls were made; every result below is either a local comparison or a check
against installed source.

**Structured output already works, unchanged.** This was expected to be the blocker and it
is not. `web-auditor`'s `llm.judge()` uses `anthropic.messages.parse(output_format=Schema)`
to get a Pydantic-validated response, and `complete()` wraps `litellm.completion`, which has
no `parse`. But `complete()` is a drop-in, so `response_format=Schema` passes straight
through, and `litellm.utils.get_optional_params` normalises a raw Pydantic class into
Anthropic's native `output_format` with `additionalProperties: false` — the same
structured-outputs feature the SDK's `parse` uses. Verified end-to-end against a stubbed
provider: the schema reaches litellm, `workload`/`ladder`/`escalate_when` do not leak into
the provider call, usage extraction still finds the tokens, and a correct cost record is
written. **No library change is needed to support a schema-driven consumer.**

One trap for whoever tests this next: `AnthropicConfig.map_openai_params` **silently drops a
raw Pydantic class** and returns `{}`, even with `drop_params=False`. It only understands the
already-normalised dict form. Probing that method directly says structured output is
unsupported, which is wrong — the normalisation happens one layer above it. Test through
`get_optional_params`, not the config method.

**The two pricing tables disagree on `claude-sonnet-5` by 50%, and this must be resolved
before anything is integrated.** `web-auditor/app/llm_pricing.py` has `$3.00/$15.00` per
Mtok; this library has `$2.00/$10.00`. On a representative judgement (2,000 in, 500 out) that
is `$0.0135` against `$0.0090`. Every other model in `web-auditor`'s dropdown matches exactly.

**Checked against the published page on 2026-09-05 and settled: this library is right on all
four rates and `web-auditor` is wrong.**
<https://platform.claude.com/docs/en/about-claude/pricing> gives Claude Sonnet 5 as `$2`
input, `$10` output, `$2.50` 5-minute cache write and `$0.20` cache read — matching
`_anthropic(2.0, 10.0, 2.50, 0.20)` exactly. No change is needed here, and `_CHECKED` stays
at 2026-09-02 because only this one model was re-checked; moving it would claim a full audit
that did not happen.

The interesting part is *why* `web-auditor` is wrong, because it was not carelessness. Its
table deliberately records "standard (non-introductory) rates... so the estimate is a stable
ceiling rather than one that jumps when an introductory discount expires", and `$3/$15` was
the genuinely scheduled post-introductory rate for Sonnet 5. Anthropic then cancelled that
increase: the page now notes that the launch pricing "announced at launch as introductory
pricing through August 31, 2026, is now the standard price" and that the rise to `$3/$15` on
2026-09-01 "will not occur". So a defensible conservative choice made on 2026-07-06 silently
became a 50% overcharge on 2026-09-01, with nothing at the call site to signal it.

**That is the argument for the `checked` date and source URL on every rate, and it is worth
keeping in mind whenever the "stable ceiling" instinct resurfaces.** Deliberately pricing
above the published rate does not fail safe; it fails *quietly*, and it stops looking like a
choice the moment the person who made it moves on. A dated, sourced rate that is briefly
stale is recoverable, because the staleness is visible. This is the same reasoning as the
2026-09-02 "null rather than wrong" rule, arriving from the opposite direction.

**`web-auditor` prices an unknown model at the Opus rate; this library returns null.** Its
`_FALLBACK_RATE` is deliberate and documented there as "a stable ceiling rather than one that
jumps". That is defensible for a display estimate, but the same figure feeds
`llm._budget_exceeded()`, so a model id the table does not know silently distorts the spend
gate rather than making it obvious. This is the "null rather than wrong" rule from
2026-09-02, met head-on in a consumer that chose the opposite. **Do not quietly convert one
to the other during an integration** — it changes what that consumer's budget means.

**`web-auditor`'s cost estimate ignores cache tokens entirely.** `_record_usage` takes only
`input_tokens` and `output_tokens`; `cache_read_input_tokens` and
`cache_creation_input_tokens` are never read. It does not use prompt caching today, so this
is latent rather than live — but if caching is ever enabled there, its own figures and this
library's would drift apart, and in opposite directions, because litellm folds cache tokens
into `prompt_tokens` (see the 2026-09-02 entry) while the Anthropic SDK reports them as
separate fields. Whether the SDK's `input_tokens` is exclusive of them is *not* settled by
the type's docstring and needs confirming before anyone relies on the direction of that drift.

**Two ledgers and two budget axes, neither subsuming the other.** `web-auditor` writes
per-account `LlmUsage` rows to Postgres and caps per account from an admin setting; this
library writes process-local JSONL and caps per process, globally and per workload label,
from environment variables. `web-auditor` is multi-worker FastAPI, which is precisely the
case the 2026-09-02 entry's cross-process caveat covers. **An integration must decide which
ledger is authoritative rather than running both and hoping they agree** — two spend figures
that disagree is worse than one, given the only reason this library exists is to be believed.

Their disclosure profiles also differ and should not be merged: `LlmUsage` deliberately
stores `prompt` and `response_text` for the admin diagnostic screen, while a cost record must
never contain either. Confirmed on the spike record — it carries token counts, rates and
identifiers only.

**A kill switch belongs in the consumer, not here.** `LLM_GATEWAY_BYPASS` is not one: by
design it truncates the ladder to a single rung and keeps the ceilings and the log on (see
the 2026-09-04 entry). An operator control meaning "stop routing through the gateway
entirely" is `judge()` choosing the direct provider call it makes today, which is this
library's own fail-open rule promoted to a setting. Building it as `LLM_GATEWAY_BYPASS`
would collapse the distinction that entry exists to protect.

**The library is synchronous, and that rules out the obvious smaller consumer.** There is no
`acompletion` wrapper and no async entry point. `email-warmup` — two `messages.create` calls,
no schemas, otherwise a far gentler first consumer — uses `AsyncAnthropic` and `await`, so
adopting it would mean building async support first. It is also not under git. Of the six
sibling projects, four make no LLM calls at all. **`web-auditor` is the only realistic first
consumer**, and its LLM surface is one function with about ten call sites, which is a smaller
target than the size of the codebase suggests.

**Order of work this implies:** the Sonnet 5 rate is now settled (fix it in `web-auditor`); decide
which ledger is authoritative; add the consumer-side kill switch; then integrate `judge()`.
The structured-output work that looked like a prerequisite is not one.

## 2026-09-05 — The cost log is not a system of record

**Decided:** where a consuming application already keeps its own record of LLM spend, that
record stays authoritative and this library's cost log does not replace it. The log is this
library's own measurement, kept so it can attribute spend and enforce a ceiling. It is not
the books.

**Why this needed deciding at all:** the integration spike found `web-auditor` keeping
per-account `LlmUsage` rows in Postgres, capped per account from its admin UI, while this
library keeps process-local JSON lines capped per process from environment variables. Both
are real, neither subsumes the other, and adopting the library naively would have left two
spend figures for the same calls. **Two figures that disagree are worse than one**, and the
only reason this library exists is to be believed — so one of them had to be named as the
answer, in advance, rather than discovered when they diverged.

**Postgres wins on the merits, not by seniority.** It is transactional, it is shared across
workers by construction, it attributes to an account rather than a process, and an admin
screen and an alerting path are already built on it. Making the JSON-lines log authoritative
would mean rebuilding all of that *and* buying a concurrency problem, to replace something
that works.

**What that makes the ceiling: a local brake, not an accounting control.** Worth stating
precisely, because "spend cap" invites a stronger reading than the mechanism supports:

- **The check is not atomic with the spend.** `enforce()` reads the ledger, the provider
  call is made, and the record is appended afterwards. Nothing holds across those three
  steps. `budget._lock` makes the ledger *read* thread-safe within one process; it does not
  span the read-call-write sequence, in-process or otherwise.
- **So concurrent calls can each pass a check that none of them has yet paid for.** The
  overshoot is bounded by how many calls are in flight when the ceiling is crossed, times
  what each costs. In a single-threaded batch that is one call. In a multi-worker,
  multi-threaded server it is not, and the README previously said "one in-flight call per
  process", which understated it. Corrected in the same commit.
- **It is a ceiling on what the log says, not on the provider's invoice.** Unchanged, and
  now the general case of the same point.

**Rules out:** presenting the ceiling as a guarantee that spend cannot exceed a number, and
presenting the cost log as a billing ledger or an audit trail. It is neither, and both
readings are easy to fall into from the name.

**Rules out, also:** adding file locking to make the log authoritative *by default*. A
library taking locks inside someone else's process is a new failure mode, and it would be
solving a problem this decision removes. If a future consumer genuinely has no ledger of its
own and needs a hard cap, that is a deliberate piece of work with its own entry — not a
quiet hardening of this path.

**Accepted losses:** a consumer with no ledger of its own gets a brake and not a guarantee.
Cross-process append interleaving remains unguaranteed on Windows, so separate consumers
still want separate files. Neither is new; both are now stated where someone relying on the
ceiling will actually read them.

**Evidence:** no behaviour change, so nothing to mutation-test. The sequencing claim above
was read off `completion.py` — `budget.enforce()`, then `litellm.completion()`, then the
record write — and the lock's scope off `budget.evaluate()`, rather than restated from
memory. Suite: 249 tests, ruff clean.

---

## 2026-09-07 — What the first real integration taught

web-auditor integrated `judge()` on 2026-09-06 (its PR #409, merged and deployed). It shipped
dark behind an operator switch that defaults to the direct Anthropic call, so no traffic moves
until someone turns it on. The 2026-09-05 spike predicted the shape of this correctly and
missed four things. They are recorded here because the spike entry reads as settled and is
now incomplete.

**litellm's exceptions are not `anthropic.AnthropicError` subclasses.** They derive from
`openai.OpenAIError` — verified against litellm 1.100.0, not restated from the taxonomy in
the 2026-09-02 entry, which describes litellm's internal hierarchy and never says this. It is
what a consumer trips over first. web-auditor's `judge()` had one `except
anthropic.AnthropicError` covering its provider call; on the gateway path that catches
nothing, so an error which previously degraded one audit check would instead propagate out
and break it. Worse, that consumer's "is this a billing failure worth alerting on" test was
keyed on Anthropic's exception classes, so it would have stopped firing on the day the switch
went on — silently, because the alert not firing looks exactly like nothing being wrong.
**Any consumer moving an existing path onto `complete()` must widen its exception handling
before the switch, not after.**

**A consumer catching `GatewayError` swallows a misconfiguration as if it were a spend
decision.** `BudgetExceeded` and `BudgetMisconfigured` share that base class deliberately —
the distinction it draws, "the gateway declined to spend this" versus "the provider failed",
is the useful one for the common case. But the two are not interchangeable.
`BudgetMisconfigured` means a ceiling cannot be enforced as configured — set but unparseable,
or set without `LLM_GATEWAY_COST_LOG` — and it refuses **every** call until a human fixes an
environment variable. The first integration caught the base class and suppressed its billing
alert for the whole family, on the reasoning that a refusal is a decision rather than an
outage. That reasoning is right for one subclass and wrong for the other, and the failure it
produces is the quiet kind: every request degrading behind a log line, no alert, and the
operator switch still reading as healthy.

**Fixed in documentation, not in the hierarchy.** Splitting `BudgetMisconfigured` out from
`GatewayError` would break the distinction that makes the base class worth having, to protect
against one misreading of it. The README now tells a caller to catch the two separately and
says why. **Rules out:** reparenting `BudgetMisconfigured`, and adding a third exception
family for operator faults.

**`import litellm` costs ~10.5s, and this library deferring it is now load-bearing rather
than tidy.** `import llm_gateway` is ~0.08s because `complete()` does that import itself. A
consumer can therefore import this library at module scope without paying for litellm on a
web boot, a worker boot or a test session — but the first real call in each process pays all
of it. **A caller's `timeout` bounds the provider call and not that import**, so the first
gateway call in a fresh process is ~10.5s slower than the steady state and no timeout will
say so. Worth knowing before wiring `complete()` into a request path.

**litellm 1.100.0 recognises this consumer's model ids.** `claude-haiku-4-5`,
`claude-sonnet-5` and `claude-opus-4-8` all route to anthropic and all have prices. The
null-provider attribution hole — the reason there is no per-provider ceiling, recorded
2026-09-05 — does not bite web-auditor. It is still a real hole for an unrecognised model id;
it is not one this consumer stands in.

**This repository is public as of 2026-09-06, and that was forced rather than chosen.** A
`git+https` dependency on a private repository fails in a consumer's CI (`pip install -e
".[dev]"`) and again in its Docker build on the VPS, because neither holds credentials for a
second private repo. The alternatives — a deploy key in two more places, or vendoring the
source — were both worse. Scanned before flipping: no keys, no customer data, nothing but the
author's own commit address. **Consequence for this file: it is a public document.** Consumer
names, hosting providers and internal reasoning in it are readable by anyone.

**Supersedes the moto SEO pipeline as a consumer, everywhere it appears above.** The
2026-09-02 entry leaves open "the moto SEO pipeline's Python version has not been checked",
and the 2026-09-05 per-workload entry motivates workload ceilings with moto and web-auditor
sharing one pot. Both were written in good faith and both are void: the only moto codebase is
a product scraper with no LLM dependency, no packaging and no git. The Python-version
question is not answered, it is moot. The case for per-workload ceilings does not depend on
it — one consumer with several workloads under one global pot makes the same argument, which
is exactly what web-auditor is, labelling every call `web-auditor:<purpose>`. The README's
worked examples have been changed to match; the dated entries above are left as written.

**`_FALLBACK_RATE` stays unreconciled, deliberately** (Pete's call, 2026-09-06). web-auditor
prices an unknown model at the Opus rate; this library returns null and never estimates.
Those fail in opposite directions — overstating trips that consumer's own gate early, nulling
lets spend run past a ceiling here — so they are not one number measured twice and averaging
them would mean nothing. **Rules out:** converting either convention to the other during a
future integration. It changes what that codebase's budget means, and the argument is written
beside the constant in its source.

**Accepted losses:** a consumer must widen its exception handling and read the
`BudgetExceeded`/`BudgetMisconfigured` distinction before switching a path over, and neither
is discoverable from the type signature alone. The ~10.5s first-call cost is unavoidable
while litellm is the provider layer.

**Evidence:** no library change, so nothing to mutation-test — every finding above was read
off installed source or observed in the consumer's integration rather than recalled. The
exception hierarchy was checked against litellm 1.100.0; the import cost measured in a
subprocess, because by the time a suite runs something else has usually imported litellm
already and an in-process measurement passes for the wrong reason. Suite: 249 tests, ruff
clean, v0.5.0 and schema 3 unchanged.
