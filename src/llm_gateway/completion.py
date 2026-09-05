"""The instrumented wrapper around ``litellm.completion``.

``complete()`` makes the same call litellm would, times it, works out what it cost, and
appends one line to the cost log. The contract, in order of importance:

1. **It fails open.** Every part of measurement — pricing, conversion, serialising,
   writing — sits inside a guard. If any of it breaks, the provider's response is still
   returned to the caller. A cost logger must never be able to take down the application
   it is measuring.
2. **A provider error is not our error.** Exceptions raised by litellm propagate
   unchanged, so the caller's own retry and error handling still works. They are recorded
   first, with the exception type only.
3. **No prompt or completion text is recorded.** Nothing here reads ``messages`` or
   ``choices``; the record is built from token counts and identifiers.
4. **The spend ceilings are checked before the call, not after it.** :mod:`.budget`
   decides, against the global monthly ceiling and against any ceiling set for this
   ``workload``; a refusal is recorded like any other outcome and then raised. Point 1 does
   not apply to it: a refusal is a decision, not a fault, and swallowing it would leave the
   ceiling unenforced.
5. **Every attempt is recorded separately.** A ladder makes several billable calls, and one
   record for the chain would understate what was spent. Attempts of one call share a
   ``chain_id``; the ceiling is re-checked before each one.
6. **``LLM_GATEWAY_BYPASS`` switches off routing and nothing else.** It stops the ladder
   after rung 1, so the call reaching the provider is the one the caller would have made
   without this library in the way. It does not touch points 4 or 5.

The escalation loop is deliberately ours rather than ``litellm``'s ``fallbacks=``. See
:mod:`.routing` for why, and for the judgement calls the loop delegates to it.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from . import budget, cost_log, fx, pricing, routing
from .cost_log import CostRecord

__all__ = ["complete"]

_log = logging.getLogger(__name__)

# Service tiers that bill at the standard published rate. Anything else changes the price
# in a way this version does not model.
_STANDARD_SERVICE_TIERS = frozenset({"standard", "auto", "default", "standard_only"})


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an object or a mapping, whichever it turns out to be.

    litellm returns pydantic models, but a stubbed or older response may be a plain dict,
    and neither shape should break measurement.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def sanitise_workload(workload: Any) -> str:
    """Coerce a caller's label into something safe to store.

    Never raises. Rejecting a bad label would mean the wrapper could fail on an input that
    has nothing to do with whether the provider call can be made, which would break the
    fail-open contract for the sake of tidiness.
    """
    if not isinstance(workload, str):
        workload = "" if workload is None else str(workload)
    # The same normalisation a per-workload ceiling's configured key gets, so a label and
    # the key meant to cap it cannot drift apart. See :func:`cost_log.normalise_workload`.
    label = cost_log.normalise_workload(workload)
    return label or "unlabelled"


def extract_usage(response: Any) -> dict[str, int]:
    """Pull billable token counts out of a litellm response.

    The subtraction matters. litellm normalises every provider onto OpenAI's convention,
    in which ``prompt_tokens`` is *inclusive* of cache reads and cache writes — see
    ``litellm/llms/anthropic/chat/transformation.py``, which adds both into
    ``prompt_tokens`` even though Anthropic's own API reports them separately. Pricing
    ``prompt_tokens`` at the full input rate would therefore bill every cached token twice,
    once at the input rate and once at the cache rate.
    """
    usage = _get(response, "usage")
    details = _get(usage, "prompt_tokens_details")

    total_prompt = _as_int(_get(usage, "prompt_tokens", 0))
    output_tokens = _as_int(_get(usage, "completion_tokens", 0))

    # Prefer the normalised details block; fall back to the provider-shaped fields litellm
    # also sets on the usage object itself.
    cache_read = _as_int(_get(details, "cached_tokens", 0)) or _as_int(
        _get(usage, "cache_read_input_tokens", 0)
    )
    cache_write = _as_int(_get(details, "cache_creation_tokens", 0)) or _as_int(
        _get(usage, "cache_creation_input_tokens", 0)
    )

    uncached_input = total_prompt - cache_read - cache_write
    if uncached_input < 0:
        # A provider reporting prompt_tokens *exclusive* of cache tokens. In that case
        # prompt_tokens already is the uncached count. Clamping to zero here would quietly
        # understate input instead.
        uncached_input = total_prompt

    return {
        "input_tokens": uncached_input,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def unpriced_modifier(kwargs: dict[str, Any], response: Any) -> str | None:
    """Name the first pricing modifier in play that this version does not model.

    Each of these genuinely changes the bill. Returning a base-rate figure when one is
    active would be a confidently wrong number, so the caller nulls the cost and records
    the name instead.
    """
    if kwargs.get("speed") == "fast":
        return "fast_mode"

    inference_geo = kwargs.get("inference_geo")
    if inference_geo not in (None, "global"):
        return "inference_geo_non_global"

    usage = _get(response, "usage")
    service_tier = kwargs.get("service_tier") or _get(usage, "service_tier")
    if service_tier and str(service_tier).lower() not in _STANDARD_SERVICE_TIERS:
        return f"service_tier_{service_tier}"

    details = _get(usage, "prompt_tokens_details")
    creation_details = _get(details, "cache_creation_token_details")
    if _as_int(_get(creation_details, "ephemeral_1h_input_tokens", 0)) > 0:
        return "cache_write_1h"

    return None


def _build_record(
    *,
    timestamp: datetime,
    workload: str,
    status: str,
    requested_model: str | None,
    response: Any,
    latency_ms: float | None,
    kwargs: dict[str, Any],
    streaming: bool,
    error_type: str | None,
    reason_override: str | None = None,
    chain_id: str | None = None,
    attempt: int = 1,
    ladder_size: int = 1,
    bypassed: bool = False,
) -> CostRecord:
    """Assemble a record. Every field is named explicitly; none is derived from prompt text."""
    resolved_model = _get(response, "model")
    resolved_model = str(resolved_model) if resolved_model else None

    rate, fx_source = fx.resolve_rate()

    # Price against what the caller asked for; if that is unknown, try what actually ran.
    lookup = pricing.look_up_price(requested_model)
    if lookup.price is None and resolved_model and resolved_model != requested_model:
        lookup = pricing.look_up_price(resolved_model)

    measured = status == "ok" and not streaming
    reason: str | None = None
    if status == "refused":
        # Checked first: a refused call never ran, so why it was refused outranks anything
        # about how it would have been made.
        reason = reason_override
    elif streaming:
        # A ladder cannot be climbed for a stream: usage and content are not available until
        # the generator has been consumed, and the predicate needs both. Say which of the two
        # happened rather than implying escalation was even considered.
        reason = "streaming_not_escalated" if ladder_size > 1 else "streaming_not_instrumented"
    elif status == "error":
        reason = "call_failed"
    elif bypassed and ladder_size > 1:
        # Recorded because it is not inferable. One attempt against a two-rung ladder looks
        # exactly like a first answer the predicate was happy with; only this says the
        # router was switched off. On a call with no ladder it changed nothing, so saying
        # so would be noise on every line.
        reason = "bypass_no_escalation"

    # A refused call has no duration; zero would read as one that returned instantly.
    latency = None if latency_ms is None else round(latency_ms, 3)

    if not measured:
        # No usable usage figures: a stream has not been consumed yet, and a failed call
        # returned nothing. Record the attempt so unmeasured calls stay countable, but do
        # not invent token counts or a cost for it.
        return CostRecord(
            timestamp=timestamp.isoformat(),
            workload=workload,
            status=status,
            measured=False,
            model=requested_model,
            model_resolved=resolved_model,
            model_priced_as=lookup.priced_as,
            provider=lookup.provider,
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            latency_ms=latency,
            cost_usd=None,
            cost_gbp=None,
            pricing_source=lookup.source,
            pricing_checked=lookup.price.checked if lookup.price else None,
            pricing_caveat=None,
            fx_rate_usd_per_gbp=rate,
            fx_source=fx_source,
            reason=reason,
            error_type=error_type,
            response_id=None,
            chain_id=chain_id,
            attempt=attempt,
            ladder_size=ladder_size,
        )

    tokens = extract_usage(response)
    caveat = unpriced_modifier(kwargs, response)

    if caveat is None:
        total_prompt = (
            tokens["input_tokens"] + tokens["cache_read_tokens"] + tokens["cache_write_tokens"]
        )
        if pricing.long_context_tier_exceeded(lookup.price, total_prompt):
            caveat = "long_context_tier"

    usd = None if caveat else pricing.cost_usd(lookup.price, **tokens)

    response_id = _get(response, "id")

    return CostRecord(
        timestamp=timestamp.isoformat(),
        workload=workload,
        status=status,
        measured=True,
        model=requested_model,
        model_resolved=resolved_model,
        model_priced_as=lookup.priced_as,
        provider=lookup.provider,
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        cache_read_tokens=tokens["cache_read_tokens"],
        cache_write_tokens=tokens["cache_write_tokens"],
        latency_ms=latency,
        cost_usd=usd,
        cost_gbp=fx.usd_to_gbp(usd, rate),
        pricing_source=lookup.source,
        pricing_checked=lookup.price.checked if lookup.price else None,
        pricing_caveat=caveat,
        fx_rate_usd_per_gbp=rate,
        fx_source=fx_source,
        reason=reason,
        error_type=error_type,
        response_id=str(response_id) if response_id else None,
        chain_id=chain_id,
        attempt=attempt,
        ladder_size=ladder_size,
    )


def _record_safely(**record_kwargs: Any) -> None:
    """Build and write a record, swallowing anything that goes wrong.

    This is the fail-open boundary. It is the only place in the library that catches a
    bare ``Exception``, and it does not stay quiet about it: the warning goes to a standard
    library logger with no handler attached, so it is invisible until a consuming
    application decides to look, and costs nothing when it does not.
    """
    try:
        path = cost_log.log_path()
        if path is None:
            return
        cost_log.write_record(_build_record(**record_kwargs), path)
    except Exception:
        _log.warning("llm-gateway could not record the cost of a call", exc_info=True)


def _refusal_reason(refusal: budget.GatewayError) -> str:
    """The ``reason`` recorded for a refused call.

    Which ceiling refused is not inferable from the record — the workload is on the line
    either way, and only the configuration says whether that workload had its own ceiling.
    A reader counting refusals needs to tell "this workload is out of money" from
    "everything is", so the two are separate values rather than one.
    """
    if not isinstance(refusal, budget.BudgetExceeded):
        return "budget_misconfigured"
    status = refusal.status
    if status is not None and status.scope == budget.SCOPE_WORKLOAD:
        return "budget_exceeded_workload"
    return "budget_exceeded"


def _call_arguments(
    args: tuple[Any, ...], kwargs: dict[str, Any], model: str | None
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Substitute ``model`` wherever the caller happened to put it.

    ``complete()`` is a drop-in for ``litellm.completion``, so the model may have arrived
    positionally or by keyword. Climbing a ladder means replacing it per attempt, and the
    caller's own arguments are never mutated — each attempt gets its own copy.
    """
    if model is None:
        return args, kwargs
    if "model" in kwargs:
        return args, {**kwargs, "model": model}
    if args:
        return (model, *args[1:]), kwargs
    return args, {**kwargs, "model": model}


