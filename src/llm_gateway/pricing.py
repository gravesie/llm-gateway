"""Per-model token prices, and the USD cost of a single call.

Every rate below carries the URL it came from and the date it was read. Provider pricing
changes, and a rate with no provenance eventually produces a confident wrong number, which
is worse than no number at all. When neither this table nor litellm knows a model, the cost
is reported as ``None`` rather than guessed.

Rates are per million tokens (MTok), in USD, matching how the providers publish them.

What this module deliberately does not price:

- Batch API submissions (50% off), which go through a different litellm entry point.
- 1-hour cache writes (2x the 5-minute rate).
- Anthropic fast mode, and ``inference_geo="us"`` (1.1x).
- Non-standard OpenAI service tiers (flex, priority).
- The higher rates that apply above a prompt-length threshold. Where a model has one, it
  is recorded as ``max_priced_prompt_tokens`` and a longer prompt is reported as unpriced.
- Gemini context-cache *storage*, which is billed per hour and cannot be attributed to a
  single call.

The caller detects those modifiers and reports a null cost with a named caveat, rather than
returning a base-rate figure that would silently understate or overstate the bill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ModelPrice",
    "PriceLookup",
    "PRICES",
    "look_up_price",
    "cost_usd",
    "long_context_tier_exceeded",
]

# Anthropic publishes a single table for every Claude model, including the prompt-caching
# multipliers. Read on the date in ``_CHECKED``, which is the one place that date lives —
# a second copy here drifts, and did.
_ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"

# OpenAI publishes input / cached input / output on the pricing page. The cache-write
# charge is documented separately as a multiplier, not a rate: 1.25x the uncached input
# rate on GPT-5.6 and later, and no additional charge on earlier models.
_OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
_OPENAI_CACHING_SOURCE = "https://developers.openai.com/api/docs/guides/prompt-caching"

# Google publishes input / output / cache read, plus a per-hour cache storage charge that
# is not attributable to a single call and is therefore not represented here.
_GEMINI_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"

# litellm ships its own catalogue, used only as a fallback for models absent from the table
# above. Its provenance is the installed litellm version, recorded per lookup.
_LITELLM_SOURCE = (
    "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json"
)

_CHECKED = "2026-09-07"

# Model ids frequently carry a release-date suffix (claude-haiku-4-5-20251001,
# gpt-4o-2024-08-06). Stripping exactly that suffix is safe; anything looser would let a
# future claude-opus-5-1 match claude-opus-5 and bill at the wrong rate.
_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


@dataclass(frozen=True)
class ModelPrice:
    """Published rates for one model, in USD per million tokens.

    A rate of ``None`` means "this provider does not publish a per-token charge for this
    category, or it is not known". It does not mean zero. Charging a non-zero token count
    against a ``None`` rate is refused rather than treated as free.
    """

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float | None
    cache_write_usd_per_mtok: float | None
    source_url: str
    checked: str | None
    note: str | None = None
    # Some models charge a higher rate once the prompt passes a threshold. Where one
    # exists, the rates above are the below-threshold rates and this is the ceiling they
    # hold to; a longer prompt is reported as unpriced rather than billed at the low tier.
    max_priced_prompt_tokens: int | None = None


@dataclass(frozen=True)
class PriceLookup:
    """The outcome of resolving a model id to a price."""

    price: ModelPrice | None
    provider: str | None
    priced_as: str | None
    source: str  # "gateway_table" | "litellm" | "unknown"


def _anthropic(
    input_rate: float,
    output_rate: float,
    cache_write_rate: float,
    cache_read_rate: float,
) -> ModelPrice:
    return ModelPrice(
        input_usd_per_mtok=input_rate,
        output_usd_per_mtok=output_rate,
        cache_read_usd_per_mtok=cache_read_rate,
        cache_write_usd_per_mtok=cache_write_rate,
        source_url=_ANTHROPIC_SOURCE,
        checked=_CHECKED,
        note="5-minute cache writes; 1-hour writes are 2x and are not priced here",
    )


def _openai(
    input_rate: float,
    cached_rate: float | None,
    output_rate: float,
    *,
    cache_write_multiplier: float,
    max_priced_prompt_tokens: int | None = None,
) -> ModelPrice:
    return ModelPrice(
        input_usd_per_mtok=input_rate,
        output_usd_per_mtok=output_rate,
        cache_read_usd_per_mtok=cached_rate,
        cache_write_usd_per_mtok=input_rate * cache_write_multiplier,
        source_url=_OPENAI_SOURCE,
        checked=_CHECKED,
        note=(
            "cache write = "
            + (
                "1.25x uncached input (GPT-5.6 and later)"
                if cache_write_multiplier != 1.0
                else "standard input rate; no additional charge"
            )
            + f", per {_OPENAI_CACHING_SOURCE}"
        ),
        max_priced_prompt_tokens=max_priced_prompt_tokens,
    )


def _gemini(
    input_rate: float,
    output_rate: float,
    cache_read_rate: float | None,
    note: str | None = None,
    max_priced_prompt_tokens: int | None = None,
) -> ModelPrice:
    return ModelPrice(
        input_usd_per_mtok=input_rate,
        output_usd_per_mtok=output_rate,
        cache_read_usd_per_mtok=cache_read_rate,
        # Gemini bills context caching by storage-hour rather than per written token, and
        # an hourly charge cannot be attributed to one call. None rather than 0.0, so an
        # unexpected cache-creation count refuses to price instead of pricing as free.
        cache_write_usd_per_mtok=None,
        source_url=_GEMINI_SOURCE,
        checked=_CHECKED,
        note=note,
        max_priced_prompt_tokens=max_priced_prompt_tokens,
    )


_PROMO = (
    "promotional input, output and cache-read rates, published as running through "
    "2026-12-31; all three double on 2027-01-01"
)

# Prompt-length thresholds above which the published rates change.
_OPENAI_TIER = 272_000
_GEMINI_PRO_TIER = 200_000

# Keys are bare model ids, lowercase, with any provider prefix already stripped.
PRICES: dict[str, ModelPrice] = {
    # --- Anthropic: input, output, 5m cache write, cache read ---
    "claude-fable-5-1": _anthropic(10.0, 50.0, 12.50, 0.25),
    "claude-mythos-5-1": _anthropic(10.0, 50.0, 12.50, 0.25),
    "claude-fable-5": _anthropic(10.0, 50.0, 12.50, 1.0),
    "claude-mythos-5": _anthropic(10.0, 50.0, 12.50, 1.0),
    "claude-opus-5": _anthropic(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-8": _anthropic(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-7": _anthropic(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-6": _anthropic(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-5": _anthropic(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-1": _anthropic(15.0, 75.0, 18.75, 1.50),
    "claude-opus-4": _anthropic(15.0, 75.0, 18.75, 1.50),
    "claude-sonnet-5": _anthropic(2.0, 10.0, 2.50, 0.20),
    "claude-sonnet-4-6": _anthropic(3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-5": _anthropic(3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4": _anthropic(3.0, 15.0, 3.75, 0.30),
    "claude-haiku-4-5": _anthropic(1.0, 5.0, 1.25, 0.10),
    "claude-haiku-3-5": _anthropic(0.80, 4.0, 1.0, 0.08),
    # --- OpenAI: input, cached input, output ---
    # Two independent things, easy to conflate. Cache writes: GPT-5.6 and later charge
    # 1.25x input, earlier models carry no additional cache-write charge. The 272k
    # long-context tier is separate and applies to every model below, including gpt-5.5
    # and gpt-5.4 — they publish it in the row label rather than as extra columns, which
    # is why it was missed here until the 2026-09-07 sweep.
    "gpt-5.6-sol": _openai(
        4.0,
        0.40,
        20.0,
        cache_write_multiplier=1.25,
        max_priced_prompt_tokens=_OPENAI_TIER,
    ),
    "gpt-5.6-terra": _openai(
        2.0,
        0.20,
        12.0,
        cache_write_multiplier=1.25,
        max_priced_prompt_tokens=_OPENAI_TIER,
    ),
    "gpt-5.6-luna": _openai(
        0.20,
        0.02,
        1.20,
        cache_write_multiplier=1.25,
        max_priced_prompt_tokens=_OPENAI_TIER,
    ),
    "gpt-5.5": _openai(
        5.0,
        0.50,
        30.0,
        cache_write_multiplier=1.0,
        max_priced_prompt_tokens=_OPENAI_TIER,
    ),
    "gpt-5.4": _openai(
        2.50,
        0.25,
        15.0,
        cache_write_multiplier=1.0,
        max_priced_prompt_tokens=_OPENAI_TIER,
    ),
    "gpt-4o": _openai(2.50, 1.25, 10.0, cache_write_multiplier=1.0),
    "gpt-4o-mini": _openai(0.15, 0.075, 0.60, cache_write_multiplier=1.0),
    "o1": _openai(15.0, 7.50, 60.0, cache_write_multiplier=1.0),
    "gpt-3.5-turbo": _openai(0.50, None, 1.50, cache_write_multiplier=1.0),
    # --- Google Gemini: input, output, cache read ---
    "gemini-3.8-flash": _gemini(0.75, 3.75, 0.075, _PROMO),
    "gemini-3.7-flash": _gemini(0.75, 3.75, 0.075, _PROMO),
    "gemini-3.6-flash": _gemini(0.75, 3.75, 0.075, _PROMO),
    "gemini-3.5-flash": _gemini(1.50, 9.0, 0.15),
    "gemini-2.5-flash": _gemini(0.30, 2.50, 0.03),
    "gemini-2.5-flash-lite": _gemini(0.10, 0.40, 0.01),
    "gemini-2.5-pro": _gemini(
        1.25,
        10.0,
        0.125,
        "rates for prompts up to 200k tokens; above that input is $2.50, output $15 "
        "and cache read $0.25",
        max_priced_prompt_tokens=_GEMINI_PRO_TIER,
    ),
}


def long_context_tier_exceeded(price: ModelPrice | None, total_prompt_tokens: int) -> bool:
    """Whether this call crossed into a pricing tier the table does not carry rates for."""
    if price is None or price.max_priced_prompt_tokens is None:
        return False
    return total_prompt_tokens > price.max_priced_prompt_tokens


def _split_provider(model: str) -> tuple[str, str | None]:
    """Return ``(bare_model, provider)``, asking litellm and falling back to a prefix split.

    litellm accepts both ``claude-opus-5`` and ``anthropic/claude-opus-5``, and raises for
    ids it does not recognise, so the fallback matters.
    """
    try:
        import litellm

        bare, provider, _key, _base = litellm.get_llm_provider(model=model)
        return str(bare).lower(), str(provider) if provider else None
    except Exception:
        # Not a litellm-known model. Strip a leading "provider/" if there is one.
        if "/" in model:
            provider, _, bare = model.partition("/")
            return bare.lower(), provider.lower() or None
        return model.lower(), None


def _per_mtok(entry: dict, key: str) -> float | None:
    """Convert one of litellm's per-token costs to a per-million-token rate."""
    value = entry.get(key)
    return float(value) * 1_000_000 if value is not None else None


