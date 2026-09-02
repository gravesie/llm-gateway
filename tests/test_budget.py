"""The monthly spend ceiling.

The single most important test in this file is
``test_an_exceeded_ceiling_refuses_before_the_provider_is_called``. Everything else here
guards a supporting property; that one guards the reason the module exists. A ceiling
checked after the provider has answered is a log entry, not a ceiling, and the failure is
invisible — the numbers still look right, the money is just gone.

The rest divides into three groups, matching the three rules the module has to hold at once:
configuration (a ceiling that cannot be enforced is refused, not ignored), enforcement (a
computed total that reaches the ceiling refuses), and fail-open (a *fault* in this machinery
never stops the application's call).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from llm_gateway import (
    BudgetExceeded,
    BudgetMisconfigured,
    GatewayError,
    budget,
    budget_status,
    complete,
)

BUDGET_VAR = "LLM_GATEWAY_MONTHLY_BUDGET_GBP"


def this_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def a_previous_month() -> str:
    """A ``YYYY-MM`` that is definitely not the current one."""
    now = datetime.now(UTC)
    year, month = (now.year - 1, now.month) if now.month == 1 else (now.year, now.month - 1)
    return f"{year:04d}-{month:02d}"


def record_line(
    *,
    cost_gbp: float | None = 0.5,
    month: str | None = None,
    status: str = "ok",
    timestamp: str | None = None,
) -> str:
    """One cost-log line, shaped like the ones ``cost_log`` writes."""
    if timestamp is None:
        timestamp = f"{month or this_month()}-15T12:00:00.000000+00:00"
    return (
        json.dumps(
            {
                "schema": 2,
                "timestamp": timestamp,
                "workload": "test",
                "status": status,
                "measured": cost_gbp is not None,
                "cost_usd": None if cost_gbp is None else cost_gbp * 1.3555,
                "cost_gbp": cost_gbp,
            }
        )
        + "\n"
    )


def seed(path, *lines: str) -> None:
    """Append raw lines to the cost log, creating it if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))


@pytest.fixture
def limit(monkeypatch):
    """Set the ceiling for a test."""

    def install(value):
        monkeypatch.setenv(BUDGET_VAR, str(value))

    return install


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_no_ceiling_allows_the_call_and_never_touches_the_ledger(monkeypatch):
    """With no ceiling configured the feature must cost nothing at all — not even a stat."""

    def explode(_path):
        raise AssertionError("the ledger was read when no ceiling was configured")

    monkeypatch.setattr(budget, "_refresh", explode)

    status = budget_status()

    assert status.verdict == budget.VERDICT_NO_LIMIT
    assert status.limit_gbp is None
    assert status.remaining_gbp is None


def test_an_empty_ceiling_is_the_same_as_no_ceiling(monkeypatch):
    """`.env.example` ships the variable blank; a copied .env must not start refusing."""
    monkeypatch.setenv(BUDGET_VAR, "   ")
    assert budget_status().verdict == budget.VERDICT_NO_LIMIT


def test_a_ceiling_without_a_cost_log_refuses_every_call(limit, stub_completion):
    """The ledger is the cost log. Without one the ceiling could only pretend to enforce."""
    limit(10)
    calls = stub_completion(result=object())

    with pytest.raises(BudgetMisconfigured) as raised:
        complete(model="claude-haiku-4-5", messages=[], workload="test")

    assert calls == [], "a call was made despite an unenforceable ceiling"
    assert "LLM_GATEWAY_COST_LOG" in str(raised.value)


@pytest.mark.parametrize("value", ["abc", "", "1,50", "ten", "nan", "inf", "-1", "-0.01"])
def test_an_unusable_ceiling_is_refused_rather_than_ignored(monkeypatch, value, cost_log_file):
    """Setting the variable is a deliberate act; a typo must not leave spend uncapped."""
    monkeypatch.setenv(BUDGET_VAR, value)

    if not value.strip():
        # The one exception: blank means "no ceiling", tested above.
        assert budget_status().verdict == budget.VERDICT_NO_LIMIT
        return

    assert budget_status().verdict == budget.VERDICT_MISCONFIGURED_LIMIT
    with pytest.raises(BudgetMisconfigured):
        budget.enforce()


def test_a_zero_ceiling_is_a_kill_switch(limit, cost_log_file, stub_completion):
    """Zero is a legitimate setting: spend nothing this month."""
    limit(0)
    calls = stub_completion(result=object())

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="test")

    assert calls == []


# --------------------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------------------


