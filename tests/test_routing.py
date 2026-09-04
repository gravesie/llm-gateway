"""The escalation ladder: when it climbs, when it stops, and what it records.

The load-bearing claim of this module is that **every billable attempt produces its own
cost record**. A chain that reports one record for three paid calls understates spend, and
a cost library that understates spend is worse than no cost library, because the wrong
figure looks entirely plausible. Most of what is asserted below exists to defend that.
"""

from __future__ import annotations

import json
import logging

import pytest
from litellm import exceptions as litellm_exceptions

from llm_gateway import budget, complete, routing
from llm_gateway.completion import _call_arguments


def read_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def scripted_completion(monkeypatch):
    """Replace ``litellm.completion`` with a stub that plays one outcome per attempt.

    Deliberately strict: running off the end of the script is an error rather than a repeat
    of the last entry. A ladder making more calls than the test expected is precisely the
    bug worth catching, and silently serving it another response would hide it.
    """
    import litellm

    def install(*outcomes):
        calls = []

        def fake_completion(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            if len(calls) > len(outcomes):
                raise AssertionError(
                    f"litellm.completion was called {len(calls)} times; the script has only"
                    f" {len(outcomes)} outcome(s). The ladder climbed further than expected."
                )
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(litellm, "completion", fake_completion)
        return calls

    return install


def bad_request(message="malformed"):
    return litellm_exceptions.BadRequestError(
        message=message, model="claude-haiku-4-5", llm_provider="anthropic"
    )


def context_window_exceeded(message="too long"):
    return litellm_exceptions.ContextWindowExceededError(
        message=message, model="claude-haiku-4-5", llm_provider="anthropic"
    )


def rate_limited(message="slow down"):
    return litellm_exceptions.RateLimitError(
        message=message, llm_provider="anthropic", model="claude-haiku-4-5"
    )


def auth_failed(message="bad key"):
    return litellm_exceptions.AuthenticationError(
        message=message, llm_provider="anthropic", model="claude-haiku-4-5"
    )


class TestTheLadderIsOptional:
    """Nothing about a caller who never asked for escalation may change."""

    def test_no_ladder_calls_once_and_records_once(
        self, cost_log_file, scripted_completion, response_factory
    ):
        expected = response_factory()
        calls = scripted_completion(expected)

        assert complete(model="claude-haiku-4-5", messages=[], workload="w") is expected
        assert len(calls) == 1
        assert len(read_rows(cost_log_file)) == 1

    def test_a_single_rung_ladder_is_just_a_call(
        self, cost_log_file, scripted_completion, response_factory
    ):
        expected = response_factory()
        calls = scripted_completion(expected)

        returned = complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5"],
            escalate_when=lambda _response: True,
        )

        # The predicate says "not good enough" and is ignored, because there is no rung
        # above this one. Calling it could only have produced a fault.
        assert returned is expected
        assert len(calls) == 1

    def test_a_record_with_no_ladder_still_describes_a_chain_of_one(
        self, cost_log_file, scripted_completion, response_factory
    ):
        scripted_completion(response_factory())
        complete(model="claude-haiku-4-5", messages=[], workload="w")
        row = read_rows(cost_log_file)[0]

        assert row["attempt"] == 1
        assert row["ladder_size"] == 1
        assert row["chain_id"]

    def test_our_arguments_never_reach_litellm(
        self, cost_log_file, scripted_completion, response_factory
    ):
        calls = scripted_completion(response_factory())
        complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5"],
            escalate_when=lambda _response: False,
            temperature=0.2,
        )

        assert calls[0]["kwargs"] == {
            "messages": [],
            "temperature": 0.2,
            "model": "claude-haiku-4-5",
        }
        for ours in ("workload", "ladder", "escalate_when"):
            assert ours not in calls[0]["kwargs"]


