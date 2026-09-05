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
WORKLOAD_VAR = "LLM_GATEWAY_WORKLOAD_BUDGETS_GBP"


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
    workload: str | None = "test",
) -> str:
    """One cost-log line, shaped like the ones ``cost_log`` writes.

    ``workload=None`` writes a line with no label at all, which is what a record from a
    future schema, or a hand-edited log, could look like.
    """
    if timestamp is None:
        timestamp = f"{month or this_month()}-15T12:00:00.000000+00:00"
    return (
        json.dumps(
            {
                "schema": 2,
                "timestamp": timestamp,
                "workload": workload,
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


@pytest.fixture
def workload_limits(monkeypatch):
    """Set the per-workload ceilings for a test.

    Takes a mapping, which is serialised the way an operator would write it in ``.env``, or
    a raw string when the point of the test is that the value is unusable.
    """

    def install(value):
        monkeypatch.setenv(
            WORKLOAD_VAR, value if isinstance(value, str) else json.dumps(value)
        )

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

    Written before anything read the variable, so that wiring it into the ceiling would
    fail the suite. It is now wired — see ``TestBypass`` in ``test_routing.py`` — and this
    is still the test that fixes the decision. An env var that silently switches off a
    spend cap would get set during an incident, which is precisely when the cap matters
    most.
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


# --------------------------------------------------------------------------------------
# Per-workload ceilings
#
# The global ceiling stops everything when any one consumer runs away. These cap a
# consumer against its own label instead, so the others keep working. The label is the one
# already on every cost record, and a key matches it exactly or as a ":"-boundary prefix.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "{}"])
def test_an_empty_workload_map_is_the_same_as_none(value, workload_limits, monkeypatch):
    """`.env.example` ships the variable blank, and `{}` is a deliberate 'none of these'."""

    def explode(_path):
        raise AssertionError("the ledger was read when no ceiling was configured")

    monkeypatch.setattr(budget, "_refresh", explode)
    workload_limits(value)

    status = budget_status(workload="web-auditor")
    assert status.verdict == budget.VERDICT_NO_LIMIT
    assert status.limit_gbp is None


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ("not json at all", "not valid JSON"),
        ("[1, 2]", "not an object"),
        ('"web-auditor"', "not an object"),
        ('{"web-auditor": "5"}', "not a number"),
        ('{"web-auditor": null}', "not a number"),
        ('{"web-auditor": true}', "not a number"),
        ('{"web-auditor": 1e999}', "not finite"),
        ('{"web-auditor": -1}', "negative"),
        ('{"": 5}', "blank workload label"),
        ('{"   ": 5}', "blank workload label"),
        ('{"web-auditor": 5, "web-auditor ": 6}', "more than once"),
    ],
)
def test_an_unusable_workload_map_is_refused_rather_than_ignored(
    value, fragment, workload_limits, cost_log_file
):
    """A typo must not leave a workload uncapped.

    Same reasoning as the global ceiling: setting one is a deliberate act, so a value that
    cannot be used is a decision to refuse, not a fault to fail open on. Silently ignoring
    it would leave a control that reads as handled and enforces nothing.
    """
    workload_limits(value)

    with pytest.raises(BudgetMisconfigured) as raised:
        budget.enforce(workload="web-auditor")

    assert WORKLOAD_VAR in str(raised.value)
    assert fragment in str(raised.value)


def test_a_zero_workload_ceiling_stops_only_that_workload(
    workload_limits, cost_log_file, stub_completion, response_factory
):
    """Zero is a kill switch for one consumer, and must not become one for the others."""
    workload_limits({"moto": 0})
    response = response_factory()
    calls = stub_completion(result=response)

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="moto:bulk")
    assert calls == []

    assert complete(model="claude-haiku-4-5", messages=[], workload="web-auditor") is response
    assert len(calls) == 1


def test_a_workload_ceiling_without_a_cost_log_refuses_every_call(workload_limits):
    """The cost log is the only ledger there is, on this axis as much as the global one."""
    workload_limits({"web-auditor": 5})

    with pytest.raises(BudgetMisconfigured) as raised:
        budget.enforce(workload="web-auditor")

    message = str(raised.value)
    assert "LLM_GATEWAY_COST_LOG" in message
    assert WORKLOAD_VAR in message, "the message must name the variable that is set"


# --------------------------------------------------------------------------------------
# Which keys govern which labels
# --------------------------------------------------------------------------------------


def test_an_exact_key_caps_that_workload(workload_limits, cost_log_file):
    workload_limits({"web-auditor:crawl": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.00, workload="web-auditor:crawl"))

    status = budget_status(workload="web-auditor:crawl")
    assert status.verdict == budget.VERDICT_EXCEEDED
    assert status.scope == budget.SCOPE_WORKLOAD
    assert status.scope_key == "web-auditor:crawl"


