# llm-gateway

In-process LLM routing, fallback and cost instrumentation for PGGI projects.

A Python library, imported by the applications that spend the money. No server, no
database, nothing deployed. See `DEPLOY.md` for why, and `docs/decisions.md` for the
research behind it.

## What it is for

To answer, with evidence, where a monthly LLM bill actually goes, and then to cut it:
cheap models first with escalation, prompt caching, batch submission where latency
allows, and a hard budget ceiling.

## Using it

The library does three things: it measures what calls cost, refuses them once a monthly
ceiling is reached, and climbs a ladder of models so the expensive one is only used when
the cheap one is not good enough. `complete()` is a drop-in replacement for
`litellm.completion`, with one required extra argument.

```python
from llm_gateway import complete

response = complete(
    model="claude-haiku-4-5",
    messages=[{"role": "user", "content": "..."}],
    workload="web-auditor:page-summary",
)
```

`workload` is required and has no default. Unattributed spend is the problem this library
exists to solve, so a call cannot opt out of being labelled.

Set `LLM_GATEWAY_COST_LOG` and every call appends a line like this:

```json
{"schema":3,"timestamp":"2026-09-02T12:34:56.789012+00:00","workload":"web-auditor:page-summary",
 "status":"ok","measured":true,"model":"claude-haiku-4-5","provider":"anthropic",
 "input_tokens":1200,"output_tokens":340,"cache_read_tokens":8000,"cache_write_tokens":2000,
 "latency_ms":1843.2,"cost_usd":0.0062,"cost_gbp":0.00457,"pricing_source":"gateway_table",
 "pricing_checked":"2026-09-02","fx_rate_usd_per_gbp":1.3555,"fx_source":"default"}
```

Leave `LLM_GATEWAY_COST_LOG` unset and nothing is written. The call still works.

### What it promises

- **It fails open.** Any fault in pricing, conversion or writing is caught, and the
  provider's response is still returned. Errors raised by the provider propagate unchanged.
- **It records no prompt or completion text.** A record is assembled field by field from
  token counts and identifiers; nothing reads `messages` or `choices`.
- **It never guesses a price.** Every rate carries the URL it came from and the date it was
  read. Where a rate is unknown, or a modifier applies that this version does not price,
  `cost_usd` is `null` and `pricing_caveat` names the reason. A missing number is recoverable;
  a wrong one that looks plausible is not.

### What it does not measure yet

`cost_usd` is deliberately `null` for calls using the Batch API, 1-hour cache writes,
Anthropic fast mode, `inference_geo="us"`, non-standard OpenAI service tiers, or a prompt
long enough to cross into a higher pricing tier. Streaming calls pass through untouched and
are recorded as `measured: false` — usage is not available until the stream is consumed.
Each of those is countable in the log rather than silently missing.

## The monthly ceiling

Set `LLM_GATEWAY_MONTHLY_BUDGET_GBP` to a number of pounds and a call is refused once
measured spend for the current calendar month reaches it:

```python
from llm_gateway import BudgetExceeded, budget_status, complete

status = budget_status()          # no call is made
print(status.spent_gbp, status.remaining_gbp, status.unpriced_calls)

try:
    complete(model="claude-haiku-4-5", messages=[...], workload="moto:bulk")
except BudgetExceeded:
    ...                           # nothing was sent to the provider
```

The check happens **before** the provider call, never after. A ceiling applied to money
already spent is a log entry, not a ceiling.

`BudgetExceeded` and `BudgetMisconfigured` both derive from `GatewayError`, so a caller can
tell "the gateway declined to spend this" from "the provider failed" without reading
messages. Set the ceiling to `0` to stop spending entirely; leave it empty for no ceiling.

### Per-workload ceilings

The ceiling above is global: one consumer exhausting it stops every other one. Set
`LLM_GATEWAY_WORKLOAD_BUDGETS_GBP` to give a workload its own ceiling as well.

```
LLM_GATEWAY_WORKLOAD_BUDGETS_GBP={"web-auditor":5,"moto:bulk":20}
```