def test_an_exceeded_ceiling_refuses_before_the_provider_is_called(
    limit, cost_log_file, stub_completion
):
    """The whole point. The refusal must happen instead of the call, not after it."""
    limit(1.00)
    seed(cost_log_file, record_line(cost_gbp=0.60), record_line(cost_gbp=0.45))
    calls = stub_completion(result=object())

    with pytest.raises(BudgetExceeded) as raised:
        complete(model="claude-haiku-4-5", messages=[], workload="test")

    assert calls == [], "the provider was called despite the ceiling being reached"
    assert "1.05" in str(raised.value)


def test_spend_under_the_ceiling_allows_the_call(
    limit, cost_log_file, stub_completion, response_factory
):
    limit(1.00)
    seed(cost_log_file, record_line(cost_gbp=0.10))
    response = response_factory()
    calls = stub_completion(result=response)

    assert complete(model="claude-haiku-4-5", messages=[], workload="test") is response
    assert len(calls) == 1


def test_spending_exactly_the_ceiling_refuses(limit, cost_log_file):
    """At the ceiling is at the limit. There is nothing left to spend."""
    limit(1.00)
    seed(cost_log_file, record_line(cost_gbp=1.00))

    status = budget_status()
    assert status.verdict == budget.VERDICT_EXCEEDED
    assert status.remaining_gbp == 0.0
    with pytest.raises(BudgetExceeded):
        budget.enforce()


def test_a_missing_log_file_means_no_spend_yet(limit, cost_log_file):
    """First call of the month. Absent is not the same as unreadable."""
    limit(5)
    assert not cost_log_file.exists()

    status = budget_status()
    assert status.verdict == budget.VERDICT_WITHIN
    assert status.spent_gbp == 0.0
    assert status.fault is None


def test_only_the_current_utc_month_counts(limit, cost_log_file):
    """Last month's spend is last month's problem."""
    limit(1.00)
    seed(
        cost_log_file,
        record_line(cost_gbp=999.0, month=a_previous_month()),
        record_line(cost_gbp=0.25),
    )

    status = budget_status()
    assert status.verdict == budget.VERDICT_WITHIN
    assert status.spent_gbp == pytest.approx(0.25)
    assert status.month == this_month()


def test_only_calls_that_reached_a_provider_count_as_spend(limit, cost_log_file):
    """A refusal cost nothing, and a failed call is not billed."""
    limit(1.00)
    seed(
        cost_log_file,
        record_line(cost_gbp=0.20),
        record_line(cost_gbp=None, status="refused"),
        record_line(cost_gbp=None, status="error"),
    )

    status = budget_status()
    assert status.spent_gbp == pytest.approx(0.20)
    assert status.unpriced_calls == 0, "a refusal or an error is not unpriced spend"


def test_unpriced_calls_are_counted_never_estimated(limit, cost_log_file):
    """Streaming and caveated calls are real spend the ceiling cannot see. Say so."""
    limit(1.00)
    seed(
        cost_log_file,
        record_line(cost_gbp=0.10),
        record_line(cost_gbp=None),
        record_line(cost_gbp=None),
    )

    status = budget_status()
    assert status.spent_gbp == pytest.approx(0.10), "an unpriced call must not be guessed at"
    assert status.unpriced_calls == 2


def test_the_refusal_message_names_the_spend_it_cannot_see(limit, cost_log_file):
    limit(0.5)
    seed(cost_log_file, record_line(cost_gbp=0.9), record_line(cost_gbp=None))

    with pytest.raises(BudgetExceeded) as raised:
        budget.enforce()

    assert "1 call(s) this month had no cost figure" in str(raised.value)