class TestClimbingOnQuality:
    def test_a_satisfied_predicate_stops_at_the_cheap_model(
        self, cost_log_file, scripted_completion, response_factory
    ):
        cheap = response_factory(response_id="cheap")
        calls = scripted_completion(cheap)

        returned = complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda _response: False,
        )

        assert returned is cheap
        assert len(calls) == 1
        assert len(read_rows(cost_log_file)) == 1

    def test_an_unsatisfied_predicate_climbs_and_returns_the_better_answer(
        self, cost_log_file, scripted_completion, response_factory
    ):
        cheap = response_factory(response_id="cheap")
        dear = response_factory(model="claude-sonnet-5", response_id="dear")
        calls = scripted_completion(cheap, dear)

        returned = complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda response: response.id == "cheap",
        )

        assert returned is dear
        assert calls[0]["kwargs"]["model"] == "claude-haiku-4-5"
        assert calls[1]["kwargs"]["model"] == "claude-sonnet-5"

    def test_both_attempts_are_billed_and_both_are_recorded(
        self, cost_log_file, scripted_completion, response_factory
    ):
        """The whole point. litellm's own fallbacks would report one record for two calls."""
        calls = scripted_completion(
            response_factory(response_id="cheap"),
            response_factory(model="claude-sonnet-5", response_id="dear"),
        )
        complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda response: response.id == "cheap",
        )

        rows = read_rows(cost_log_file)
        assert len(calls) == 2
        assert len(rows) == 2
        assert [row["model"] for row in rows] == ["claude-haiku-4-5", "claude-sonnet-5"]
        assert all(row["status"] == "ok" for row in rows)
        assert all(row["cost_gbp"] > 0 for row in rows)

    def test_the_attempts_of_one_call_share_a_chain_id(
        self, cost_log_file, scripted_completion, response_factory
    ):
        scripted_completion(
            response_factory(response_id="cheap"),
            response_factory(model="claude-sonnet-5", response_id="dear"),
        )
        complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda response: response.id == "cheap",
        )

        rows = read_rows(cost_log_file)
        assert rows[0]["chain_id"] == rows[1]["chain_id"]
        assert [row["attempt"] for row in rows] == [1, 2]
        assert all(row["ladder_size"] == 2 for row in rows)

    def test_separate_calls_do_not_share_a_chain_id(
        self, cost_log_file, scripted_completion, response_factory
    ):
        scripted_completion(response_factory(), response_factory())
        complete(model="claude-haiku-4-5", messages=[], workload="w")
        complete(model="claude-haiku-4-5", messages=[], workload="w")

        rows = read_rows(cost_log_file)
        assert rows[0]["chain_id"] != rows[1]["chain_id"]

    def test_an_exhausted_ladder_still_returns_the_top_rung(
        self, cost_log_file, scripted_completion, response_factory
    ):
        """Never satisfied is not the same as no answer. The caller gets the best available."""
        dear = response_factory(model="claude-sonnet-5", response_id="dear")
        calls = scripted_completion(response_factory(response_id="cheap"), dear)

        returned = complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda _response: True,
        )

        assert returned is dear
        assert len(calls) == 2

    def test_without_a_predicate_a_ladder_never_climbs_on_quality(
        self, cost_log_file, scripted_completion, response_factory
    ):
        """Only the caller can judge an answer. With no predicate there is nothing to judge."""
        cheap = response_factory(response_id="cheap")
        calls = scripted_completion(cheap)

        returned = complete(
            messages=[], workload="w", ladder=["claude-haiku-4-5", "claude-sonnet-5"]
        )

        assert returned is cheap
        assert len(calls) == 1

    def test_a_predicate_that_raises_keeps_the_answer_it_was_given(
        self, cost_log_file, scripted_completion, response_factory, caplog
    ):
        """Escalating on the strength of a bug spends real money. Accepting spends nothing."""
        cheap = response_factory(response_id="cheap")
        calls = scripted_completion(cheap)

        def broken(_response):
            raise ValueError("the predicate itself is buggy")

        with caplog.at_level(logging.WARNING, logger="llm_gateway.routing"):
            returned = complete(
                messages=[],
                workload="w",
                ladder=["claude-haiku-4-5", "claude-sonnet-5"],
                escalate_when=broken,
            )

        assert returned is cheap
        assert len(calls) == 1
        assert "escalate_when raised ValueError" in caplog.text

    def test_the_predicate_is_not_asked_about_the_top_rung(
        self, cost_log_file, scripted_completion, response_factory
    ):
        asked = []
        scripted_completion(
            response_factory(response_id="cheap"),
            response_factory(model="claude-sonnet-5", response_id="dear"),
        )

        def note(response):
            asked.append(response.id)
            return True

        complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=note,
        )

        assert asked == ["cheap"]