A key matches the `workload` label exactly, or as a prefix at a `:` boundary. `web-auditor`
caps everything that application does; `web-auditor:crawl` caps only that operation; set
both and both apply. Matching is by segment, so a ceiling on `web` does **not** cap
`web-auditor`.

Every ceiling that applies is checked, and the first to refuse stops the call — a workload
under its own ceiling is still refused once the global one is reached. `0` is valid and
stops that workload alone. A workload no key names is capped only by the global ceiling.

```python
status = budget_status(workload="moto:bulk")
print(status.scope, status.scope_key, status.remaining_gbp)   # workload moto 18.4
```

**`spent_gbp` belongs to the ceiling being reported on.** When `scope` is `"workload"` it is
that workload's spend for the month, not the month's total. With nothing refusing, the
ceiling with the least headroom is reported, because that is the one that bites next.
`budget_status()` with no `workload` answers about the global ceiling: there is no honest
per-workload answer for a label that was not named.

`BudgetExceeded` and `BudgetMisconfigured` carry the deciding status as `.status`, so a
caller can tell which ceiling stopped it without reading the message. A refused call is
recorded with `reason: "budget_exceeded_workload"` rather than `"budget_exceeded"`.

There is no per-provider ceiling, deliberately. `workload` is given by the caller and is
always present; a provider is inferred from the model id and is unknown for models litellm
does not recognise, and a spend cap cannot rest on an attribution that is sometimes missing.

### It needs the cost log

The cost log is the only record of what this library has spent, so it is the only thing a
ceiling can be enforced against. Setting `LLM_GATEWAY_MONTHLY_BUDGET_GBP` or
`LLM_GATEWAY_WORKLOAD_BUDGETS_GBP` without `LLM_GATEWAY_COST_LOG` refuses every call with
`BudgetMisconfigured`, rather than leaving a ceiling that appears configured and enforces
nothing. So does a value that cannot be parsed — a typo must not leave spend uncapped.

### What the ceiling cannot see

**It only counts spend it could price.** Every call listed under *What it does not measure
yet* above is billed by the provider and invisible to the total. Those calls are counted, in
`budget_status().unpriced_calls` and in the refusal message, but never estimated — a guessed
figure inside a spend cap is the one thing this library cannot afford.

**A workload that is entirely streaming will therefore never trip the ceiling.** If you
stream, watch `unpriced_calls` — which is that workload's own count when the status is
reporting on a per-workload ceiling.

**It is a ceiling on what the log says, not on the provider's invoice.** Delete or rotate
the log mid-month and spend resets to zero.

**The check is not atomic with the spend, so concurrent calls can overshoot it.** The
ceiling is read before the call and the cost is recorded after it, and nothing holds across
those steps — so calls already in flight when the ceiling is crossed have each passed a
check that none of them has paid for yet. The overshoot is roughly the number of concurrent
calls times what each costs: one call in a single-threaded batch, more in a multi-worker or
multi-threaded server. There is no file locking, and the ledger read being thread-safe does
not close this window. A library taking locks inside someone else's process is a new failure
mode, and `docs/decisions.md` explains why that trade is deliberate.

**Treat the ceiling as a brake, not an accounting control**, and the log as this library's
own measurement rather than your books. If your application already records LLM spend — a
usage table, a billing ledger — that record stays authoritative and this one does not
replace it. See *The cost log is not a system of record* in `docs/decisions.md`.

### Faults do not stop your calls

A fault in the ceiling itself — an unreadable log, a corrupt line, an I/O error — allows the
call and is reported in `budget_status().fault`. A ceiling that was successfully computed
and reached refuses it. Faults fail open; decisions do not. `docs/decisions.md` records why.

## Routing and model escalation

Give `ladder` an ordered list of models, cheapest first, and `escalate_when` a predicate
that returns true when a response is not good enough:

```python
response = complete(
    messages=[{"role": "user", "content": "..."}],
    workload="web-auditor:page-summary",
    ladder=["claude-haiku-4-5", "claude-sonnet-5"],
    escalate_when=lambda r: len(r.choices[0].message.content) < 200,
)
```

