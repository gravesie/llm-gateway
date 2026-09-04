"""The record shape and the JSON-lines writer."""

from __future__ import annotations

import json

import pytest

from llm_gateway import cost_log
from llm_gateway.cost_log import CostRecord


def make_record(**overrides) -> CostRecord:
    fields = {
        "timestamp": "2026-09-02T12:00:00+00:00",
        "workload": "test:unit",
        "status": "ok",
        "measured": True,
        "model": "claude-haiku-4-5",
        "model_resolved": "claude-haiku-4-5",
        "model_priced_as": "claude-haiku-4-5",
        "provider": "anthropic",
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_tokens": 8000,
        "cache_write_tokens": 2000,
        "latency_ms": 1843.2,
        "cost_usd": 0.0062,
        "cost_gbp": 0.004574,
        "pricing_source": "gateway_table",
        "pricing_checked": "2026-09-02",
        "pricing_caveat": None,
        "fx_rate_usd_per_gbp": 1.3555,
        "fx_source": "default",
    }
    fields.update(overrides)
    return CostRecord(**fields)


class TestLogPath:
    def test_no_path_when_the_variable_is_unset(self):
        assert cost_log.log_path({}) is None

    def test_no_path_when_the_variable_is_blank(self):
        assert cost_log.log_path({cost_log.ENV_VAR: "  "}) is None

    def test_the_configured_path_is_used(self, tmp_path):
        target = tmp_path / "spend.jsonl"
        assert cost_log.log_path({cost_log.ENV_VAR: str(target)}) == target


class TestWriting:
    def test_a_record_is_one_parseable_line(self, tmp_path):
        path = tmp_path / "spend.jsonl"
        cost_log.write_record(make_record(), path)

        contents = path.read_text(encoding="utf-8")
        assert contents.endswith("\n")
        assert contents.count("\n") == 1

        row = json.loads(contents)
        assert row["workload"] == "test:unit"
        assert row["cost_usd"] == 0.0062
        assert row["schema"] == cost_log.SCHEMA_VERSION

    def test_records_append_rather_than_replace(self, tmp_path):
        path = tmp_path / "spend.jsonl"
        cost_log.write_record(make_record(workload="first"), path)
        cost_log.write_record(make_record(workload="second"), path)

        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [row["workload"] for row in rows] == ["first", "second"]

    def test_missing_directories_are_created(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "spend.jsonl"
        cost_log.write_record(make_record(), path)
        assert path.exists()

    def test_lines_end_with_a_bare_newline_on_every_platform(self, tmp_path):
        """A JSON-lines file should be byte-identical wherever it was produced."""
        path = tmp_path / "spend.jsonl"
        cost_log.write_record(make_record(), path)
        assert b"\r\n" not in path.read_bytes()

    def test_a_record_carries_every_field_a_reader_needs(self, tmp_path):
        path = tmp_path / "spend.jsonl"
        cost_log.write_record(make_record(), path)
        row = json.loads(path.read_text(encoding="utf-8"))

        expected = {
            "schema",
            "timestamp",
            "workload",
            "status",
            "measured",
            "model",
            "model_resolved",
            "model_priced_as",
            "provider",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "latency_ms",
            "cost_usd",
            "cost_gbp",
            "pricing_source",
            "pricing_checked",
            "pricing_caveat",
            "fx_rate_usd_per_gbp",
            "fx_source",
            "reason",
            "error_type",
            "response_id",
            "chain_id",
            "attempt",
            "ladder_size",
        }
        assert set(row) == expected

    def test_the_schema_version_moved_with_the_field_list(self):
        """Schema 3 added chain_id, attempt and ladder_size.

        Pinned deliberately. A reader summing spend per logical request has to group by
        chain_id rather than count lines once a ladder can make several billable attempts,
        and it can only know to do that from the version.
        """
        assert cost_log.SCHEMA_VERSION == 3

    def test_a_record_with_no_ladder_still_reads_as_a_chain_of_one(self, tmp_path):
        """The defaults have to describe a single call honestly, not merely parse.

        Every record carries these fields now, including the overwhelming majority written
        by callers who never touch escalation.
        """
        path = tmp_path / "spend.jsonl"
        cost_log.write_record(make_record(), path)
        row = json.loads(path.read_text(encoding="utf-8"))

        assert row["attempt"] == 1
        assert row["ladder_size"] == 1

    def test_the_writer_raises_rather_than_losing_a_record_silently(self, tmp_path):
        """cost_log itself is strict; the fail-open guard lives in the caller."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        with pytest.raises(OSError):
            cost_log.write_record(make_record(), blocker / "spend.jsonl")