def _litellm_version() -> str:
    try:
        import importlib.metadata as metadata

        return metadata.version("litellm")
    except Exception:
        return "unknown"


def _from_litellm(candidates: list[str]) -> tuple[ModelPrice, str] | None:
    """Build a ModelPrice from litellm's own catalogue, or ``None`` if it has no entry."""
    try:
        import litellm

        for candidate in candidates:
            entry = litellm.model_cost.get(candidate)
            if not entry or entry.get("input_cost_per_token") is None:
                continue
            price = ModelPrice(
                input_usd_per_mtok=float(entry["input_cost_per_token"]) * 1_000_000,
                output_usd_per_mtok=float(entry.get("output_cost_per_token") or 0.0) * 1_000_000,
                cache_read_usd_per_mtok=_per_mtok(entry, "cache_read_input_token_cost"),
                cache_write_usd_per_mtok=_per_mtok(entry, "cache_creation_input_token_cost"),
                source_url=_LITELLM_SOURCE,
                checked=None,
                note=f"litellm {_litellm_version()} catalogue, not independently verified",
            )
            return price, candidate
    except Exception:
        return None
    return None


def look_up_price(model: str | None) -> PriceLookup:
    """Resolve a model id to published rates.

    Tries, in order: this module's table on the bare id, the same with a release-date
    suffix stripped, then litellm's catalogue. Anything else resolves to no price, which
    the caller records as a null cost.
    """
    if not model or not isinstance(model, str):
        return PriceLookup(price=None, provider=None, priced_as=None, source="unknown")

    bare, provider = _split_provider(model.strip())
    undated = _DATE_SUFFIX.sub("", bare)

    for candidate in (bare, undated):
        price = PRICES.get(candidate)
        if price is not None:
            return PriceLookup(
                price=price,
                provider=provider,
                priced_as=candidate,
                source="gateway_table",
            )

    fallback = _from_litellm([model.strip().lower(), bare, undated])
    if fallback is not None:
        price, matched = fallback
        return PriceLookup(price=price, provider=provider, priced_as=matched, source="litellm")

    return PriceLookup(price=None, provider=provider, priced_as=None, source="unknown")


def cost_usd(
    price: ModelPrice | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float | None:
    """Total USD for one call, or ``None`` when it cannot be computed honestly.

    Returns ``None`` if there is no price at all, or if a token category has a non-zero
    count but no published rate. Treating an unknown rate as zero would understate the
    bill, and understating it silently is the one failure this library cannot afford.
    """
    if price is None:
        return None

    charges = (
        (input_tokens, price.input_usd_per_mtok),
        (output_tokens, price.output_usd_per_mtok),
        (cache_read_tokens, price.cache_read_usd_per_mtok),
        (cache_write_tokens, price.cache_write_usd_per_mtok),
    )

    total = 0.0
    for tokens, rate in charges:
        if not tokens:
            continue
        if rate is None:
            return None
        total += tokens * rate

    return round(total / 1_000_000, 10)
