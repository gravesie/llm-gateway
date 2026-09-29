"""Calls sent to a custom ``api_base``: who billed them, and at what rate.

relax.ai is the case that forced this. It serves open-weight models through an
OpenAI-compatible API, so litellm reaches it as ``openai/<model>`` with an ``api_base``.
Priced by name alone, such a call was labelled ``openai`` and, for ``DeepSeek-V4-Pro``,
billed at DeepSeek's own dollar rate from litellm's catalogue. Confirmed live on
2026-09-29 before this was fixed; see ``docs/decisions.md``.
"""

from __future__ import annotations

import json

import pytest

from llm_gateway import BudgetExceeded, complete, fx, pricing
from llm_gateway.completion import call_endpoint

RELAX = "https://api.relax.ai/v1"
BUDGET_VAR = "LLM_GATEWAY_MONTHLY_BUDGET_GBP"


def read_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def relax_response(response_factory, *, model="DeepSeek-V4-Pro", prompt=145, completion=21):
    """A relax.ai-shaped response: no cache tokens, as the live smoke test returned."""
    return response_factory(
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=0,
        cache_creation_tokens=0,
    )


# --------------------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------------------


class TestLookupAtAnEndpoint:
    def test_a_relax_call_is_priced_from_relaxs_own_table(self):
        lookup = pricing.look_up_price("openai/DeepSeek-V4-Pro", api_base=RELAX)

        assert lookup.provider == "relax"
        assert lookup.source == "gateway_table"
        assert lookup.priced_as == "deepseek-v4-pro"
        assert lookup.price is pricing.RELAX_PRICES["deepseek-v4-pro"]
        assert lookup.price.currency == "GBP"
        assert lookup.unknown_endpoint is False

    def test_a_name_other_vendors_also_sell_is_not_priced_at_their_rate(self):
        # The collision is real: without an endpoint, litellm's catalogue prices this name
        # at DeepSeek's own rate. If litellm ever drops the entry this precondition fails
        # loudly rather than letting the assertion below pass vacuously.
        by_name = pricing.look_up_price("openai/DeepSeek-V4-Pro")
        assert by_name.source == "litellm"
        assert by_name.provider == "openai"

        at_relax = pricing.look_up_price("openai/DeepSeek-V4-Pro", api_base=RELAX)
        assert at_relax.source == "gateway_table"
        assert at_relax.price.source_url == "https://relax.ai/docs/getting-started/pricing"

    def test_relax_rates_are_never_used_for_a_call_that_did_not_go_to_relax(self):
        lookup = pricing.look_up_price("deepseek-v41-flash")
        assert lookup.provider != "relax"
        assert lookup.price is None

    def test_a_model_relax_does_not_list_is_unpriced_but_still_attributed_to_relax(self):
        lookup = pricing.look_up_price("openai/Some-Future-Model", api_base=RELAX)

        assert lookup.price is None
        assert lookup.provider == "relax"
        assert lookup.source == "unknown"
        assert lookup.unknown_endpoint is False

    def test_a_relax_model_is_not_found_in_the_ordinary_table_by_accident(self):
        # claude-haiku-4-5 is in PRICES. Sent to relax.ai, it is still relax.ai billing
        # (or refusing) the call, so the Anthropic rate must not apply.
        lookup = pricing.look_up_price("claude-haiku-4-5", api_base=RELAX)
        assert lookup.price is None
        assert lookup.provider == "relax"

    def test_an_unknown_host_is_never_priced_even_for_a_known_model(self):
        lookup = pricing.look_up_price(
            "claude-haiku-4-5", api_base="https://llm-proxy.example.com/v1"
        )

        assert lookup.price is None
        assert lookup.provider == "llm-proxy.example.com"
        assert lookup.source == "unknown"
        assert lookup.unknown_endpoint is True

    def test_an_api_base_that_is_not_a_url_is_an_unknown_endpoint(self):
        lookup = pricing.look_up_price("claude-haiku-4-5", api_base="not a url")
        assert lookup.price is None
        assert lookup.unknown_endpoint is True

    @pytest.mark.parametrize(
        "api_base",
        [
            "https://api.anthropic.com",
            "https://api.openai.com/v1",
            "https://generativelanguage.googleapis.com/v1beta",
        ],
    )
    def test_a_first_party_host_prices_exactly_as_if_no_api_base_were_given(self, api_base):
        model = {
            "https://api.anthropic.com": "claude-haiku-4-5",
            "https://api.openai.com/v1": "gpt-4o",
            "https://generativelanguage.googleapis.com/v1beta": "gemini-2.5-flash",
        }[api_base]
        assert pricing.look_up_price(model, api_base=api_base) == pricing.look_up_price(model)

    @pytest.mark.parametrize("api_base", [None, "", "   "])
    def test_no_api_base_is_the_ordinary_lookup(self, api_base):
        lookup = pricing.look_up_price("claude-haiku-4-5", api_base=api_base)
        assert lookup.source == "gateway_table"
        assert lookup.provider == "anthropic"

    def test_host_matching_ignores_case_port_and_path(self):
        lookup = pricing.look_up_price(
            "openai/Muse-Glimmer-30B", api_base="HTTPS://API.Relax.AI:443/v1/"
        )
        assert lookup.provider == "relax"
        assert lookup.price is pricing.RELAX_PRICES["muse-glimmer-30b"]

    def test_a_lookalike_host_is_not_relax(self):
        lookup = pricing.look_up_price(
            "openai/DeepSeek-V4-Pro", api_base="https://api.relax.ai.example.com/v1"
        )
        assert lookup.provider != "relax"
        assert lookup.unknown_endpoint is True


