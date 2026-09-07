"""The wrapper: what it records, and what it refuses to let break the call."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from llm_gateway import complete, pricing
from llm_gateway.completion import extract_usage, sanitise_workload


def read_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestHappyPath:
    def test_the_response_is_returned_unchanged(
        self, cost_log_file, stub_completion, response_factory
    ):
        expected = response_factory()
        stub_completion(expected)
        assert complete(model="claude-haiku-4-5", messages=[], workload="w") is expected

    def test_one_call_writes_one_record(self, cost_log_file, stub_completion, response_factory):
        stub_completion(response_factory())
        complete(model="claude-haiku-4-5", messages=[], workload="audit:summary")
        complete(model="claude-haiku-4-5", messages=[], workload="audit:summary")
        assert len(read_rows(cost_log_file)) == 2

    def test_the_record_carries_the_facts_asked_for(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory())
        complete(model="claude-haiku-4-5", messages=[], workload="audit:summary")
        row = read_rows(cost_log_file)[0]

        assert row["workload"] == "audit:summary"
        assert row["model"] == "claude-haiku-4-5"
        assert row["provider"] == "anthropic"
        assert row["status"] == "ok"
        assert row["measured"] is True
        assert row["input_tokens"] == 1200
        assert row["output_tokens"] == 340
        assert row["cache_read_tokens"] == 8000
        assert row["cache_write_tokens"] == 2000
        assert row["cost_usd"] == pytest.approx(0.0062)
        assert row["cost_gbp"] == pytest.approx(0.0062 / 1.3555)
        assert row["latency_ms"] >= 0
        assert row["timestamp"].endswith("+00:00")
        assert row["response_id"] == "chatcmpl-test-1"
        assert row["pricing_source"] == "gateway_table"
        # Against the constant, not a literal: what this asserts is that the record
        # carries the table's own check date through, and a literal here just means a
        # second file to edit on every price sweep. Provenance itself is covered by
        # test_pricing.py::TestProvenance.
        assert row["pricing_checked"] == pricing._CHECKED
        assert row["fx_rate_usd_per_gbp"] == 1.3555

    def test_arguments_reach_litellm_untouched(
        self, cost_log_file, stub_completion, response_factory
    ):
        calls = stub_completion(response_factory())
        messages = [{"role": "user", "content": "hello"}]
        complete(model="claude-haiku-4-5", messages=messages, temperature=0.2, workload="w")

        assert calls[0]["kwargs"] == {
            "model": "claude-haiku-4-5",
            "messages": messages,
            "temperature": 0.2,
        }
        assert "workload" not in calls[0]["kwargs"]

    def test_a_positional_model_is_still_recorded(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory())
        complete("claude-haiku-4-5", [], workload="w")
        assert read_rows(cost_log_file)[0]["model"] == "claude-haiku-4-5"

    def test_the_environment_rate_is_used_and_recorded(
        self, cost_log_file, stub_completion, response_factory, monkeypatch
    ):
        monkeypatch.setenv("LLM_GATEWAY_USD_GBP_RATE", "1.25")
        stub_completion(response_factory())
        complete(model="claude-haiku-4-5", messages=[], workload="w")

        row = read_rows(cost_log_file)[0]
        assert row["fx_source"] == "env"
        assert row["fx_rate_usd_per_gbp"] == 1.25
        assert row["cost_gbp"] == pytest.approx(0.0062 / 1.25)


class TestTokenAccounting:
    def test_cache_tokens_are_not_billed_twice(self):
        """The regression this library would be worthless without.

        litellm reports prompt_tokens inclusive of cache reads and writes. Billing that
        figure at the full input rate would charge every cached token twice.
        """
        response = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=11200,
                completion_tokens=340,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=8000, cache_creation_tokens=2000
                ),
            )
        )
        assert extract_usage(response)["input_tokens"] == 1200

    def test_a_provider_reporting_exclusive_totals_is_not_understated(self):
        """The mirror-image failure: never clamp a negative remainder to zero."""
        response = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=1200,
                completion_tokens=340,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=8000, cache_creation_tokens=2000
                ),
            )
        )
        assert extract_usage(response)["input_tokens"] == 1200

    def test_provider_shaped_usage_fields_are_read_as_a_fallback(self):
        response = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=11200,
                completion_tokens=340,
                prompt_tokens_details=None,
                cache_read_input_tokens=8000,
                cache_creation_input_tokens=2000,
            )
        )
        assert extract_usage(response)["input_tokens"] == 1200

    def test_a_dict_shaped_usage_is_understood(self):
        response = {"usage": {"prompt_tokens": 500, "completion_tokens": 100}}
        tokens = extract_usage(response)
        assert tokens == {
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    def test_an_absent_usage_block_yields_zeros_rather_than_an_error(self):
        assert extract_usage(SimpleNamespace())["input_tokens"] == 0


class TestNoTextIsRecorded:
    def test_neither_prompt_nor_completion_reaches_the_log(
        self, cost_log_file, stub_completion, response_factory
    ):
        secret_prompt = "SUPERSECRET-PROMPT-TEXT-a1b2c3"
        secret_completion = "SUPERSECRET-COMPLETION-TEXT-d4e5f6"
        stub_completion(response_factory(content=secret_completion))

        complete(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": secret_prompt}],
            workload="w",
        )

        written = cost_log_file.read_text(encoding="utf-8")
        assert secret_prompt not in written
        assert secret_completion not in written
        assert "content" not in written
        assert "messages" not in written


class TestFailOpen:
    """A fault in measurement must never reach the application being measured."""

    def test_an_unwritable_log_does_not_break_the_call(
        self, tmp_path, monkeypatch, stub_completion, response_factory
    ):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("LLM_GATEWAY_COST_LOG", str(blocker / "spend.jsonl"))

        expected = response_factory()
        stub_completion(expected)
        assert complete(model="claude-haiku-4-5", messages=[], workload="w") is expected

    def test_a_broken_pricing_table_does_not_break_the_call(
        self, cost_log_file, monkeypatch, stub_completion, response_factory
    ):
        def explode(*_args, **_kwargs):
            raise RuntimeError("pricing is broken")

        monkeypatch.setattr(pricing, "look_up_price", explode)

        expected = response_factory()
        stub_completion(expected)
        assert complete(model="claude-haiku-4-5", messages=[], workload="w") is expected
        assert read_rows(cost_log_file) == []

    def test_an_unreadable_usage_object_does_not_break_the_call(
        self, cost_log_file, stub_completion
    ):
        class Hostile:
            model = "claude-haiku-4-5"
            id = "x"

            @property
            def usage(self):
                raise RuntimeError("usage exploded")

        expected = Hostile()
        stub_completion(expected)
        assert complete(model="claude-haiku-4-5", messages=[], workload="w") is expected

    def test_a_measurement_failure_is_warned_about_rather_than_hidden(
        self, cost_log_file, monkeypatch, stub_completion, response_factory, caplog
    ):
        def explode(*_args, **_kwargs):
            raise RuntimeError("pricing is broken")

        monkeypatch.setattr(pricing, "look_up_price", explode)
        stub_completion(response_factory())

        with caplog.at_level(logging.WARNING, logger="llm_gateway.completion"):
            complete(model="claude-haiku-4-5", messages=[], workload="w")

        assert any("could not record" in record.message for record in caplog.records)

    def test_an_unknown_model_records_a_null_cost_and_still_returns(
        self, cost_log_file, stub_completion, response_factory
    ):
        expected = response_factory(model="not-a-real-model-9000")
        stub_completion(expected)
        assert complete(model="not-a-real-model-9000", messages=[], workload="w") is expected

        row = read_rows(cost_log_file)[0]
        assert row["cost_usd"] is None
        assert row["cost_gbp"] is None
        assert row["pricing_source"] == "unknown"
        # The tokens are still known and recorded, even when the price is not.
        assert row["input_tokens"] == 1200

    def test_nothing_is_written_when_no_log_is_configured(
        self, monkeypatch, stub_completion, response_factory
    ):
        """An imported library must not start creating files in someone's working directory."""
        from llm_gateway import cost_log

        writes = []
        monkeypatch.setattr(cost_log, "write_record", lambda *a, **k: writes.append(a))

        expected = response_factory()
        stub_completion(expected)
        assert complete(model="claude-haiku-4-5", messages=[], workload="w") is expected
        assert writes == []