def test_a_prefix_key_caps_everything_under_it(workload_limits, cost_log_file):
    """The point of the hierarchy: cap an application without listing its operations."""
    workload_limits({"web-auditor": 1.00})
    seed(
        cost_log_file,
        record_line(cost_gbp=0.60, workload="web-auditor:crawl"),
        record_line(cost_gbp=0.45, workload="web-auditor:summary"),
    )

    status = budget_status(workload="web-auditor:crawl")
    assert status.verdict == budget.VERDICT_EXCEEDED
    assert status.scope_key == "web-auditor"
    assert status.spent_gbp == pytest.approx(1.05), "both operations count against the app"


def test_every_level_of_the_hierarchy_applies_at_once(workload_limits, cost_log_file):
    """A tight ceiling on one operation binds even when the application's has headroom."""
    workload_limits({"web-auditor": 10.00, "web-auditor:crawl": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.20, workload="web-auditor:crawl:page"))

    status = budget_status(workload="web-auditor:crawl:page")
    assert status.verdict == budget.VERDICT_EXCEEDED
    assert status.scope_key == "web-auditor:crawl", "the more specific ceiling decides"


def test_a_sibling_workload_is_unaffected(workload_limits, cost_log_file):
    """The whole reason this exists: one consumer's spend must not refuse another's call."""
    workload_limits({"moto": 1.00, "web-auditor": 1.00})
    seed(cost_log_file, record_line(cost_gbp=5.00, workload="moto:bulk"))

    assert budget_status(workload="moto:bulk").verdict == budget.VERDICT_EXCEEDED
    assert budget_status(workload="web-auditor:crawl").verdict == budget.VERDICT_WITHIN


def test_a_key_only_matches_at_a_segment_boundary(workload_limits, cost_log_file):
    """A raw string prefix would make a ceiling on "web" quietly cap "web-auditor"."""
    workload_limits({"web": 1.00})
    seed(cost_log_file, record_line(cost_gbp=5.00, workload="web-auditor:crawl"))

    status = budget_status(workload="web-auditor:crawl")
    assert status.verdict == budget.VERDICT_NO_LIMIT, "'web' must not govern 'web-auditor'"


def test_a_configured_key_is_trimmed_the_way_a_label_is(workload_limits, cost_log_file):
    """A key differing only by whitespace would otherwise be a ceiling that caps nothing."""
    workload_limits({"  web-auditor  ": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="web-auditor:crawl"))

    assert budget_status(workload="web-auditor:crawl").verdict == budget.VERDICT_EXCEEDED


def test_the_label_asked_about_is_trimmed_too(workload_limits, cost_log_file):
    """``budget_status`` takes a label straight from a caller, unlike ``complete``.

    ``complete`` sanitises the label before asking, but a consumer sizing a batch calls
    ``budget_status(workload=...)`` itself, and a stray space must not quietly answer that
    no ceiling applies.
    """
    workload_limits({"moto": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))

    assert budget_status(workload="  moto:bulk  ").verdict == budget.VERDICT_EXCEEDED


# --------------------------------------------------------------------------------------
# Layering the two axes
# --------------------------------------------------------------------------------------


def test_the_global_ceiling_still_refuses_when_the_workload_is_within(
    limit, workload_limits, cost_log_file, stub_completion
):
    """Adding a second axis must not open a way round the first."""
    limit(1.00)
    workload_limits({"web-auditor": 100.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))
    calls = stub_completion(result=object())

    with pytest.raises(BudgetExceeded) as raised:
        complete(model="claude-haiku-4-5", messages=[], workload="web-auditor:crawl")

    assert calls == []
    assert raised.value.status.scope == budget.SCOPE_GLOBAL


def test_a_workload_ceiling_refuses_while_the_global_one_has_headroom(
    limit, workload_limits, cost_log_file, stub_completion
):
    limit(100.00)
    workload_limits({"moto": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))
    calls = stub_completion(result=object())

    with pytest.raises(BudgetExceeded) as raised:
        complete(model="claude-haiku-4-5", messages=[], workload="moto:bulk")

    assert calls == []
    assert raised.value.status.scope == budget.SCOPE_WORKLOAD
    assert raised.value.status.scope_key == "moto"


def test_when_both_are_exceeded_the_global_ceiling_is_reported(
    limit, workload_limits, cost_log_file
):
    """Raising the workload's ceiling would not let the call through; say so."""
    limit(1.00)
    workload_limits({"moto": 1.00})
    seed(cost_log_file, record_line(cost_gbp=2.00, workload="moto:bulk"))

    status = budget_status(workload="moto:bulk")
    assert status.verdict == budget.VERDICT_EXCEEDED
    assert status.scope == budget.SCOPE_GLOBAL


