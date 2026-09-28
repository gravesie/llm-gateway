# Plan: relax.ai as a second LLM provider (V0-1)

## Context
Pete wants the admin to switch web-auditor's LLM calls between Anthropic and relax.ai
(`https://api.relax.ai/v1`), with relax.ai calls tracked in the admin usage lists and their
cost compared with Anthropic's. Almost all of this lives in **web-auditor**. This session is
rooted in **llm-gateway**, so the work is two units, two repos, two PRs.

Decisions taken (Pete, 2026-09-29):
- **Per-purpose choice.** Each of the 15 `judge()` purposes gets its own provider setting on `/admin/llm`.
- **Fall back to Anthropic** when a relax.ai call fails. Both attempts are recorded.
- **llm-gateway change first**, so the gateway's cost log attributes relax.ai correctly.

Facts established (relax.ai docs, read 2026-09-29):
- OpenAI-compatible. Auth is **a Bearer API key only**. No username appears anywhere in the auth docs.
- Priced in **GBP**. There is no litellm provider for it. Route it as `openai/<model>` with `api_base`.
- Current chat models: `DeepSeek-V4-Pro`, `DeepSeek-V41-Flash`, `Nemotron-3-Super`, `Muse-Glimmer-30B`.
- **Unverified:** whether `response_format` accepts a JSON schema. Every `judge()` call depends on structured output.

## Step 0: live smoke test (Pete's key, costs pennies, needs Pete's go)
Pete puts the key in the llm-gateway checkout's `.env`. That file is gitignored and never read by a session. Then a scratchpad script runs:
1. `GET /v1/models`, to confirm the real model IDs. The IDs above came from a summarised page read.
2. For each chat model, one small call with a Pydantic `response_format` sent through `llm_gateway.complete()`. Record three things: does the schema pass, is the `usage` block populated, and what `model` string comes back.

The result decides how Unit 2 gets structured output:
- **Native `json_schema`** if relax accepts it.
- **Otherwise `json_object`**, with the schema put in the system prompt and the reply validated by Pydantic. A validation failure counts as a relax.ai failure and falls back to Anthropic.

## Unit 1: llm-gateway, this session. PR, then tag v0.6.0 (Sonnet can build it)
**Problem.** `complete()` passes `api_base` through untouched, but pricing never sees it. As a result:
- A relax.ai call is logged as `provider: "openai"`.
- It can be priced from litellm's catalogue if a bare ID like `deepseek-v4-pro` matches some other vendor's entry.
That is misattributed spend, and in this library that is a correctness bug.

**Change:**
- `pricing.py`:
  - Add an endpoint registry that maps a host to a provider label: `api.relax.ai` → `relax`.
  - Add a `_relax(...)` helper with the relax rates. Each rate carries its source URL and the date checked. Rates are re-read from the pricing page on the day, never taken from this plan.
  - Add a `currency` field on `ModelPrice`. For a GBP row, `cost_gbp` is exact and `cost_usd = cost_gbp × fx`. There is no GBP→USD→GBP round trip.
- `completion.py`: when `api_base` is present, work out the provider from its host.
  - **Known host:** price only from that provider's rows, and never from `PRICES` or litellm's catalogue.
  - **Unknown host:** `cost_usd` is null and `pricing_caveat="custom_endpoint_unpriced"`.
  - No `api_base`: behaviour is unchanged.
- The cost record schema stays at 3. The record gains no new field, only a new `provider` value and a new caveat value, which is the same bar as earlier releases. Record the decision in `docs/decisions.md`.
- README: document the custom-endpoint behaviour.
- Bump to 0.6.0, since observable pricing behaviour changes. Follow the release procedure in the resume file: tag, then verify the published artefact in a fresh venv.

**Tests (mutation-tested one behaviour at a time, per the repo convention):**
- A relax.ai call gets `provider == "relax"` and the GBP-exact cost.
- A relax model ID that collides with a litellm catalogue entry is not priced from that entry.
- An unknown host gives a null cost plus the caveat.
- No `api_base` behaves exactly as before.
- Ceilings count relax.ai spend.
- A ladder with `api_base` applies it to every rung. This is a limitation, not a new bug: `api_base` is not per-rung. Document it and test it.

**Flagged, not absorbed:** README:55 says an unknown model's `pricing_caveat` names the reason, but today it is null. That is a separate pre-existing bug.

## Unit 2: web-auditor, new session rooted there. One PR (Sonnet)
**Config and secrets**
- `app/config.py`: add `relax_api_key: str | None` and `relax_api_base` (default `https://api.relax.ai/v1`).
- Add placeholders to `.env.example` (`RELAX_API_KEY=`, `RELAX_API_BASE=`) and to `.env.production.example` (lowercase).
- **No username placeholder**, because relax.ai has none.
- Pete adds the real key to the local and production `.env` himself. The session never reads either.