Only you can judge whether an answer is good enough — that is your domain knowledge, not
this library's — so without a predicate a ladder never climbs on quality. It still falls
back on provider errors.

### Every attempt is recorded

A ladder makes several billable calls, and one record for the chain would understate what
was spent. Each attempt gets its own record, and the records of one call share a
`chain_id`:

```
{"status":"ok","model":"claude-haiku-4-5","cost_gbp":<cheap>, "chain_id":"9f2c…","attempt":1,"ladder_size":2}
{"status":"ok","model":"claude-sonnet-5", "cost_gbp":<dearer>,"chain_id":"9f2c…","attempt":2,"ladder_size":2}
```

(Shape only — the real figures come from `pricing.py`, where every rate carries its source
URL and the date it was checked.)

Sum `cost_gbp` across a `chain_id` to get what one logical request cost. You can also read
*why* it climbed without another field: if an attempt's `status` is `error`, the next one
was an error fallback; if it is `ok`, your predicate asked for the climb.

This is why the loop is ours rather than `litellm.completion(fallbacks=[...])`. That works,
but litellm's fallback loop is invisible to this wrapper, so several paid attempts come
back as one response and produce one record. `docs/decisions.md` has the detail.

### Which errors climb

Everything except a `BadRequestError`, which will fail the same way on every rung. The
exception is `ContextWindowExceededError` — it subclasses `BadRequestError` but is the best
reason there is to move to a larger model, so it does climb.

Authentication and rate-limit errors climb too. That looks wrong until you remember a
ladder may cross providers: a 401 from Anthropic on rung 1 says nothing about an OpenAI
model on rung 2.

### The ceiling is re-checked before every attempt

Not just the first. If it refuses an attempt after an earlier one has already answered,
you get that answer rather than an exception — the ceiling exists to stop further spend,
not to destroy something already paid for. The refused attempt is still recorded. A
refusal on the *first* attempt raises, because there is nothing to hand back.

The same rule covers failure: a later attempt erroring never discards an earlier success.
A ladder means "give me the best you can get". With no ladder there is never an earlier
success, so provider errors propagate exactly as they always have.

### What a ladder will not do

Streaming calls are never escalated. Usage and content do not exist until the generator is
consumed, so there is nothing for a predicate to judge. The first rung is used, passed
through untouched, and recorded with reason `streaming_not_escalated`.

A malformed `ladder` does not raise. The router must fail open, so it degrades to a single
call and warns. Our validation must never be the thing that stops your call.

### Turning the routing off

Set `LLM_GATEWAY_BYPASS=1` in the environment and a ladder is truncated to its first rung:
one attempt, no escalation, no fallback. That is the call you would have made had you never
passed a ladder, which is what makes it useful when something is misbehaving and you need
to know whether it is the routing or the provider.

It is not a way out of the rest of the library. The spend ceiling is still enforced and
every call is still recorded — a variable that quietly switched off a spend cap would get
set during an incident, which is exactly when the cap matters. A record whose ladder was
suppressed carries reason `bypass_no_escalation`, so the log shows what happened rather
than looking like a first answer your predicate was happy with. `ladder_size` still reports
the ladder you gave.

`1`, `true`, `yes` and `on` switch it on; `0`, `false`, `no`, `off`, empty and unset leave
it off. Anything else is treated as off and warned about — `LLM_GATEWAY_BYPASS=0` must
never mean "on".

## Consumers

**Nothing uses this library yet.** It is built and released, and integration has not
happened.

- **web-auditor** (Hetzner) — the intended first consumer: page content sent to Claude
  during an audit, through a single `llm.judge()` choke point. Its own Postgres usage table
  stays authoritative for spend; see *The cost log is not a system of record* in
  `docs/decisions.md`.
- Local scripts on pete24.

## Install

```
pip install "llm-gateway @ git+https://github.com/gravesie/llm-gateway.git@v0.5.0"
```

Always a tag, never `main`. `DEPLOY.md` explains why.

## Development

```
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -e ".[dev]"
./scripts/install-hooks.sh      # once per clone
python -m pytest
```

`CLAUDE.md` holds the rules a session must follow. Read it before changing anything.
