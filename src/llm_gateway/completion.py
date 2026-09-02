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
4. **The spend ceiling is checked before the call, not after it.** :mod:`.budget` decides;
   a refusal is recorded like any other outcome and then raised. Point 1 does not apply to
   it: a refusal is a decision, not a fault, and swallowing it would leave the ceiling
   unenforced.

Not in scope here: routing and model escalation. This module measures, and declines.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from . import budget, cost_log, fx, pricing
from .cost_log import CostRecord

__all__ = ["complete"]

_log = logging.getLogger(__name__)

# A label is a routing key for spend analysis, not free text. Long enough for
# "app:component:operation", short enough that a runaway value cannot bloat the log.
_MAX_WORKLOAD_LENGTH = 200

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
    workload = workload.strip()
    if not workload:
        return "unlabelled"
    return workload[:_MAX_WORKLOAD_LENGTH]


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
        reason = "streaming_not_instrumented"
    elif status == "error":
        reason = "call_failed"

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


def complete(*args: Any, workload: str, **kwargs: Any) -> Any:
    """Call ``litellm.completion`` and record what it cost.

    Arguments other than ``workload`` are passed straight through, so this is a drop-in
    replacement for ``litellm.completion``. ``workload`` is required and keyword-only:
    unattributed spend is the problem this library exists to solve, so there is no default.

    Streaming calls are passed through untouched. Usage is not available until the
    generator has been consumed, so a record is still written but marked ``measured:
    false`` — an unmeasured call should be countable, not missing.

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
    timestamp = datetime.now(UTC)

    # Before the call, never after it. A ceiling applied to money already spent is a log
    # entry. This is deliberately outside the timing window: the refusal is not a call.
    try:
        budget.enforce()
    except budget.GatewayError as refusal:
        _record_safely(
            timestamp=timestamp,
            workload=label,
            status="refused",
            requested_model=requested_model,
            response=None,
            latency_ms=None,
            kwargs=kwargs,
            streaming=streaming,
            error_type=type(refusal).__name__,
            reason_override=(
                "budget_exceeded"
                if isinstance(refusal, budget.BudgetExceeded)
                else "budget_misconfigured"
            ),
        )
        raise

    started = time.perf_counter()

    try:
        response = litellm.completion(*args, **kwargs)
    except Exception as exc:
        _record_safely(
            timestamp=timestamp,
            workload=label,
            status="error",
            requested_model=requested_model,
            response=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            kwargs=kwargs,
            streaming=streaming,
            error_type=type(exc).__name__,
        )
        raise

    _record_safely(
        timestamp=timestamp,
        workload=label,
        status="ok",
        requested_model=requested_model,
        response=response,
        latency_ms=(time.perf_counter() - started) * 1000,
        kwargs=kwargs,
        streaming=streaming,
        error_type=None,
    )
    return response
