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

At present the library does one thing: it measures. `complete()` is a drop-in replacement
for `litellm.completion`, with one extra argument.

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
{"schema":1,"timestamp":"2026-09-02T12:34:56.789012+00:00","workload":"web-auditor:page-summary",
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

Routing, model escalation and budget enforcement are not implemented.

## Consumers

- **web-auditor** (Hetzner) — page content sent to Claude during an audit.
- **moto SEO pipeline** — bulk catalogue work.
- Local scripts on pete24.

## Install

```
pip install "llm-gateway @ git+https://github.com/gravesie/llm-gateway.git@v0.1.0"
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
