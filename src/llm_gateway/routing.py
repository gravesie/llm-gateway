"""Deciding which model to try, and whether to try another one.

This module holds the three judgement calls the escalation loop in :mod:`.completion` has
to make: what the ladder actually is, whether a provider error is worth trying elsewhere,
and whether the caller wants to climb a rung. The loop itself lives in ``completion.py``;
what is here is the part worth testing on its own.

Why the loop is ours
--------------------

``litellm.completion(fallbacks=[...])`` works — it routes to ``completion_with_fallbacks``
without needing a ``Router``. It is still the wrong tool here. litellm's fallback loop is
invisible to this wrapper, so several billable attempts come back as one response and
produce **one** cost record. Per-attempt cost records are the entire value of this library;
a chain that understates spend is worse than no chain at all. So the loop is reimplemented
here deliberately, and ``fallbacks`` is never passed through.

Nothing in this module raises
-----------------------------

``CLAUDE.md`` requires the router to fail open: a fault in routing must never stop a
consuming application from making its call. Every function here therefore degrades to the
safe answer rather than propagating, and says so through the fault channel described below.

Faults are warned about, not swallowed silently. The warning goes to a standard library
logger with no handler attached, so it is invisible until a consuming application decides
to look and costs nothing when it does not. That is the same narrow exception the rest of
the package makes: a fault channel, not instrumentation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

__all__ = ["resolve_ladder", "should_fall_back", "wants_escalation"]

_log = logging.getLogger(__name__)

# Fault text already warned about, so a caller making the same mistake on every call in a
# loop warns once rather than once per call. Mirrors the approach in ``budget``.
_warned: set[str] = set()


def reset_warnings() -> None:
    """Forget which faults have been warned about.

    Needed by the test suite, which would otherwise see a warning suppressed because an
    earlier test in the same process had already triggered it.
    """
    _warned.clear()


def _warn_once(fault: str) -> None:
    if fault in _warned:
        return
    _warned.add(fault)
    _log.warning("llm-gateway routing: %s", fault)


def _valid_rungs(ladder: Any) -> list[str] | None:
    """The ladder as a clean list of model names, or ``None`` if it is not usable.

    A string is rejected on purpose. ``ladder="claude-haiku-4-5"`` is a plausible mistake,
    and iterating it would produce a ladder of single characters — a very confusing way to
    fail. Better to treat it as malformed and fall back to the single-model path.
    """
    if isinstance(ladder, str) or not isinstance(ladder, (list, tuple)):
        return None

    rungs = []
    for entry in ladder:
        if not isinstance(entry, str) or not entry.strip():
            return None
        rungs.append(entry.strip())

    return rungs or None


def resolve_ladder(model: str | None, ladder: Any) -> list[str]:
    """Work out the ordered list of models to try.

    Returns ``[model]`` when no usable ladder was given, which is the single-attempt path
    this library had before escalation existed. Never raises: rejecting a caller's ladder
    must not be the thing that stops their call, so a malformed one degrades to the direct
    call rather than becoming an error.

    ``[None]`` is a legitimate return. It means the caller passed neither a model nor a
    ladder, and litellm should be left to raise its own, better error about that.
    """
    if ladder is None:
        return [model]

    rungs = _valid_rungs(ladder)
    if rungs is None:
        _warn_once(
            f"ladder={ladder!r} is not a non-empty list of model names; falling back to a"
            f" single call to {model!r}"
        )
        return [model]

    if model is not None and model not in rungs:
        # Both were given and they disagree. The ladder is the more specific instruction, so
        # it wins — but silently ignoring an explicit model= would be the kind of surprise
        # that costs someone an afternoon.
        _warn_once(
            f"both model={model!r} and ladder={rungs!r} were given; the ladder wins and"
            " model= is ignored"
        )

    return rungs


def should_fall_back(exc: BaseException) -> bool:
    """Whether a provider error is worth retrying on the next rung.

    The ordering matters and is not arbitrary. ``ContextWindowExceededError`` subclasses
    ``BadRequestError`` in litellm (verified against 1.99.0), so it has to be checked first
    or it would be classified as a request that no model will accept — when it is in fact
    the single best reason there is to escalate to a larger model.

    Everything that is not a bad request falls back, including authentication and
    permission errors. That looks wrong until you remember a ladder may cross providers: a
    401 from Anthropic on rung 1 says nothing at all about an OpenAI model on rung 2.
    """
    try:
        from litellm import exceptions
    except Exception:
        # We cannot classify, and we have just successfully called into litellm, so this is
        # a corner that should not happen. Trying the next rung is bounded by the length of
        # the ladder, so the cost of being wrong here is small and finite.
        _warn_once("could not import litellm.exceptions to classify an error; falling back")
        return True

    context_window = getattr(exceptions, "ContextWindowExceededError", None)
    if context_window is not None and isinstance(exc, context_window):
        return True

    bad_request = getattr(exceptions, "BadRequestError", None)
    if bad_request is not None and isinstance(exc, bad_request):
        # Malformed, unsupported or policy-blocked. It will fail the same way on every rung,
        # so climbing the ladder only spends latency to arrive at the same error.
        return False

    return True


def wants_escalation(predicate: Callable[[Any], Any] | None, response: Any) -> bool:
    """Ask the caller's predicate whether this answer is good enough.

    The library cannot judge answer quality — that is the consuming application's domain
    knowledge, not ours — so with no predicate there is no reason to climb. A ladder on its
    own still buys error fallback.

    The predicate is caller code running inside our loop, so it is guarded. If it raises,
    the answer is **accepted**: escalating on the strength of a bug would spend real money,
    whereas accepting spends nothing more and hands back a response that genuinely arrived.
    """
    if predicate is None:
        return False

    try:
        return bool(predicate(response))
    except Exception as exc:
        _warn_once(
            f"escalate_when raised {type(exc).__name__}: {exc}; keeping the response it was"
            " asked about rather than escalating"
        )
        return False
