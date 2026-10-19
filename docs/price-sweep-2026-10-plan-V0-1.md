# Price sweep, 2026-10-19 — plan (V0-1)

Overdue monthly sweep (last: 2026-09-07 USD table, 2026-09-29 relax.ai table).
One work unit, one PR. Scope: `src/llm_gateway/pricing.py`, its tests, a findings note in
`docs/decisions.md`, and the FX default's "read on" date in `fx.py` if it moves.

## Method
1. Read each vendor's live pricing page (Anthropic, OpenAI, Gemini, relax.ai).
2. Cross-check every USD rate against `litellm.model_cost` (offline, independent).
   relax.ai has no litellm entries: re-read the raw page; VAT treatment noted.
3. Check every model in a block, not the first (OpenAI publishes the 272k tier two ways).
4. Re-verify FX default 1.3555 USD/GBP against Fed H.10.
5. Add models that launched since 2026-09-07 and are pricing through litellm's fallback.
6. Gemini 3.8/3.6 Flash promo doubles 2027-01-01: confirm and keep the note.

## Rules
- No rate changes without page evidence; unreadable page or removed model => leave rate
  and its date alone and say so.
- Only move a date for what was actually re-audited.
- Do not merge; no release tag. Flag any version implication.

## Done
pytest green, ruff clean, PR opened, decisions.md note if anything non-obvious.
