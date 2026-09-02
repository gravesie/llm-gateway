"""Model resolution and the USD arithmetic."""

from __future__ import annotations

import pytest

from llm_gateway import pricing


class TestModelResolution:
    def test_exact_match_uses_the_gateway_table(self):
        lookup = pricing.look_up_price("claude-haiku-4-5")
        assert lookup.source == "gateway_table"
        assert lookup.priced_as == "claude-haiku-4-5"
        assert lookup.price.input_usd_per_mtok == 1.0

    def test_release_date_suffix_is_stripped(self):
        lookup = pricing.look_up_price("claude-haiku-4-5-20251001")
        assert lookup.source == "gateway_table"
        assert lookup.priced_as == "claude-haiku-4-5"

    def test_iso_date_suffix_is_stripped(self):
        lookup = pricing.look_up_price("gpt-4o-2024-08-06")
        assert lookup.priced_as == "gpt-4o"
        assert lookup.price.input_usd_per_mtok == 2.50

    def test_provider_prefix_is_stripped_and_reported(self):
        lookup = pricing.look_up_price("anthropic/claude-opus-5")
        assert lookup.source == "gateway_table"
        assert lookup.priced_as == "claude-opus-5"
        assert lookup.provider == "anthropic"

    def test_unknown_model_resolves_to_no_price(self):
        lookup = pricing.look_up_price("not-a-real-model-9000")
        assert lookup.source == "unknown"
        assert lookup.price is None
        assert lookup.priced_as is None

    def test_no_price_for_a_missing_model_name(self):
        assert pricing.look_up_price(None).source == "unknown"
        assert pricing.look_up_price("").source == "unknown"

    def test_an_unreleased_version_does_not_inherit_its_predecessors_rates(self):
        """The regression that matters: no fuzzy prefix matching.

        A model we have never priced must not quietly bill at the rates of the nearest
        name, which would produce a wrong figure that looks entirely credible.
        """
        lookup = pricing.look_up_price("claude-opus-5-1")
        assert lookup.priced_as != "claude-opus-5"

    def test_litellm_supplies_a_fallback_for_models_absent_from_the_table(self):
        litellm = pytest.importorskip("litellm")
        candidate = next(
            (
                name
                for name, entry in litellm.model_cost.items()
                if name not in pricing.PRICES
                and "/" not in name
                and entry.get("input_cost_per_token")
            ),
            None,
        )
        assert candidate is not None, "expected litellm to know a model we do not"

        lookup = pricing.look_up_price(candidate)
        assert lookup.source == "litellm"
        assert lookup.price is not None
        # A fallback price has no date we checked it, and says where it came from.
        assert lookup.price.checked is None
        assert "litellm" in lookup.price.note


class TestCostArithmetic:
    def test_worked_example_with_caching(self):
        """Haiku 4.5 at $1 in / $5 out / $1.25 5m-write / $0.10 read per MTok."""
        price = pricing.PRICES["claude-haiku-4-5"]
        usd = pricing.cost_usd(
            price,
            input_tokens=1200,
            output_tokens=340,
            cache_read_tokens=8000,
            cache_write_tokens=2000,
        )
        # 1200(1.00) + 340(5.00) + 8000(0.10) + 2000(1.25) = 6200 per million
        assert usd == pytest.approx(0.0062)

    def test_no_price_means_no_cost(self):
        assert (
            pricing.cost_usd(
                None,
                input_tokens=10,
                output_tokens=10,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
            is None
        )

    def test_a_missing_rate_with_tokens_charged_refuses_to_price(self):
        """An unpublished rate must not be treated as free."""
        price = pricing.PRICES["gemini-2.5-flash"]
        assert price.cache_write_usd_per_mtok is None
        assert (
            pricing.cost_usd(
                price,
                input_tokens=100,
                output_tokens=100,
                cache_read_tokens=0,
                cache_write_tokens=50,
            )
            is None
        )

    def test_a_missing_rate_with_no_tokens_is_harmless(self):
        price = pricing.PRICES["gemini-2.5-flash"]
        usd = pricing.cost_usd(
            price,
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        assert usd == pytest.approx(0.30)

    def test_zero_tokens_cost_nothing(self):
        usd = pricing.cost_usd(
            pricing.PRICES["claude-opus-5"],
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        assert usd == 0.0


class TestLongContextTiers:
    def test_a_prompt_within_the_tier_is_priceable(self):
        assert not pricing.long_context_tier_exceeded(pricing.PRICES["gemini-2.5-pro"], 199_999)

    def test_a_prompt_past_the_tier_is_flagged(self):
        assert pricing.long_context_tier_exceeded(pricing.PRICES["gemini-2.5-pro"], 200_001)

    def test_models_without_a_tier_are_never_flagged(self):
        price = pricing.PRICES["claude-opus-5"]
        assert price.max_priced_prompt_tokens is None
        assert not pricing.long_context_tier_exceeded(price, 900_000)


class TestProvenance:
    def test_every_rate_carries_a_source_and_a_date(self):
        for name, price in pricing.PRICES.items():
            assert price.source_url.startswith("https://"), name
            assert price.checked == "2026-09-02", name

    def test_the_table_agrees_with_litellms_independent_catalogue(self):
        """Cross-check against a second source to catch transcription errors.

        Only rates that both sources publish are compared. Where litellm has no entry for
        a model, or either side leaves a category unpriced, there is nothing to compare
        and the disagreement is recorded in the table's own notes instead.
        """
        litellm = pytest.importorskip("litellm")
        fields = (
            ("input_usd_per_mtok", "input_cost_per_token"),
            ("output_usd_per_mtok", "output_cost_per_token"),
            ("cache_read_usd_per_mtok", "cache_read_input_token_cost"),
            ("cache_write_usd_per_mtok", "cache_creation_input_token_cost"),
        )

        compared = 0
        for name, price in pricing.PRICES.items():
            entry = litellm.model_cost.get(name)
            if not entry:
                continue
            for ours_field, theirs_key in fields:
                ours = getattr(price, ours_field)
                theirs = entry.get(theirs_key)
                if ours is None or theirs is None:
                    continue
                assert ours == pytest.approx(theirs * 1_000_000), f"{name}.{ours_field}"
                compared += 1

        assert compared > 40, "expected a meaningful number of rates to cross-check"