def test_the_tightest_ceiling_is_reported_when_none_refuses(
    limit, workload_limits, cost_log_file
):
    """A consumer sizing a batch needs the ceiling that bites next, not an arbitrary one."""
    limit(100.00)
    workload_limits({"moto": 2.00})
    seed(cost_log_file, record_line(cost_gbp=1.00, workload="moto:bulk"))

    status = budget_status(workload="moto:bulk")
    assert status.verdict == budget.VERDICT_WITHIN
    assert status.scope_key == "moto"
    assert status.remaining_gbp == pytest.approx(1.00)


def test_budget_status_without_a_workload_answers_about_the_global_ceiling(
    limit, workload_limits, cost_log_file
):
    """There is no honest per-workload answer for a label that was not named."""
    limit(100.00)
    workload_limits({"moto": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))

    status = budget_status()
    assert status.verdict == budget.VERDICT_WITHIN
    assert status.scope == budget.SCOPE_GLOBAL
    assert status.spent_gbp == pytest.approx(1.50), "the month's total, not a workload's"


def test_a_workload_no_key_governs_is_not_capped(
    workload_limits, cost_log_file, stub_completion, response_factory
):
    """Per-workload ceilings cap what they name. Anything else is the global ceiling's job."""
    workload_limits({"moto": 0})
    seed(cost_log_file, record_line(cost_gbp=9.99, workload="moto:bulk"))
    response = response_factory()
    calls = stub_completion(result=response)

    status = budget_status(workload="web-auditor")
    assert status.verdict == budget.VERDICT_NO_LIMIT
    assert status.spent_gbp == pytest.approx(9.99), "the month's spend is still reported"

    assert complete(model="claude-haiku-4-5", messages=[], workload="web-auditor") is response
    assert len(calls) == 1


# --------------------------------------------------------------------------------------
# The ledger, with a second axis on it
# --------------------------------------------------------------------------------------


def test_only_configured_keys_are_bucketed(workload_limits, cost_log_file):
    """Memory must be a function of the configuration, not of a consumer's naming.

    A caller is free to invent a label per call. Totalling every label ever logged would
    make a long-running process's memory grow with the log.
    """
    workload_limits({"moto": 5.00})
    seed(
        cost_log_file,
        record_line(cost_gbp=0.10, workload="moto:bulk"),
        *[record_line(cost_gbp=0.01, workload=f"web-auditor:page-{n}") for n in range(50)],
    )

    budget_status(workload="moto:bulk")

    buckets = budget._ledgers[cost_log_file].by_workload[this_month()]
    assert set(buckets) == {"moto"}


def test_changing_the_configured_keys_rescans_the_ledger(workload_limits, cost_log_file):
    """Attribution happens as lines are folded, so an old set's totals are incomplete."""
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))

    workload_limits({"web-auditor": 5.00})
    assert budget_status(workload="web-auditor").verdict == budget.VERDICT_WITHIN

    workload_limits({"moto": 1.00})
    status = budget_status(workload="moto:bulk")
    assert status.verdict == budget.VERDICT_EXCEEDED, "spend read before the change was lost"
    assert status.spent_gbp == pytest.approx(1.50)


def test_a_fault_during_a_rescan_keeps_the_totals_it_had(
    limit, workload_limits, cost_log_file, monkeypatch
):
    """Changing the configuration must not be a moment at which a ceiling can be lifted.

    The rescan builds a detached ledger and publishes it only once the read has succeeded.
    Clearing the cached totals first would mean an I/O blip during a config change fell
    back to zero spend, which is a fault re-opening the tap.
    """
    limit(1.00)
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))
    assert budget_status().verdict == budget.VERDICT_EXCEEDED

    workload_limits({"moto": 5.00})  # a different key set forces a full rescan

    def unreadable(_ledger, _path):
        raise OSError("the cost log went away mid-rescan")

    monkeypatch.setattr(budget, "_read_into", unreadable)

    status = budget_status(workload="moto:bulk")
    assert status.verdict == budget.VERDICT_EXCEEDED, "a fault mid-rescan lifted the ceiling"
    assert status.spent_gbp == pytest.approx(1.50)
    assert status.fault is not None


def test_unpriced_calls_are_counted_per_workload_never_estimated(
    workload_limits, cost_log_file
):
    """Real spend this axis cannot see. Counted and reported, never guessed at."""
    workload_limits({"moto": 1.00})
    seed(
        cost_log_file,
        record_line(cost_gbp=None, workload="moto:bulk"),
        record_line(cost_gbp=None, workload="web-auditor:crawl"),
    )

    status = budget_status(workload="moto:bulk")
    assert status.verdict == budget.VERDICT_WITHIN
    assert status.spent_gbp == 0.0
    assert status.unpriced_calls == 1, "only this workload's unpriced calls"


