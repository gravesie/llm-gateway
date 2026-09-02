"""Exchange-rate resolution and conversion."""

from __future__ import annotations

import pytest

from llm_gateway import fx


class TestRateResolution:
    def test_default_is_used_when_nothing_is_set(self):
        rate, source = fx.resolve_rate({})
        assert rate == fx.DEFAULT_USD_PER_GBP
        assert source == "default"

    def test_environment_overrides_the_default(self):
        rate, source = fx.resolve_rate({fx.ENV_VAR: "1.29"})
        assert rate == 1.29
        assert source == "env"

    def test_surrounding_whitespace_is_tolerated(self):
        rate, source = fx.resolve_rate({fx.ENV_VAR: "  1.29  "})
        assert rate == 1.29
        assert source == "env"

    def test_an_empty_override_falls_back_quietly(self):
        rate, source = fx.resolve_rate({fx.ENV_VAR: "   "})
        assert rate == fx.DEFAULT_USD_PER_GBP
        assert source == "default"

    @pytest.mark.parametrize("value", ["not-a-number", "0", "-1.3", "nan", "inf"])
    def test_an_unusable_override_is_reported_distinctly(self, value):
        """A typo in a consumer's environment must be visible in the data.

        Falling back silently would make a misconfigured run indistinguishable from one
        that never set the variable at all.
        """
        rate, source = fx.resolve_rate({fx.ENV_VAR: value})
        assert rate == fx.DEFAULT_USD_PER_GBP
        assert source == "default_env_invalid"


class TestConversion:
    def test_dollars_convert_to_pounds(self):
        assert fx.usd_to_gbp(0.0062, 1.3555) == pytest.approx(0.0062 / 1.3555)

    def test_no_dollars_means_no_pounds(self):
        assert fx.usd_to_gbp(None, 1.3555) is None

    def test_a_nonsense_rate_produces_no_figure_rather_than_a_wrong_one(self):
        assert fx.usd_to_gbp(1.0, 0.0) is None
        assert fx.usd_to_gbp(1.0, -1.0) is None

    def test_the_documented_default_matches_its_recorded_source(self):
        assert fx.DEFAULT_SOURCE_URL.startswith("https://www.federalreserve.gov/")
        assert fx.DEFAULT_RATE_DATE == "2026-08-28"