def complete(
    *args: Any,
    workload: str,
    ladder: list[str] | None = None,
    escalate_when: Callable[[Any], Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Call ``litellm.completion`` and record what it cost.

    Arguments other than ``workload``, ``ladder`` and ``escalate_when`` are passed straight
    through, so this is a drop-in replacement for ``litellm.completion``. ``workload`` is
    required and keyword-only: unattributed spend is the problem this library exists to
    solve, so there is no default.

    **Escalation.** Give ``ladder`` an ordered list of models, cheapest first, and
    ``escalate_when`` a predicate that returns true when a response is not good enough::

        complete(
            messages=messages,
            workload="web-auditor:page-summary",
            ladder=["claude-haiku-4-5", "claude-sonnet-5"],
            escalate_when=lambda r: len(r.choices[0].message.content) < 200,
        )

    Only the caller can judge whether an answer is good enough, so without a predicate a
    ladder still buys fallback on provider errors but never climbs on quality. Each attempt
    is billed by the provider and gets its own cost record; the records of one call share a
    ``chain_id``, and summing ``cost_gbp`` across that chain is what the call really cost.

    ``workload`` is also what a per-workload ceiling is matched against. Set
    ``LLM_GATEWAY_WORKLOAD_BUDGETS_GBP`` and this call is refused once *either* the global
    monthly ceiling or one set for this label is reached, so one consumer exhausting its
    own budget does not stop the others. See :mod:`.budget`.

    The spend ceilings are re-checked before *every* attempt, not just the first. If one
    refuses an attempt after an earlier one has already answered, that answer is returned
    rather than discarded — the ceiling exists to stop further spend, not to throw away
    something already paid for. A refusal on the first attempt still raises, because there
    is nothing to return.

    Likewise, a later attempt failing never discards an earlier success: a ladder means
    "give me the best you can get". With no ladder there is never an earlier success, so
    provider errors propagate exactly as they always have.

    Setting ``LLM_GATEWAY_BYPASS=1`` in the environment stops the ladder after its first
    rung, for the whole process: one attempt, no escalation and no fallback, which is the
    call this library would have made had it never been given a ladder. It is a diagnostic
    for telling a fault in the routing apart from a fault at the provider, and it changes
    nothing else — the ceiling is still enforced and every call is still recorded, with
    ``reason: "bypass_no_escalation"`` on a record whose ladder was suppressed.

    Streaming calls are passed through untouched and never escalated — usage and content
    are not available until the generator is consumed. A record is still written, marked
    ``measured: false``, because an unmeasured call should be countable rather than missing.

    Raises :class:`~llm_gateway.budget.BudgetExceeded` or
    :class:`~llm_gateway.budget.BudgetMisconfigured` *instead of* calling the provider when
    the monthly ceiling says so. Both derive from
    :class:`~llm_gateway.budget.GatewayError`, so a caller can tell a refusal by this
    library apart from a failure at the provider.
    """
    import litellm

    label = sanitise_workload(workload)

    requested_model = kwargs.get("model")
    if requested_model is None and args:
        requested_model = args[0]
    requested_model = str(requested_model) if requested_model is not None else None

    streaming = bool(kwargs.get("stream"))

    # Resolving the ladder is our code deciding what to do with the caller's input, and a
    # bug in it must not be what stops their call. Degrade to the single-model path.
    try:
        rungs = routing.resolve_ladder(requested_model, ladder)
    except Exception:
        _log.warning("llm-gateway could not resolve the model ladder", exc_info=True)
        rungs = [requested_model]
    if not rungs:
        rungs = [requested_model]

    # LLM_GATEWAY_BYPASS makes this call the plain provider call it would have been without
    # this library routing it: rung 1, no escalation, no error fallback. It stops there.
    # The ceiling below is still enforced and the record is still written.
    bypassed = routing.bypass_enabled()

    # Recorded as the ladder the caller *gave*, even when only the first rung is attempted,
    # so the log shows a ladder was supplied and not climbed rather than hiding it.
    ladder_size = len(rungs)
    attempts = rungs[:1] if streaming or bypassed else rungs

    chain_id = uuid.uuid4().hex
    best: Any = None
    last_error: BaseException | None = None

    for attempt, model in enumerate(attempts, start=1):
        call_args, call_kwargs = _call_arguments(args, kwargs, model)
        record = {
            "workload": label,
            "requested_model": model,
            "kwargs": call_kwargs,
            "streaming": streaming,
            "chain_id": chain_id,
            "attempt": attempt,
            "ladder_size": ladder_size,
            "bypassed": bypassed,
        }
        timestamp = datetime.now(UTC)

        # Before the call, never after it. A ceiling applied to money already spent is a log
        # entry. This is deliberately outside the timing window: the refusal is not a call.
        try:
            budget.enforce(workload=label)
        except budget.GatewayError as refusal:
            _record_safely(
                timestamp=timestamp,
                status="refused",
                response=None,
                latency_ms=None,
                error_type=type(refusal).__name__,
                reason_override=_refusal_reason(refusal),
                **record,
            )
            if best is not None:
                # An earlier rung already answered and was already paid for. Refusing to
                # spend more is the point; destroying what that money bought is not.
                return best
            raise

        started = time.perf_counter()

        try:
            response = litellm.completion(*call_args, **call_kwargs)
        except Exception as exc:
            _record_safely(
                timestamp=timestamp,
                status="error",
                response=None,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_type=type(exc).__name__,
                **record,
            )
            last_error = exc
            if attempt < len(attempts) and routing.should_fall_back(exc):
                continue
            if best is not None:
                return best
            raise

        _record_safely(
            timestamp=timestamp,
            status="ok",
            response=response,
            latency_ms=(time.perf_counter() - started) * 1000,
            error_type=None,
            **record,
        )

        # Asking the predicate on the top rung could only produce a fault: there is nowhere
        # left to climb, so the answer is the answer either way.
        if attempt == len(attempts):
            return response
        if not routing.wants_escalation(escalate_when, response):
            return response
        best = response

    # Only reachable if `attempts` were empty, which `resolve_ladder` does not allow. Kept
    # so a future change that breaks that invariant fails loudly instead of returning None.
    if best is not None:
        return best
    if last_error is not None:
        raise last_error
    raise RuntimeError("llm-gateway made no attempt at all; this is a bug in the router")