# --------------------------------------------------------------------------------------
# Currency
# --------------------------------------------------------------------------------------


TOKENS = {
    "input_tokens": 1_000_000,
    "output_tokens": 1_000_000,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
}


class TestCurrency:
    def test_a_gbp_price_is_exact_in_pounds_and_converted_once_to_dollars(self):
        price = pricing.RELAX_PRICES["deepseek-v4-pro"]
        usd, gbp = pricing.costs_usd_and_gbp(price, usd_per_gbp=1.25, **TOKENS)

        assert gbp == pytest.approx(1.17 + 2.33)
        assert usd == pytest.approx((1.17 + 2.33) * 1.25)

    def test_a_usd_price_is_exact_in_dollars_and_converted_once_to_pounds(self):
        price = pricing.PRICES["claude-haiku-4-5"]
        usd, gbp = pricing.costs_usd_and_gbp(price, usd_per_gbp=1.25, **TOKENS)

        assert usd == pytest.approx(1.0 + 5.0)
        assert gbp == pytest.approx((1.0 + 5.0) / 1.25)

    def test_cost_usd_refuses_a_price_that_is_not_in_dollars(self):
        # It has no exchange rate. Returning the pound figure would be a confident wrong
        # number, labelled as dollars.
        assert pricing.cost_usd(pricing.RELAX_PRICES["deepseek-v4-pro"], **TOKENS) is None

    def test_an_unsupported_currency_prices_to_nothing(self):
        price = pricing.ModelPrice(
            input_usd_per_mtok=1.0,
            output_usd_per_mtok=1.0,
            cache_read_usd_per_mtok=None,
            cache_write_usd_per_mtok=None,
            source_url="https://example.com",
            checked="2026-09-29",
            currency="EUR",
        )
        assert pricing.costs_usd_and_gbp(price, usd_per_gbp=1.25, **TOKENS) == (None, None)

    def test_an_unusable_rate_nulls_only_the_converted_figure(self):
        price = pricing.RELAX_PRICES["deepseek-v4-pro"]
        usd, gbp = pricing.costs_usd_and_gbp(price, usd_per_gbp=0.0, **TOKENS)
        assert usd is None
        assert gbp == pytest.approx(1.17 + 2.33)

    def test_cached_tokens_on_relax_refuse_to_price_rather_than_price_as_free(self):
        price = pricing.RELAX_PRICES["deepseek-v4-pro"]
        usd, gbp = pricing.costs_usd_and_gbp(
            price, usd_per_gbp=1.25, **{**TOKENS, "cache_read_tokens": 500}
        )
        assert (usd, gbp) == (None, None)

    def test_gbp_to_usd(self):
        assert fx.gbp_to_usd(2.0, 1.3555) == pytest.approx(2.711)
        assert fx.gbp_to_usd(None, 1.3555) is None
        assert fx.gbp_to_usd(2.0, 0) is None


class TestRelaxProvenance:
    def test_every_relax_rate_carries_a_source_a_date_and_its_currency(self):
        for name, price in pricing.RELAX_PRICES.items():
            assert price.source_url.startswith("https://relax.ai/"), name
            assert price.checked == "2026-09-29", name
            assert price.currency == "GBP", name

    def test_every_rate_in_the_ordinary_table_is_in_dollars(self):
        for name, price in pricing.PRICES.items():
            assert price.currency == "USD", name


# --------------------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------------------