class TestProviderErrors:
    def test_the_exception_reaches_the_caller(self, cost_log_file, stub_completion):
        stub_completion(raises=ValueError("rate limited"))
        with pytest.raises(ValueError, match="rate limited"):
            complete(model="claude-haiku-4-5", messages=[], workload="w")

    def test_the_failure_is_recorded_without_the_message(self, cost_log_file, stub_completion):
        stub_completion(raises=ValueError("rate limited by upstream"))
        with pytest.raises(ValueError):
            complete(model="claude-haiku-4-5", messages=[], workload="w")

        row = read_rows(cost_log_file)[0]
        assert row["status"] == "error"
        assert row["error_type"] == "ValueError"
        assert row["measured"] is False
        assert row["reason"] == "call_failed"
        assert row["cost_usd"] is None
        # The exception text could contain anything, including prompt content.
        assert "rate limited by upstream" not in cost_log_file.read_text(encoding="utf-8")

    def test_a_recording_failure_does_not_mask_the_providers_error(
        self, cost_log_file, monkeypatch, stub_completion
    ):
        def explode(*_args, **_kwargs):
            raise RuntimeError("pricing is broken")

        monkeypatch.setattr(pricing, "look_up_price", explode)
        stub_completion(raises=ValueError("rate limited"))

        with pytest.raises(ValueError, match="rate limited"):
            complete(model="claude-haiku-4-5", messages=[], workload="w")