class TestFallingBackOnErrors:
    def test_a_rate_limit_falls_back_to_the_next_rung(
        self, cost_log_file, scripted_completion, response_factory
    ):
        dear = response_factory(model="claude-sonnet-5", response_id="dear")
        calls = scripted_completion(rate_limited(), dear)

        returned = complete(
            messages=[], workload="w", ladder=["claude-haiku-4-5", "claude-sonnet-5"]
        )

        assert returned is dear
        assert len(calls) == 2

    def test_an_auth_failure_falls_back_because_a_ladder_may_cross_providers(
        self, cost_log_file, scripted_completion, response_factory
    ):
        """A 401 from Anthropic on rung 1 says nothing about an OpenAI model on rung 2."""
        dear = response_factory(model="gpt-5", response_id="dear")
        calls = scripted_completion(auth_failed(), dear)

        returned = complete(messages=[], workload="w", ladder=["claude-haiku-4-5", "gpt-5"])

        assert returned is dear
        assert len(calls) == 2

    def test_a_bad_request_does_not_climb_because_it_fails_everywhere(
        self, cost_log_file, scripted_completion, response_factory
    ):
        calls = scripted_completion(bad_request())

        with pytest.raises(litellm_exceptions.BadRequestError):
            complete(
                messages=[], workload="w", ladder=["claude-haiku-4-5", "claude-sonnet-5"]
            )

        assert len(calls) == 1

    def test_a_context_window_overflow_does_climb(
        self, cost_log_file, scripted_completion, response_factory
    ):
        """It subclasses BadRequestError but is the best reason there is to use a bigger model."""
        dear = response_factory(model="claude-sonnet-5", response_id="dear")
        calls = scripted_completion(context_window_exceeded(), dear)

        returned = complete(
            messages=[], workload="w", ladder=["claude-haiku-4-5", "claude-sonnet-5"]
        )

        assert returned is dear
        assert len(calls) == 2

    def test_a_failed_attempt_is_still_recorded(
        self, cost_log_file, scripted_completion, response_factory
    ):
        scripted_completion(
            rate_limited(), response_factory(model="claude-sonnet-5", response_id="dear")
        )
        complete(messages=[], workload="w", ladder=["claude-haiku-4-5", "claude-sonnet-5"])

        rows = read_rows(cost_log_file)
        assert len(rows) == 2
        assert rows[0]["status"] == "error"
        assert rows[0]["error_type"] == "RateLimitError"
        assert rows[0]["measured"] is False
        assert rows[1]["status"] == "ok"
        assert rows[0]["chain_id"] == rows[1]["chain_id"]

    def test_when_every_rung_fails_the_last_error_is_raised(
        self, cost_log_file, scripted_completion
    ):
        calls = scripted_completion(rate_limited(), auth_failed("the second one"))

        with pytest.raises(litellm_exceptions.AuthenticationError, match="the second one"):
            complete(
                messages=[], workload="w", ladder=["claude-haiku-4-5", "claude-sonnet-5"]
            )

        assert len(calls) == 2
        assert len(read_rows(cost_log_file)) == 2

    def test_a_single_model_error_propagates_exactly_as_before(
        self, cost_log_file, scripted_completion
    ):
        """No ladder means no earlier success, so the caller's own retry logic still works."""
        scripted_completion(rate_limited())

        with pytest.raises(litellm_exceptions.RateLimitError):
            complete(model="claude-haiku-4-5", messages=[], workload="w")

    def test_a_later_failure_never_discards_an_earlier_answer(
        self, cost_log_file, scripted_completion, response_factory
    ):
        """A ladder means "give me the best you can get", and rung 1's answer was paid for."""
        cheap = response_factory(response_id="cheap")
        calls = scripted_completion(cheap, bad_request("sonnet rejected it"))

        returned = complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda response: response.id == "cheap",
        )

        assert returned is cheap
        assert len(calls) == 2
        rows = read_rows(cost_log_file)
        assert [row["status"] for row in rows] == ["ok", "error"]