class TestRecord:
    def test_a_relax_call_is_recorded_as_relax_at_relaxs_rate(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(relax_response(response_factory))
        complete(model="openai/DeepSeek-V4-Pro", api_base=RELAX, messages=[], workload="w")
        row = read_rows(cost_log_file)[0]

        expected_gbp = (145 * 1.17 + 21 * 2.33) / 1_000_000
        assert row["provider"] == "relax"
        assert row["model_priced_as"] == "deepseek-v4-pro"
        assert row["pricing_source"] == "gateway_table"
        assert row["pricing_checked"] == "2026-09-29"
        assert row["pricing_caveat"] is None
        assert row["cost_gbp"] == pytest.approx(expected_gbp)
        assert row["cost_usd"] == pytest.approx(expected_gbp * row["fx_rate_usd_per_gbp"])

    def test_the_endpoint_and_key_reach_litellm_untouched(
        self, cost_log_file, stub_completion, response_factory
    ):
        calls = stub_completion(relax_response(response_factory))
        complete(
            model="openai/DeepSeek-V4-Pro",
            api_base=RELAX,
            api_key="test-key-not-real",
            messages=[],
            workload="w",
        )
        assert calls[0]["kwargs"]["api_base"] == RELAX
        assert calls[0]["kwargs"]["api_key"] == "test-key-not-real"
        assert calls[0]["kwargs"]["model"] == "openai/DeepSeek-V4-Pro"

    def test_base_url_is_recognised_as_well_as_api_base(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(relax_response(response_factory))
        complete(model="openai/DeepSeek-V4-Pro", base_url=RELAX, messages=[], workload="w")
        assert read_rows(cost_log_file)[0]["provider"] == "relax"

    def test_api_base_wins_over_base_url_as_it_does_in_litellm(self):
        assert call_endpoint({"api_base": RELAX, "base_url": "https://api.openai.com/v1"}) == RELAX
        assert call_endpoint({"api_base": "  ", "base_url": RELAX}) == RELAX
        assert call_endpoint({}) is None

    def test_an_unknown_endpoint_is_recorded_unpriced_with_the_reason(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory())
        complete(
            model="claude-haiku-4-5",
            api_base="https://llm-proxy.example.com",
            messages=[],
            workload="w",
        )
        row = read_rows(cost_log_file)[0]

        assert row["provider"] == "llm-proxy.example.com"
        assert row["pricing_caveat"] == "custom_endpoint_unpriced"
        assert row["cost_usd"] is None
        assert row["cost_gbp"] is None
        assert row["measured"] is True
        assert row["input_tokens"] == 1200

    def test_an_unknown_endpoint_is_the_reason_given_even_when_a_modifier_is_present(
        self, cost_log_file, stub_completion, response_factory
    ):
        # Who billed the call is the more fundamental unknown; naming fast mode instead
        # would suggest the cost could be worked out once fast mode is priced.
        stub_completion(response_factory())
        complete(
            model="claude-haiku-4-5",
            api_base="https://llm-proxy.example.com",
            speed="fast",
            messages=[],
            workload="w",
        )
        assert read_rows(cost_log_file)[0]["pricing_caveat"] == "custom_endpoint_unpriced"

    def test_a_retired_model_is_priced_at_what_relax_actually_ran(
        self, cost_log_file, stub_completion, response_factory
    ):
        # relax.ai routes a retired id to its replacement and bills the replacement.
        stub_completion(relax_response(response_factory, model="DeepSeek-V4-Pro"))
        complete(model="openai/GLM-46", api_base=RELAX, messages=[], workload="w")
        row = read_rows(cost_log_file)[0]

        assert row["provider"] == "relax"
        assert row["model_priced_as"] == "deepseek-v4-pro"
        assert row["cost_gbp"] == pytest.approx((145 * 1.17 + 21 * 2.33) / 1_000_000)

    def test_a_failed_relax_call_is_still_attributed_to_relax(self, cost_log_file, stub_completion):
        stub_completion(raises=RuntimeError("provider down"))
        with pytest.raises(RuntimeError):
            complete(model="openai/DeepSeek-V4-Pro", api_base=RELAX, messages=[], workload="w")
        row = read_rows(cost_log_file)[0]

        assert row["status"] == "error"
        assert row["provider"] == "relax"

    def test_no_api_base_records_exactly_as_before(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory())
        complete(model="claude-haiku-4-5", messages=[], workload="w")
        row = read_rows(cost_log_file)[0]

        assert row["provider"] == "anthropic"
        assert row["cost_usd"] == pytest.approx(0.0062)
        assert row["cost_gbp"] == pytest.approx(0.0062 / 1.3555)


class TestCeilingsAndLadders:
    def test_the_monthly_ceiling_counts_relax_spend(
        self, monkeypatch, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(relax_response(response_factory, prompt=1_000_000, completion=0))
        complete(model="openai/DeepSeek-V4-Pro", api_base=RELAX, messages=[], workload="w")
        assert read_rows(cost_log_file)[0]["cost_gbp"] == pytest.approx(1.17)

        # £1.17 already spent; a £1 ceiling must now refuse.
        monkeypatch.setenv(BUDGET_VAR, "1")
        with pytest.raises(BudgetExceeded):
            complete(model="openai/DeepSeek-V4-Pro", api_base=RELAX, messages=[], workload="w")

    def test_a_ladder_sends_every_rung_to_the_same_endpoint(
        self, cost_log_file, stub_completion, response_factory
    ):
        # A known limitation, pinned so it cannot change unnoticed: api_base is one keyword
        # argument, not per rung, so a ladder cannot mix relax.ai with another provider.
        calls = stub_completion(relax_response(response_factory))
        complete(
            model="openai/Muse-Glimmer-30B",
            api_base=RELAX,
            ladder=["openai/Muse-Glimmer-30B", "openai/DeepSeek-V4-Pro"],
            escalate_when=lambda _response: True,
            messages=[],
            workload="w",
        )
        assert [c["kwargs"]["api_base"] for c in calls] == [RELAX, RELAX]
        rows = read_rows(cost_log_file)
        assert [r["provider"] for r in rows] == ["relax", "relax"]
        assert [r["model_priced_as"] for r in rows] == ["muse-glimmer-30b", "deepseek-v4-pro"]