class TestStreaming:
    def test_the_stream_is_passed_through_untouched(
        self, cost_log_file, stub_completion, response_factory
    ):
        stream = iter(["chunk"])
        stub_completion(stream)
        assert complete(model="claude-haiku-4-5", messages=[], stream=True, workload="w") is stream

    def test_an_unmeasured_call_is_still_countable(
        self, cost_log_file, stub_completion
    ):
        stub_completion(iter(["chunk"]))
        complete(model="claude-haiku-4-5", messages=[], stream=True, workload="w")

        row = read_rows(cost_log_file)[0]
        assert row["status"] == "ok"
        assert row["measured"] is False
        assert row["reason"] == "streaming_not_instrumented"
        assert row["cost_usd"] is None
        assert row["input_tokens"] is None


class TestUnpricedModifiers:
    """Where a modifier changes the real bill, report nothing rather than a base rate."""

    def test_fast_mode_is_not_priced_at_standard_rates(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory(model="claude-opus-5"))
        complete(model="claude-opus-5", messages=[], speed="fast", workload="w")

        row = read_rows(cost_log_file)[0]
        assert row["pricing_caveat"] == "fast_mode"
        assert row["cost_usd"] is None
        assert row["input_tokens"] == 1200

    def test_non_global_inference_geography_is_not_priced(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory(model="claude-opus-5"))
        complete(model="claude-opus-5", messages=[], inference_geo="us", workload="w")
        assert read_rows(cost_log_file)[0]["pricing_caveat"] == "inference_geo_non_global"

    def test_global_inference_geography_prices_normally(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory())
        complete(model="claude-haiku-4-5", messages=[], inference_geo="global", workload="w")
        row = read_rows(cost_log_file)[0]
        assert row["pricing_caveat"] is None
        assert row["cost_usd"] == pytest.approx(0.0062)

    def test_hour_long_cache_writes_are_not_priced_as_five_minute_ones(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory(ephemeral_1h_input_tokens=2000))
        complete(model="claude-haiku-4-5", messages=[], workload="w")
        assert read_rows(cost_log_file)[0]["pricing_caveat"] == "cache_write_1h"

    def test_a_non_standard_service_tier_is_not_priced(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory(model="gpt-5.6-luna"))
        complete(model="gpt-5.6-luna", messages=[], service_tier="priority", workload="w")
        assert read_rows(cost_log_file)[0]["pricing_caveat"] == "service_tier_priority"

    def test_a_standard_service_tier_prices_normally(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(response_factory(service_tier="standard"))
        complete(model="claude-haiku-4-5", messages=[], workload="w")
        assert read_rows(cost_log_file)[0]["pricing_caveat"] is None

    def test_a_prompt_past_a_pricing_tier_is_not_priced_at_the_lower_rate(
        self, cost_log_file, stub_completion, response_factory
    ):
        stub_completion(
            response_factory(
                model="gemini-2.5-pro",
                prompt_tokens=250_000,
                cached_tokens=0,
                cache_creation_tokens=0,
            )
        )
        complete(model="gemini-2.5-pro", messages=[], workload="w")

        row = read_rows(cost_log_file)[0]
        assert row["pricing_caveat"] == "long_context_tier"
        assert row["cost_usd"] is None


class TestWorkloadLabel:
    def test_the_label_is_required(self, cost_log_file, stub_completion, response_factory):
        stub_completion(response_factory())
        with pytest.raises(TypeError):
            complete(model="claude-haiku-4-5", messages=[])

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_an_empty_label_becomes_unlabelled_rather_than_an_error(self, value):
        assert sanitise_workload(value) == "unlabelled"

    def test_a_long_label_is_truncated(self):
        assert len(sanitise_workload("x" * 5000)) == 200

    def test_a_non_string_label_is_coerced(self):
        assert sanitise_workload(42) == "42"

    def test_a_bad_label_never_stops_the_call(
        self, cost_log_file, stub_completion, response_factory
    ):
        expected = response_factory()
        stub_completion(expected)
        assert complete(model="claude-haiku-4-5", messages=[], workload=None) is expected
        assert read_rows(cost_log_file)[0]["workload"] == "unlabelled"