**Pricing:** `app/llm_pricing.py`
- Make the rate table provider-aware and add `known_models(provider)`.
- Hold relax rates in GBP with source and date. Convert them to USD with a dated FX constant for the USD budget gate. Reuse `llm_gateway.fx` if it imports without litellm; otherwise use a local dated constant.
- Relax models never hit `_FALLBACK_RATE`, the Opus rate used for unknown models.

**Settings:** `app/appsettings.py`. No migration needed here.
- `llm_provider_by_purpose` holds JSON `{purpose: "anthropic"|"relax"}`. A missing purpose means Anthropic, and unknown purposes are rejected.
- `llm_relax_model` is validated against `known_models("relax")`.

**Routing:** `app/llm.py`
- `judge()` resolves the provider from `purpose`.
- The relax route calls `llm_gateway.complete(model="openai/<id>", api_base, api_key, …)`. The import is lazy, per doctrine.
- It is always through the gateway, whatever the gateway switch says. The switch keeps governing the Anthropic route only.
- If relax is chosen but no relax key is set, the call goes to Anthropic.
- `available()` is true if either key is set.
- **Fallback:** a relax exception, parse failure, or empty result triggers one Anthropic call on the normal route.
  - Each attempt writes its own `LlmUsage` row.
  - The budget gate is re-checked before the fallback call.
- Billing-alert text becomes provider-aware. No relax.ai billing phrases are guessed; only exception-class detection applies.
- The three keyword-chain purposes pin an Anthropic model. When set to relax.ai they use the relax model, and their rows show a warning: "re-grade against `docs/answer-keys/`".

**Migration (new migration: this plan asks for your go):** on `llm_usage`, add:
- `provider`: String(32), not null, server default `'anthropic'`. This backfills existing rows correctly.
- `outcome`: String(16), nullable, values `ok`/`failed`/`fallback`.

**Admin**
- `/admin/llm`:
  - A provider select per purpose, saved by one POST form.
  - A relax model dropdown.
  - A "relax.ai key configured: yes/no" badge. The key itself is never shown.
  - A Provider column on the per-purpose call tables.
- New **cost comparison** section, this month, provider × purpose:
  - Calls, fallbacks, average input and output tokens, average cost per call (£ and $), and total.
  - Note on the page: tokenisers differ, so compare cost per call, not per token.
- `/admin/summary`: a relax.ai row on the API-dependencies card, showing calls and cost this month.
- "Anthropic credits" labels become "LLM spend" where the figure now sums both providers.
- The budget stays as one cap, `budget_anthropic_usd`, now summing both providers. The label changes and the key does not.

**Other**
- Bump the `llm-gateway` pin to `v0.6.0`, the tag from Unit 1.
- Update the doctrine in `docs/doctrine/judge-through-llm-gateway.md`. No new CLAUDE.md index row, because CLAUDE.md has only 15 bytes of headroom.
- `i18n` `ui.admin.llm.*` keys.

**Tests** (mocked; the existing conftest guard on `llm_gateway.complete` already blocks the relax route, and a test proves it):
- Routing per purpose.
- Fallback writes two rows and re-checks the budget.
- Missing relax key goes to Anthropic.
- GBP→USD costing.
- Settings validation.
- Admin POST: admin only, invalid provider or model rejected.
- Comparison numbers against a seeded fixture.
- Migration upgrade and downgrade.
- `test_importing_app_llm_does_not_import_litellm` still passes.

## Verification
- **Unit 1:**
  - `.venv/Scripts/python.exe -m pytest` and `ruff check src tests`.
  - Mutation pass.
  - Post-tag smoke test against the published v0.6.0 wheel, with litellm stubbed.
- **Unit 2:**
  - Full suite, lint, and `alembic upgrade head` locally.
  - Restart the dev server on 8001 (port 8000 is shared).
  - Set one low-stakes purpose (for example `FEEDBACK_CLASSIFY`) to relax.ai and trigger it.
  - Confirm the row shows provider `relax` and a GBP cost, and that the comparison table populates.
  - Break the relax key and confirm the fallback writes two rows.
- **Before any production purpose is switched:** Pete trials relax.ai per purpose and compares output quality, not just cost. Cheaper and wrong is not a saving.

## Order
Step 0 → Unit 1 (plan commit, build, PR, merge on Pete's go, tag) → new web-auditor session for Unit 2.
Each plan is committed as its branch's first commit (`docs/plan-relax-ai-provider-V0-1.md`).