def test_a_refusal_is_recorded_in_the_cost_log(limit, cost_log_file, stub_completion):
    """A refused call has to be countable, or the ceiling's effect is invisible."""
    limit(0.5)
    seed(cost_log_file, record_line(cost_gbp=0.9))
    stub_completion(result=object())

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="web-auditor:page")

    written = json.loads(cost_log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert written["status"] == "refused"
    assert written["reason"] == "budget_exceeded"
    assert written["error_type"] == "BudgetExceeded"
    assert written["measured"] is False
    assert written["cost_gbp"] is None
    assert written["latency_ms"] is None, "a call that never ran has no duration"
    assert written["workload"] == "web-auditor:page"


def test_a_misconfiguration_is_recorded_with_its_own_reason(limit, cost_log_file, stub_completion):
    """Distinguishable in the log from a genuine overspend, because the fix differs."""
    limit("not-a-number")
    stub_completion(result=object())

    with pytest.raises(BudgetMisconfigured):
        complete(model="claude-haiku-4-5", messages=[], workload="test")

    written = json.loads(cost_log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert written["status"] == "refused"
    assert written["reason"] == "budget_misconfigured"


def test_both_refusals_share_one_catchable_base(limit, cost_log_file):
    """A consumer must be able to tell 'we declined' from 'the provider failed'."""
    limit(0)
    with pytest.raises(GatewayError):
        budget.enforce()

    limit("nonsense")
    with pytest.raises(GatewayError):
        budget.enforce()


def test_a_refusal_is_not_swallowed_by_the_fail_open_guard(limit, cost_log_file, monkeypatch):
    """Recording a refusal is best-effort; the refusal itself is not."""
    limit(0)
    monkeypatch.setattr(
        budget.cost_log, "write_record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    )

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="test")


# --------------------------------------------------------------------------------------
# Fail open — a fault in this machinery must not stop the application
# --------------------------------------------------------------------------------------


def test_a_corrupt_line_is_skipped_and_the_call_proceeds(
    limit, cost_log_file, stub_completion, response_factory
):
    limit(1.00)
    seed(
        cost_log_file,
        record_line(cost_gbp=0.10),
        "{not json at all\n",
        record_line(cost_gbp=0.10),
    )
    response = response_factory()
    calls = stub_completion(result=response)

    status = budget_status()
    assert status.verdict == budget.VERDICT_WITHIN
    assert status.spent_gbp == pytest.approx(0.20)
    assert status.fault is not None and "unreadable line" in status.fault

    assert complete(model="claude-haiku-4-5", messages=[], workload="test") is response
    assert len(calls) == 1


def test_a_record_with_an_unusable_timestamp_is_treated_as_corrupt(limit, cost_log_file):
    limit(1.00)
    seed(cost_log_file, record_line(cost_gbp=5.0, timestamp="not-a-timestamp"))

    status = budget_status()
    assert status.spent_gbp == 0.0
    assert status.fault is not None


def test_an_unreadable_ledger_fails_open(limit, cost_log_file, stub_completion, monkeypatch):
    """CLAUDE.md: a fault in budgeting must never stop the application's call.

    ``_refresh`` is the boundary where I/O happens, so making it raise is exactly the fault
    ``evaluate`` has to absorb — and the assertion is that the call still goes out.
    """
    limit(1.00)
    seed(cost_log_file, record_line(cost_gbp=99.0))

    def unreadable(_path):
        raise OSError("the cost log could not be opened")

    monkeypatch.setattr(budget, "_refresh", unreadable)
    calls = stub_completion(result=object())

    status = budget_status()
    assert status.verdict == budget.VERDICT_WITHIN
    assert status.fault is not None and "OSError" in status.fault

    complete(model="claude-haiku-4-5", messages=[], workload="test")
    assert len(calls) == 1, "a fault in the ceiling stopped a call it should have allowed"


def test_a_timestamp_in_another_timezone_is_converted_not_assumed(limit, cost_log_file):
    """A local-offset timestamp near a month boundary must land in the right UTC month."""
    limit(10)
    # 1st of this month, 00:30 at UTC+13 — which is still the previous month in UTC.
    seed(cost_log_file, record_line(cost_gbp=4.0, timestamp=f"{this_month()}-01T00:30:00+13:00"))

    assert budget_status().spent_gbp == 0.0


# --------------------------------------------------------------------------------------
# The ledger cache
# --------------------------------------------------------------------------------------


@pytest.fixture
def fold_counter(monkeypatch):
    """Count how many cost-log lines get parsed, to prove reads stay incremental."""
    counter = {"lines": 0}
    original = budget._fold_line

    def counting(ledger, raw):
        counter["lines"] += 1
        return original(ledger, raw)

    monkeypatch.setattr(budget, "_fold_line", counting)
    return counter


def test_a_second_check_reads_only_the_new_bytes(limit, cost_log_file, fold_counter):
    """Re-summing the whole file per call would be O(file) on every provider call."""
    limit(100)
    seed(cost_log_file, *[record_line(cost_gbp=0.01) for _ in range(3)])

    budget_status()
    assert fold_counter["lines"] == 3

    seed(cost_log_file, record_line(cost_gbp=0.01))
    status = budget_status()

    assert fold_counter["lines"] == 4, "the ledger was re-read from the beginning"
    assert status.spent_gbp == pytest.approx(0.04)


def test_an_unchanged_ledger_is_not_re_read(limit, cost_log_file, fold_counter):
    limit(100)
    seed(cost_log_file, record_line(cost_gbp=0.01))

    budget_status()
    budget_status()
    budget_status()

    assert fold_counter["lines"] == 1


def test_a_partial_trailing_line_is_not_consumed_until_it_is_complete(limit, cost_log_file):
    """Another process may be mid-append; a torn read must not corrupt the total."""
    limit(100)
    complete_line = record_line(cost_gbp=0.30)
    seed(cost_log_file, record_line(cost_gbp=0.10), complete_line[:20])

    assert budget_status().spent_gbp == pytest.approx(0.10)

    seed(cost_log_file, complete_line[20:])
    assert budget_status().spent_gbp == pytest.approx(0.40)


def test_a_truncated_ledger_triggers_a_full_rescan(limit, cost_log_file):
    """Log rotation must not leave a stale offset pointing past the end of a new file."""
    limit(100)
    seed(cost_log_file, *[record_line(cost_gbp=1.0) for _ in range(5)])
    assert budget_status().spent_gbp == pytest.approx(5.0)

    cost_log_file.write_text(record_line(cost_gbp=0.25), encoding="utf-8", newline="")
    status = budget_status()

    assert status.spent_gbp == pytest.approx(0.25)
    assert budget._ledgers[cost_log_file].offset == cost_log_file.stat().st_size


def test_a_deleted_ledger_resets_the_total(limit, cost_log_file):
    limit(100)
    seed(cost_log_file, record_line(cost_gbp=3.0))
    assert budget_status().spent_gbp == pytest.approx(3.0)

    cost_log_file.unlink()
    assert budget_status().spent_gbp == 0.0


def test_each_log_path_gets_its_own_ledger(limit, tmp_path, monkeypatch):
    limit(100)
    first = tmp_path / "one.jsonl"
    second = tmp_path / "two.jsonl"
    seed(first, record_line(cost_gbp=2.0))
    seed(second, record_line(cost_gbp=0.5))

    monkeypatch.setenv("LLM_GATEWAY_COST_LOG", str(first))
    assert budget_status().spent_gbp == pytest.approx(2.0)

    monkeypatch.setenv("LLM_GATEWAY_COST_LOG", str(second))
    assert budget_status().spent_gbp == pytest.approx(0.5)


def test_a_line_longer_than_one_read_block_is_still_folded(limit, cost_log_file, monkeypatch):
    """A record must not be lost just because it straddles a block boundary."""
    limit(100)
    monkeypatch.setattr(budget, "_READ_BLOCK_BYTES", 16)
    seed(cost_log_file, record_line(cost_gbp=0.10), record_line(cost_gbp=0.20))

    assert budget_status().spent_gbp == pytest.approx(0.30)


# --------------------------------------------------------------------------------------
# Invariants that must survive the routing work still to come
# --------------------------------------------------------------------------------------


def test_bypass_does_not_disable_the_ceiling(limit, cost_log_file, stub_completion, monkeypatch):
    """``LLM_GATEWAY_BYPASS`` is for skipping the router, not the spend cap.

    Nothing reads it yet — routing is a later work unit — so this passes trivially today.
    It is here so that when routing lands, wiring bypass into the ceiling fails the suite.
    An env var that silently switches off a spend cap would get set during an incident,
    which is precisely when the cap matters most.
    """
    monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
    limit(0.5)
    seed(cost_log_file, record_line(cost_gbp=0.9))
    calls = stub_completion(result=object())

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="test")

    assert calls == []


def test_the_ceiling_applies_to_streaming_calls_too(limit, cost_log_file, stub_completion):
    """A streaming call spends money even though its cost cannot be measured."""
    limit(0.5)
    seed(cost_log_file, record_line(cost_gbp=0.9))
    calls = stub_completion(result=object())

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="test", stream=True)

    assert calls == []


def test_a_fault_after_a_good_read_keeps_the_total_it_already_had(
    limit, cost_log_file, stub_completion, monkeypatch
):
    """A read error must not silently lift the ceiling.

    Falling back to zero on a transient fault would mean any I/O blip re-opened the tap.
    Whatever was folded in before the fault is still known to have been spent.
    """
    limit(1.00)
    seed(cost_log_file, record_line(cost_gbp=1.50))
    assert budget_status().verdict == budget.VERDICT_EXCEEDED

    def unreadable(_path):
        raise OSError("the cost log went away mid-run")

    monkeypatch.setattr(budget, "_refresh", unreadable)
    calls = stub_completion(result=object())

    status = budget_status()
    assert status.verdict == budget.VERDICT_EXCEEDED, "a read fault lifted the ceiling"
    assert status.spent_gbp == pytest.approx(1.50)
    assert status.fault is not None

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="test")
    assert calls == []