class TestTheCeilingIsRecheckedEveryAttempt:
    def test_spend_from_the_first_attempt_refuses_the_second(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        """The load-bearing budget test.

        It also proves the ledger sees attempt 1's write before attempt 2 is checked. If the
        cost log were not flushed, or the budget's cached file offset were not refreshed, an
        escalating call could spend without limit inside a single ``complete()``.
        """
        # One response costs about £0.0046. A ceiling below that is clear of the first
        # attempt and comfortably breached by it.
        monkeypatch.setenv("LLM_GATEWAY_MONTHLY_BUDGET_GBP", "0.001")
        budget.reset_cache()

        cheap = response_factory(response_id="cheap")
        calls = scripted_completion(cheap)

        returned = complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda _response: True,
        )

        # The answer already paid for is handed back rather than thrown away; the ceiling
        # stops further spend, which is what it is for.
        assert returned is cheap
        assert len(calls) == 1

        rows = read_rows(cost_log_file)
        assert [row["status"] for row in rows] == ["ok", "refused"]
        assert rows[1]["reason"] == "budget_exceeded"
        assert rows[1]["attempt"] == 2
        assert rows[0]["chain_id"] == rows[1]["chain_id"]

    def test_a_refusal_on_the_first_attempt_still_raises(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        """There is no answer to hand back, so the refusal must reach the caller."""
        cost_log_file.parent.mkdir(parents=True, exist_ok=True)
        prior = response_factory()
        scripted_completion(prior)
        complete(model="claude-haiku-4-5", messages=[], workload="w")

        monkeypatch.setenv("LLM_GATEWAY_MONTHLY_BUDGET_GBP", "0.001")
        budget.reset_cache()

        calls = scripted_completion(response_factory())
        with pytest.raises(budget.BudgetExceeded):
            complete(
                messages=[],
                workload="w",
                ladder=["claude-haiku-4-5", "claude-sonnet-5"],
                escalate_when=lambda _response: True,
            )

        assert len(calls) == 0

    def test_a_misconfigured_ceiling_refuses_before_any_attempt(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        monkeypatch.setenv("LLM_GATEWAY_MONTHLY_BUDGET_GBP", "not-a-number")
        calls = scripted_completion(response_factory())

        with pytest.raises(budget.BudgetMisconfigured):
            complete(
                messages=[], workload="w", ladder=["claude-haiku-4-5", "claude-sonnet-5"]
            )

        assert len(calls) == 0


class TestFailingOpen:
    """A fault in our routing must never be the reason a caller's call does not happen."""

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param("claude-haiku-4-5", id="a bare string"),
            pytest.param([], id="an empty list"),
            pytest.param([""], id="a blank entry"),
            pytest.param([None], id="a non-string entry"),
            pytest.param(42, id="not a sequence"),
        ],
    )
    def test_a_malformed_ladder_degrades_to_a_direct_call(
        self, cost_log_file, scripted_completion, response_factory, malformed
    ):
        expected = response_factory()
        calls = scripted_completion(expected)

        returned = complete(
            model="claude-haiku-4-5", messages=[], workload="w", ladder=malformed
        )

        assert returned is expected
        assert calls[0]["kwargs"]["model"] == "claude-haiku-4-5"

    def test_a_malformed_ladder_says_so(
        self, cost_log_file, scripted_completion, response_factory, caplog
    ):
        scripted_completion(response_factory())
        with caplog.at_level(logging.WARNING, logger="llm_gateway.routing"):
            complete(model="claude-haiku-4-5", messages=[], workload="w", ladder="oops")

        assert "not a non-empty list of model names" in caplog.text

    def test_a_ladder_wins_over_an_explicit_model_and_says_so(
        self, cost_log_file, scripted_completion, response_factory, caplog
    ):
        calls = scripted_completion(response_factory(model="claude-sonnet-5"))

        with caplog.at_level(logging.WARNING, logger="llm_gateway.routing"):
            complete(
                model="claude-haiku-4-5",
                messages=[],
                workload="w",
                ladder=["claude-sonnet-5"],
            )

        assert calls[0]["kwargs"]["model"] == "claude-sonnet-5"
        assert "the ladder wins" in caplog.text

    def test_a_positional_model_is_still_substituted_per_attempt(
        self, cost_log_file, scripted_completion, response_factory
    ):
        """complete() is a drop-in, so the model may have arrived positionally."""
        calls = scripted_completion(
            response_factory(response_id="cheap"),
            response_factory(model="claude-sonnet-5", response_id="dear"),
        )

        complete(
            "claude-haiku-4-5",
            [],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda response: response.id == "cheap",
        )

        assert calls[0]["args"][0] == "claude-haiku-4-5"
        assert calls[1]["args"][0] == "claude-sonnet-5"

    def test_substituting_the_model_does_not_disturb_the_arguments_given(self):
        """Asserted against the helper, not through complete().

        Through ``complete()`` this is unfalsifiable: ``**kwargs`` is already a fresh dict
        that Python built for the call, so mutating it is invisible to the caller and a test
        of it could never fail. The contract that is real belongs to the helper.
        """
        kwargs = {"model": "claude-haiku-4-5", "messages": [], "temperature": 0.2}
        args, new_kwargs = _call_arguments((), kwargs, "claude-sonnet-5")

        assert new_kwargs["model"] == "claude-sonnet-5"
        assert kwargs["model"] == "claude-haiku-4-5", "the dict passed in was mutated"
        assert new_kwargs is not kwargs

    def test_a_model_is_added_when_the_caller_gave_only_a_ladder(self):
        args, kwargs = _call_arguments((), {"messages": []}, "claude-sonnet-5")
        assert kwargs == {"messages": [], "model": "claude-sonnet-5"}

    def test_nothing_is_substituted_when_there_is_no_model_to_substitute(self):
        """``[None]`` is a real ladder: the caller gave neither. litellm should raise its
        own, better error about that rather than us inventing one."""
        args, kwargs = _call_arguments((), {"messages": []}, None)
        assert kwargs == {"messages": []}

    def test_a_broken_cost_log_does_not_stop_a_chain(
        self, scripted_completion, response_factory, monkeypatch, tmp_path
    ):
        """Recording is measurement. It must never take down the application it measures."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("LLM_GATEWAY_COST_LOG", str(blocker / "spend.jsonl"))

        dear = response_factory(model="claude-sonnet-5", response_id="dear")
        calls = scripted_completion(response_factory(response_id="cheap"), dear)

        returned = complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda response: response.id == "cheap",
        )

        assert returned is dear
        assert len(calls) == 2


class TestStreaming:
    def test_a_stream_is_never_escalated(
        self, cost_log_file, scripted_completion, response_factory
    ):
        """Usage and content only exist once the generator is consumed, so there is
        nothing for a predicate to judge and nothing to price."""
        stream = object()
        calls = scripted_completion(stream)

        returned = complete(
            messages=[],
            workload="w",
            stream=True,
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda _response: True,
        )

        assert returned is stream
        assert len(calls) == 1

    def test_the_record_says_the_ladder_was_not_climbed(
        self, cost_log_file, scripted_completion
    ):
        scripted_completion(object())
        complete(
            messages=[],
            workload="w",
            stream=True,
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
        )

        row = read_rows(cost_log_file)[0]
        assert row["reason"] == "streaming_not_escalated"
        assert row["measured"] is False
        # The ladder the caller gave is still recorded, so the log shows one was supplied
        # and ignored rather than hiding the fact.
        assert row["ladder_size"] == 2

    def test_a_stream_without_a_ladder_keeps_its_original_reason(
        self, cost_log_file, scripted_completion
    ):
        scripted_completion(object())
        complete(model="claude-haiku-4-5", messages=[], workload="w", stream=True)

        assert read_rows(cost_log_file)[0]["reason"] == "streaming_not_instrumented"


class TestNoTextEscapesAChain:
    def test_no_record_in_a_chain_carries_prompt_or_completion_text(
        self, cost_log_file, scripted_completion, response_factory
    ):
        """The guarantee is structural, but a chain writes records on paths a single call
        never takes, so it is worth proving again here."""
        scripted_completion(
            response_factory(response_id="cheap", content="SECRET-COMPLETION-CHEAP"),
            response_factory(
                model="claude-sonnet-5", response_id="dear", content="SECRET-COMPLETION-DEAR"
            ),
        )
        complete(
            messages=[{"role": "user", "content": "SECRET-PROMPT"}],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda response: response.id == "cheap",
        )

        written = cost_log_file.read_text(encoding="utf-8")
        assert "SECRET-PROMPT" not in written
        assert "SECRET-COMPLETION-CHEAP" not in written
        assert "SECRET-COMPLETION-DEAR" not in written


class TestLadderResolution:
    """``resolve_ladder`` on its own.

    ``complete()`` has a second, defensive fallback for an empty ladder, which means a
    broken ``resolve_ladder`` can still produce a working call. That is deliberate
    defence-in-depth, but it also means the function's own contract has to be asserted here
    or it is not really guarded at all.
    """

    def test_no_ladder_gives_the_single_model_path(self):
        assert routing.resolve_ladder("claude-haiku-4-5", None) == ["claude-haiku-4-5"]

    def test_a_good_ladder_is_used_in_order(self):
        assert routing.resolve_ladder(None, ["a", "b", "c"]) == ["a", "b", "c"]

    def test_entries_are_stripped(self):
        assert routing.resolve_ladder(None, ["  a  ", "b"]) == ["a", "b"]

    def test_a_tuple_is_a_perfectly_good_ladder(self):
        assert routing.resolve_ladder(None, ("a", "b")) == ["a", "b"]

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param("claude-haiku-4-5", id="a bare string"),
            pytest.param([], id="an empty list"),
            pytest.param([""], id="a blank entry"),
            pytest.param(["a", "   "], id="a whitespace entry"),
            pytest.param([None], id="a non-string entry"),
            pytest.param([["a"]], id="a nested list"),
            pytest.param(42, id="not a sequence"),
            pytest.param({"a": 1}, id="a mapping"),
        ],
    )
    def test_anything_unusable_degrades_to_the_single_model(self, malformed):
        assert routing.resolve_ladder("claude-haiku-4-5", malformed) == ["claude-haiku-4-5"]

    def test_a_bare_string_is_not_iterated_into_single_characters(self):
        """The mistake worth naming: ladder="gpt-5" must not become a five-rung ladder."""
        assert routing.resolve_ladder(None, "gpt-5") == [None]

    def test_it_never_raises_on_anything(self):
        """The router fails open, so this function has no failure mode that reaches a caller."""

        class Awkward:
            def __iter__(self):
                raise RuntimeError("iterating me is a mistake")

        assert routing.resolve_ladder("claude-haiku-4-5", Awkward()) == ["claude-haiku-4-5"]


class TestErrorClassification:
    """``should_fall_back`` on its own, because the ordering in it is easy to get wrong."""

    def test_a_context_window_overflow_is_checked_before_bad_request(self):
        # It subclasses BadRequestError, so a naive isinstance order returns the wrong answer.
        assert issubclass(
            litellm_exceptions.ContextWindowExceededError, litellm_exceptions.BadRequestError
        )
        assert routing.should_fall_back(context_window_exceeded()) is True

    def test_a_bad_request_does_not_fall_back(self):
        assert routing.should_fall_back(bad_request()) is False

    @pytest.mark.parametrize(
        "error", [rate_limited(), auth_failed(), RuntimeError("something else entirely")]
    )
    def test_everything_else_falls_back(self, error):
        assert routing.should_fall_back(error) is True


class TestBypass:
    """``LLM_GATEWAY_BYPASS`` switches off the router, and only the router.

    The variable exists to answer one question during an incident: is this the routing or
    is this the provider? So it has to make the call reaching litellm the call the caller
    would have made unrouted — and it has to leave the spend ceiling and the cost log
    exactly where they were. See ``test_bypass_does_not_disable_the_ceiling`` in
    ``test_budget.py`` for the other half of that, which predates this work unit.
    """

    def test_a_ladder_is_truncated_to_its_first_rung(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
        expected = response_factory(response_id="cheap")
        # Strict by construction: a second attempt would run off the end of the script.
        calls = scripted_completion(expected)

        returned = complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda _response: True,
        )

        assert returned is expected
        assert len(calls) == 1
        assert calls[0]["kwargs"]["model"] == "claude-haiku-4-5"

    def test_the_predicate_is_never_asked(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        """Rung 1 is the top rung under bypass, so there is nothing to judge."""
        monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
        scripted_completion(response_factory())
        asked = []

        def predicate(response):
            asked.append(response)
            return True

        complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=predicate,
        )

        assert asked == []

    def test_an_error_on_the_first_rung_propagates(
        self, cost_log_file, scripted_completion, monkeypatch
    ):
        """Without bypass a rate limit climbs. With it there is no rung to climb to, so the
        caller's own error handling gets the error, exactly as with no ladder at all."""
        monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
        calls = scripted_completion(rate_limited())

        with pytest.raises(litellm_exceptions.RateLimitError):
            complete(
                messages=[],
                workload="w",
                ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            )

        assert len(calls) == 1
        assert read_rows(cost_log_file)[0]["reason"] == "call_failed"

    def test_the_record_says_the_ladder_was_suppressed(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        """Not inferable, so it is recorded. One attempt against a two-rung ladder is
        otherwise indistinguishable from a first answer the predicate was happy with."""
        monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
        scripted_completion(response_factory())

        complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
        )

        row = read_rows(cost_log_file)[0]
        assert row["reason"] == "bypass_no_escalation"
        # The ladder the caller gave, not the one attempt made: the log shows a ladder was
        # supplied and suppressed rather than hiding it.
        assert row["ladder_size"] == 2
        assert row["attempt"] == 1

    def test_a_call_with_no_ladder_records_nothing_new(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        """Bypass changed nothing here, so saying so would be a reason on every line."""
        monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
        scripted_completion(response_factory())

        complete(model="claude-haiku-4-5", messages=[], workload="w")

        assert read_rows(cost_log_file)[0]["reason"] is None

    def test_the_call_is_still_measured_and_logged(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        """The half of the contract that is easiest to break by accident: bypass must never
        take the cost log with it."""
        monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
        scripted_completion(response_factory())

        complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
        )

        row = read_rows(cost_log_file)[0]
        assert row["measured"] is True
        assert row["cost_gbp"] > 0

    def test_the_ceiling_is_still_enforced(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        """The same decision as ``test_bypass_does_not_disable_the_ceiling``, asserted here
        against a laddered call so that the routing path is covered too."""
        cost_log_file.parent.mkdir(parents=True, exist_ok=True)
        scripted_completion(response_factory())
        complete(model="claude-haiku-4-5", messages=[], workload="w")

        monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
        monkeypatch.setenv("LLM_GATEWAY_MONTHLY_BUDGET_GBP", "0.001")
        budget.reset_cache()

        calls = scripted_completion(response_factory())
        with pytest.raises(budget.BudgetExceeded):
            complete(
                messages=[],
                workload="w",
                ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            )

        assert calls == []

    def test_a_stream_keeps_its_own_reason(
        self, cost_log_file, scripted_completion, monkeypatch
    ):
        """Both explanations are true; the streaming one also says why the call is
        unmeasured, which is what a reader of that record needs first."""
        monkeypatch.setenv("LLM_GATEWAY_BYPASS", "1")
        scripted_completion(object())

        complete(
            messages=[],
            workload="w",
            stream=True,
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
        )

        assert read_rows(cost_log_file)[0]["reason"] == "streaming_not_escalated"

    def test_off_leaves_the_ladder_alone(
        self, cost_log_file, scripted_completion, response_factory, monkeypatch
    ):
        """The value shipped in .env.example. If "any value is on" ever creeps in, this is
        the test that fails."""
        monkeypatch.setenv("LLM_GATEWAY_BYPASS", "0")
        calls = scripted_completion(
            response_factory(response_id="cheap"),
            response_factory(model="claude-sonnet-5", response_id="dear"),
        )

        complete(
            messages=[],
            workload="w",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda response: response.id == "cheap",
        )

        assert len(calls) == 2


class TestBypassValues:
    """``bypass_enabled`` on its own. Which strings mean on is the whole of its contract."""

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "  On  "])
    def test_recognised_on_values(self, value):
        assert routing.bypass_enabled({routing.BYPASS_ENV_VAR: value}) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "FALSE"])
    def test_recognised_off_values(self, value):
        assert routing.bypass_enabled({routing.BYPASS_ENV_VAR: value}) is False

    def test_unset_is_off(self):
        assert routing.bypass_enabled({}) is False

    def test_an_unrecognised_value_is_off_and_says_so(self, caplog):
        """Off is the documented default, but someone who set this believes the router is
        switched off, so it cannot be silent."""
        with caplog.at_level(logging.WARNING, logger="llm_gateway.routing"):
            assert routing.bypass_enabled({routing.BYPASS_ENV_VAR: "maybe"}) is False

        assert "not a recognised on/off value" in caplog.text
        assert "the router is NOT bypassed" in caplog.text

    def test_it_warns_once_rather_than_once_per_call(self, caplog):
        with caplog.at_level(logging.WARNING, logger="llm_gateway.routing"):
            for _ in range(3):
                routing.bypass_enabled({routing.BYPASS_ENV_VAR: "maybe"})

        assert caplog.text.count("not a recognised on/off value") == 1

    def test_it_reads_the_real_environment_by_default(self, monkeypatch):
        monkeypatch.setenv(routing.BYPASS_ENV_VAR, "1")
        assert routing.bypass_enabled() is True

    def test_a_fault_reading_the_environment_is_not_a_bypass(self, caplog):
        """The router fails open, and "open" here means the behaviour every other consumer
        gets — routed — not a silently different one."""

        class Awkward(dict):
            def get(self, *_args, **_kwargs):
                raise RuntimeError("reading me is a mistake")

        with caplog.at_level(logging.WARNING, logger="llm_gateway.routing"):
            assert routing.bypass_enabled(Awkward()) is False

        assert "could not read LLM_GATEWAY_BYPASS" in caplog.text