def test_a_record_without_a_usable_label_still_counts_globally(
    limit, workload_limits, cost_log_file
):
    """Spend we know about but cannot place is not a reason to discard the line."""
    limit(1.00)
    workload_limits({"moto": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload=None))

    assert budget_status().verdict == budget.VERDICT_EXCEEDED
    assert budget_status().fault is None, "an unattributable label is not a corrupt line"
    assert budget_status(workload="moto:bulk").scope == budget.SCOPE_GLOBAL


def test_a_read_fault_keeps_the_workload_total_it_already_had(
    workload_limits, cost_log_file, stub_completion, monkeypatch
):
    """A fault must not lift a per-workload ceiling any more than the global one."""
    workload_limits({"moto": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))
    assert budget_status(workload="moto:bulk").verdict == budget.VERDICT_EXCEEDED

    def unreadable(_path):
        raise OSError("the cost log went away mid-run")

    monkeypatch.setattr(budget, "_refresh", unreadable)
    calls = stub_completion(result=object())

    status = budget_status(workload="moto:bulk")
    assert status.verdict == budget.VERDICT_EXCEEDED, "a read fault lifted the ceiling"
    assert status.spent_gbp == pytest.approx(1.50)
    assert status.fault is not None

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="moto:bulk")
    assert calls == []


def test_a_corrupt_line_still_fails_open_with_the_workload_axis_on(
    workload_limits, cost_log_file, stub_completion, response_factory
):
    """Faults fail open; decisions do not. Adding an axis does not change which is which."""
    workload_limits({"moto": 5.00})
    seed(cost_log_file, "{not json at all\n", record_line(cost_gbp=0.10, workload="moto:bulk"))
    response = response_factory()
    calls = stub_completion(result=response)

    status = budget_status(workload="moto:bulk")
    assert status.verdict == budget.VERDICT_WITHIN
    assert status.fault is not None and "unreadable line" in status.fault

    assert complete(model="claude-haiku-4-5", messages=[], workload="moto:bulk") is response
    assert len(calls) == 1


# --------------------------------------------------------------------------------------
# What the rest of the library sees
# --------------------------------------------------------------------------------------


def test_a_workload_refusal_is_recorded_with_its_own_reason(
    workload_limits, cost_log_file, stub_completion
):
    """Which ceiling refused is not inferable from the record; only the config knows."""
    workload_limits({"moto": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))
    stub_completion(result=object())

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="moto:bulk")

    written = json.loads(cost_log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert written["status"] == "refused"
    assert written["reason"] == "budget_exceeded_workload"
    assert written["workload"] == "moto:bulk"


def test_the_refusal_carries_the_status_that_produced_it(workload_limits, cost_log_file):
    """A consumer must be able to ask which ceiling stopped it without parsing English."""
    workload_limits({"moto": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))

    with pytest.raises(BudgetExceeded) as raised:
        budget.enforce(workload="moto:bulk")

    status = raised.value.status
    assert status is not None
    assert status.scope == budget.SCOPE_WORKLOAD
    assert status.scope_key == "moto"
    assert status.spent_gbp == pytest.approx(1.50)
    assert "moto" in str(raised.value) and WORKLOAD_VAR in str(raised.value)


def test_bypass_does_not_disable_a_workload_ceiling(
    workload_limits, cost_log_file, stub_completion, monkeypatch
):
    """``LLM_GATEWAY_BYPASS`` switches off routing and nothing else. Both axes, both times."""
    monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
    workload_limits({"moto": 1.00})
    seed(cost_log_file, record_line(cost_gbp=1.50, workload="moto:bulk"))
    calls = stub_completion(result=object())

    with pytest.raises(BudgetExceeded):
        complete(model="claude-haiku-4-5", messages=[], workload="moto:bulk")

    assert calls == []


def test_a_workload_ceiling_is_re_checked_before_every_rung(
    workload_limits, cost_log_file, stub_completion, response_factory
):
    """A ladder makes several billable calls, and the second must see the first's spend.

    The refusal lands after an answer has already been paid for, so that answer is
    returned rather than discarded — the ceiling stops further spend, it does not throw
    away what the money already bought.
    """
    workload_limits({"moto": 0.001})
    response = response_factory()
    calls = stub_completion(result=response)

    returned = complete(
        messages=[],
        workload="moto:bulk",
        ladder=["claude-haiku-4-5", "claude-sonnet-5"],
        escalate_when=lambda _response: True,
    )

    assert returned is response
    assert len(calls) == 1, "the second rung was attempted despite the ceiling"

    written = [
        json.loads(line) for line in cost_log_file.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["status"] for record in written] == ["ok", "refused"]
    assert written[-1]["reason"] == "budget_exceeded_workload"
    assert written[0]["chain_id"] == written[-1]["chain_id"]
