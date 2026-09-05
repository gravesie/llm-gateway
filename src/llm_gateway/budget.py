"""The monthly spend ceilings, checked before a call is made rather than after it.

``LLM_GATEWAY_MONTHLY_BUDGET_GBP`` is a hard ceiling in GBP for the current calendar month,
across every provider and every workload. When measured spend reaches it, :func:`enforce`
refuses the call by raising :class:`BudgetExceeded`, and nothing is sent to a provider. A
cap that is checked after the money has been spent is not a cap.

Per-workload ceilings
---------------------

``LLM_GATEWAY_WORKLOAD_BUDGETS_GBP`` holds a JSON object mapping a workload label to its
own monthly ceiling, so one consumer running away does not stop every other one::

    LLM_GATEWAY_WORKLOAD_BUDGETS_GBP={"web-auditor":5,"moto:bulk":20}

A key governs a label when it is that label or a ``:``-boundary prefix of it, matching the
``app:component:operation`` shape labels are documented in: a ceiling on ``web-auditor``
caps everything that application does, and one on ``web-auditor:crawl`` caps only that.
Every ceiling that governs a label applies, alongside the global one, and the first to
refuse stops the call. There is no per-provider axis: ``workload`` is given by the caller
and always present, while a provider is inferred from the model id and is unknown for
models litellm does not recognise — a spend cap cannot be built on an attribution that is
sometimes missing.

The ledger is the cost log. That is the only record this library has of what it has spent,
which has two consequences worth stating plainly rather than discovering later.

**A ceiling with no cost log is a control that does nothing.** Setting either budget
variable without also setting ``LLM_GATEWAY_COST_LOG`` is therefore refused outright,
rather than quietly allowing every call. A control that is visible in configuration but
unenforced is worse than no control, because it reads as handled.

**The ceiling only sees spend it could price.** ``cost_gbp`` is null for streaming calls,
unknown models, and every modifier ``pricing`` declines to guess at. Those calls are billed
by the provider and are invisible here. They are counted, in ``BudgetStatus.unpriced_calls``
and in the refusal message, but never estimated: a guessed figure inside a spend cap is the
one thing this library cannot afford. A workload that is entirely streaming will never trip
the ceiling.

A brake, not an accounting control
----------------------------------

The check is not atomic with the spend it is checking. :func:`enforce` reads the ledger, the
provider call is made, and the cost record is appended afterwards; nothing holds across
those three steps. ``_lock`` makes the ledger *read* thread-safe within one process — it
does not span the read-call-write sequence, so it does not close this window.

The consequence is that calls already in flight when a ceiling is crossed have each passed a
check none of them has paid for yet, and the ceiling is overshot by roughly the number of
concurrent calls times what each costs. That is one call in a single-threaded batch and more
in a multi-worker server. There is no file locking, deliberately: a library taking locks
inside someone else's process is a new failure mode.

So this is a brake on runaway spend, not a guarantee that a number cannot be exceeded, and
the cost log it reads is this library's own measurement rather than a billing ledger. Where
a consuming application keeps its own record of spend, that record stays authoritative. See
``docs/decisions.md``, 2026-09-05.

Fail open, but only for faults
------------------------------

``CLAUDE.md`` requires that a fault in budgeting never stops a consuming application from
making its call. A ceiling that fails open, though, is not a ceiling. Both hold, because
they are about different things:

- A **fault** in this machinery — an unreadable log, a corrupt line, an I/O error — allows
  the call, records why in ``BudgetStatus.fault``, and warns once.
- A **decision** by this machinery — a total that was computed successfully and reaches the
  ceiling, or a configuration that makes the ceiling unenforceable — refuses the call.

Do not "fix" that asymmetry in either direction. It is the deliberate reading of two rules
that otherwise contradict each other; see ``docs/decisions.md``.

Months are UTC, because that is what cost records are timestamped in.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from . import cost_log

__all__ = [
    "ENV_VAR",
    "BudgetExceeded",
    "BudgetMisconfigured",
    "BudgetStatus",
    "GatewayError",
    "SCOPE_GLOBAL",
    "SCOPE_WORKLOAD",
    "VERDICT_EXCEEDED",
    "VERDICT_MISCONFIGURED_LIMIT",
    "VERDICT_MISCONFIGURED_NO_LOG",
    "VERDICT_NO_LIMIT",
    "VERDICT_WITHIN",
    "WORKLOAD_ENV_VAR",
    "enforce",
    "evaluate",
    "reset_cache",
]

_log = logging.getLogger(__name__)

ENV_VAR = "LLM_GATEWAY_MONTHLY_BUDGET_GBP"
WORKLOAD_ENV_VAR = "LLM_GATEWAY_WORKLOAD_BUDGETS_GBP"

# Which ceiling a status is talking about. A per-workload ceiling produces the same verdicts
# as the global one, so the scope is what distinguishes "this workload is out of money" from
# "everything is out of money" — including in ``BudgetStatus.spent_gbp``, which is the
# scope's spend and not always the month's total.
SCOPE_GLOBAL = "global"
SCOPE_WORKLOAD = "workload"

VERDICT_NO_LIMIT = "no_limit"
VERDICT_WITHIN = "within"
VERDICT_EXCEEDED = "exceeded"
VERDICT_MISCONFIGURED_NO_LOG = "misconfigured_no_log"
VERDICT_MISCONFIGURED_LIMIT = "misconfigured_limit"

# The ledger is read incrementally, a block at a time, so that folding in a month of bulk
# work does not mean holding the whole file in memory.
_READ_BLOCK_BYTES = 1 << 20

# A month key as it appears at the front of an ISO-8601 timestamp.
_MONTH_PREFIX = re.compile(r"^\d{4}-\d{2}$")

# Timestamps this library writes always end in one of these. Anything else is read the slow,
# correct way rather than assumed to already be UTC.
_UTC_SUFFIXES = ("+00:00", "Z", "+0000")


class GatewayError(Exception):
    """Raised when the gateway itself refuses a call.

    Distinct from anything the provider raises, so a consumer can tell "we declined to
    spend this" apart from "the provider failed" without inspecting messages.
    """

    #: The status that produced the refusal. :func:`enforce` attaches it, so a caller can
    #: ask *which* ceiling stopped the call — global or one workload's — rather than
    #: parsing the message. ``None`` on an instance raised by anything but ``enforce``.
    status: BudgetStatus | None = None


class BudgetExceeded(GatewayError):
    """Measured spend for the current month has reached the configured ceiling."""


class BudgetMisconfigured(GatewayError):
    """A ceiling is configured but cannot be enforced as configured."""


@dataclass(frozen=True)
class BudgetStatus:
    """What the ceiling thinks, at one moment, without making a call.

    ``spent_gbp`` and ``unpriced_calls`` belong to the ceiling named by ``scope`` and
    ``scope_key``. When ``scope`` is :data:`SCOPE_WORKLOAD` they are that workload's
    figures, not the month's totals — the alternative, always reporting the month, would
    make ``remaining_gbp`` meaningless for the ceiling actually being reported on.
    """

    verdict: str
    limit_gbp: float | None
    spent_gbp: float
    unpriced_calls: int
    month: str
    fault: str | None = None
    scope: str = SCOPE_GLOBAL
    scope_key: str | None = None

    @property
    def remaining_gbp(self) -> float | None:
        """What is left before the ceiling, or ``None`` when there is no ceiling.

        Never negative: once the ceiling is reached there is nothing left, and how far past
        it a total went is ``spent_gbp`` minus ``limit_gbp``, not this.
        """
        if self.limit_gbp is None:
            return None
        return max(0.0, round(self.limit_gbp - self.spent_gbp, 10))


@dataclass
class _Ledger:
    """Running per-month totals for one cost log, and how far into it we have read."""

    offset: int = 0
    totals: dict[str, float] = field(default_factory=dict)
    unpriced: dict[str, int] = field(default_factory=dict)
    corrupt_lines: int = 0
    # month -> configured ceiling key -> that key's share. Only keys a per-workload ceiling
    # is actually set on are bucketed: a consumer is free to invent a label per call, and
    # totalling every label ever logged would make memory a function of their naming.
    by_workload: dict[str, dict[str, float]] = field(default_factory=dict)
    unpriced_by_workload: dict[str, dict[str, int]] = field(default_factory=dict)
    # The keys the two dicts above were folded for. Attribution happens once, at read time,
    # so a change to the configured set makes them incomplete rather than merely stale.
    tracked: frozenset[str] = frozenset()

    def clear(self) -> None:
        """Forget everything read so far. ``tracked`` is configuration, so it survives."""
        self.offset = 0
        self.totals.clear()
        self.unpriced.clear()
        self.corrupt_lines = 0
        self.by_workload.clear()
        self.unpriced_by_workload.clear()


# Keyed by log path. Module-level because the point of it is to survive between calls;
# guarded by a lock because a consuming application may well call from several threads.
_ledgers: dict[Path, _Ledger] = {}
_lock = Lock()

# Fault text already warned about, so a persistent fault does not warn once per call.
_warned: set[str] = set()


def reset_cache() -> None:
    """Forget every cached ledger, so the next check re-reads from the beginning.

    Needed by the test suite, and by any consumer that rotates its cost log out from under
    a long-running process.
    """
    with _lock:
        _ledgers.clear()
        _warned.clear()


def _current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _read_limit(env: dict[str, str]) -> tuple[float | None, str | None]:
    """Return ``(limit, problem)``.

    ``(None, None)`` means no ceiling was asked for, which is the shipped default and not an
    error. A non-empty value that cannot be used yields a ``problem``, because setting one is
    a deliberate act and silently ignoring a typo would leave spend uncapped.
    """
    raw = env.get(ENV_VAR)
    if raw is None or not raw.strip():
        return None, None

    text = raw.strip()
    try:
        limit = float(text)
    except (TypeError, ValueError):
        return None, f"{ENV_VAR} is set to {text!r}, which is not a number"

    if not math.isfinite(limit):
        return None, f"{ENV_VAR} is set to {text!r}, which is not a finite number"
    if limit < 0:
        return None, f"{ENV_VAR} is set to {text!r}, which is negative"

    # Zero is deliberately allowed. It is a kill switch: spend nothing this month.
    return limit, None


def _read_workload_limits(env: dict[str, str]) -> tuple[dict[str, float], str | None]:
    """Return ``(limits, problem)`` for the per-workload ceilings.

    The variable holds a JSON object mapping a workload label to a number of pounds::

        LLM_GATEWAY_WORKLOAD_BUDGETS_GBP={"web-auditor":5,"moto:bulk":20}

    An empty or unset value means no per-workload ceilings, which is the shipped default.
    Anything else that cannot be used is a ``problem``, on exactly the reasoning behind
    :func:`_read_limit`: setting a ceiling is a deliberate act, and a typo that silently
    left a workload uncapped would be a control that reads as handled and is not.
    """
    raw = env.get(WORKLOAD_ENV_VAR)
    if raw is None or not raw.strip():
        return {}, None

    text = raw.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}, (
            f"{WORKLOAD_ENV_VAR} is not valid JSON. It takes an object mapping workload"
            ' labels to pounds, like {"web-auditor":5}'
        )

    if not isinstance(parsed, dict):
        return {}, (
            f"{WORKLOAD_ENV_VAR} is a JSON {type(parsed).__name__}, not an object mapping"
            " workload labels to pounds"
        )

    limits: dict[str, float] = {}
    for key, value in parsed.items():
        # JSON object keys are always strings, so only their content needs checking.
        label = cost_log.normalise_workload(key)
        if not label:
            return {}, f"{WORKLOAD_ENV_VAR} has a blank workload label as one of its keys"

        if isinstance(value, bool) or not isinstance(value, int | float):
            return {}, (
                f"{WORKLOAD_ENV_VAR} sets {key!r} to {value!r}, which is not a number of"
                " pounds"
            )
        if not math.isfinite(value):
            return {}, f"{WORKLOAD_ENV_VAR} sets {key!r} to {value!r}, which is not finite"
        if value < 0:
            return {}, f"{WORKLOAD_ENV_VAR} sets {key!r} to {value!r}, which is negative"

        if label in limits:
            # Two keys the same once trimmed to a label. Picking a winner would mean one of
            # the two ceilings the operator wrote does nothing, without saying which.
            return {}, (
                f"{WORKLOAD_ENV_VAR} sets a ceiling for {label!r} more than once; labels"
                " are compared after trimming, so two keys collided"
            )

        # Zero is allowed here too, and means this workload spends nothing this month.
        limits[label] = float(value)

    return limits, None


def _matching_keys(workload: object, tracked: frozenset[str]) -> tuple[str, ...]:
    """Every configured ceiling that governs ``workload``, most specific first.

    A key governs a label when it is that label, or a ``:``-boundary prefix of it. Labels
    are documented as ``app:component:operation``, so a ceiling on ``web-auditor`` caps
    everything that application does without the operator having to list each operation,
    while ``web-auditor:crawl`` caps only the one. Matching on raw string prefixes instead
    would make a ceiling on ``web`` quietly cap ``web-auditor``, which nobody asked for.
    """
    if not tracked or not isinstance(workload, str):
        return ()

    label = cost_log.normalise_workload(workload)
    if not label:
        return ()

    matches: list[str] = []
    prefix = ""
    for segment in label.split(":")[:-1]:
        prefix = f"{prefix}:{segment}" if prefix else segment
        if prefix in tracked:
            matches.append(prefix)

    matches.reverse()  # longest prefix first
    if label in tracked:
        matches.insert(0, label)  # the exact key is more specific than any prefix of it

    return tuple(matches)


def _add_spend(ledger: _Ledger, month: str, keys: tuple[str, ...], amount: float) -> None:
    """Attribute one call's cost to every ceiling that governs it."""
    if not keys:
        return
    bucket = ledger.by_workload.setdefault(month, {})
    for key in keys:
        bucket[key] = bucket.get(key, 0.0) + amount


def _count_unpriced(ledger: _Ledger, month: str, keys: tuple[str, ...]) -> None:
    """Attribute one call that could not be priced to every ceiling that governs it."""
    if not keys:
        return
    bucket = ledger.unpriced_by_workload.setdefault(month, {})
    for key in keys:
        bucket[key] = bucket.get(key, 0) + 1


def _month_of(timestamp: str) -> str | None:
    """The UTC ``YYYY-MM`` a record belongs to, or ``None`` if it cannot be determined."""
    if not isinstance(timestamp, str) or len(timestamp) < 7:
        return None

    if timestamp.endswith(_UTC_SUFFIXES):
        month = timestamp[:7]
        return month if _MONTH_PREFIX.match(month) else None

    # Not one of ours, or written with a local offset. Convert properly rather than assume
    # the leading characters are already the UTC month.
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m")


def _fold_line(ledger: _Ledger, raw: bytes) -> None:
    """Add one cost record to the running totals. Never raises."""
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        ledger.corrupt_lines += 1
        return

    if not isinstance(record, dict):
        ledger.corrupt_lines += 1
        return

    # Only calls that actually reached a provider represent spend. A refusal cost nothing,
    # and a failed call is not billed.
    if record.get("status") != "ok":
        return

    month = _month_of(record.get("timestamp"))
    if month is None:
        ledger.corrupt_lines += 1
        return

    # A record whose label is missing or not a string still counts towards the month, but
    # cannot be attributed to a per-workload ceiling. It is spend we know about and cannot
    # place, which is the honest reading — not a reason to discard the line.
    keys = _matching_keys(record.get("workload"), ledger.tracked)

    cost = record.get("cost_gbp")
    if cost is None:
        # Real spend that could not be priced honestly. Counted, never estimated.
        ledger.unpriced[month] = ledger.unpriced.get(month, 0) + 1
        _count_unpriced(ledger, month, keys)
        return

    if not isinstance(cost, int | float) or isinstance(cost, bool) or not math.isfinite(cost):
        ledger.corrupt_lines += 1
        return

    ledger.totals[month] = ledger.totals.get(month, 0.0) + float(cost)
    _add_spend(ledger, month, keys, float(cost))


def _refresh(path: Path) -> _Ledger:
    """Bring the cached ledger for ``path`` up to date with the file on disk.

    Reads only the bytes appended since last time. Raises on I/O the caller cannot ignore;
    a missing file is not one of those.
    """
    return _read_into(_ledgers.setdefault(path, _Ledger()), path)


def _read_into(ledger: _Ledger, path: Path) -> _Ledger:
    """Fold everything ``ledger`` has not read yet into it.

    Split out from :func:`_refresh` so a full rescan can be built in a detached ledger and
    published only once it has succeeded. Discarding the cached totals first would mean a
    read that faults part-way through fell back to zero, which is an I/O blip lifting a
    ceiling.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        # No log yet, or it has been rotated away. Either way there is no recorded spend,
        # and a stale total from a file that no longer exists would be worse than zero.
        ledger.clear()
        return ledger

    if size < ledger.offset:
        # Truncated or replaced by a shorter file. The cached offset is meaningless now.
        ledger.clear()

    if size == ledger.offset:
        return ledger

    remaining = size - ledger.offset
    remainder = b""

    with path.open("rb") as handle:
        handle.seek(ledger.offset)
        while remaining > 0:
            block = handle.read(min(_READ_BLOCK_BYTES, remaining))
            if not block:
                break
            remaining -= len(block)

            # ledger.offset always points at the first byte of `remainder`, so advancing it
            # by the complete lines in this buffer keeps that true.
            head, separator, remainder = (remainder + block).rpartition(b"\n")
            if not separator:
                continue
            for line in head.split(b"\n"):
                if line.strip():
                    _fold_line(ledger, line)
            ledger.offset += len(head) + 1

    # Anything left in `remainder` is a partial line: another process may be mid-append.
    # It stays unconsumed and is picked up on the next check.
    return ledger


def _warn_once(fault: str) -> None:
    """Warn about a fault the first time it is seen, not once per call."""
    if fault in _warned:
        return
    _warned.add(fault)
    _log.warning("llm-gateway could not enforce the spend ceiling reliably: %s", fault)


@dataclass(frozen=True)
class _Ceiling:
    """One configured ceiling, alongside the spend measured against it."""

    scope: str
    key: str | None
    limit_gbp: float
    spent_gbp: float
    unpriced_calls: int

    @property
    def headroom(self) -> float:
        return self.limit_gbp - self.spent_gbp

    @property
    def exceeded(self) -> bool:
        return self.spent_gbp >= self.limit_gbp


def _decide(ceilings: list[_Ceiling]) -> _Ceiling | None:
    """Which of the applicable ceilings a status should report on.

    One that refuses wins, and the list arrives global-first, then workload keys
    most-specific first. When both the global ceiling and a workload's are out of money the
    global one is the more useful thing to be told, because raising the workload's would
    not let the call through. With none refusing, the ceiling with the least headroom is
    reported: it is the one that bites next, and it is what a consumer deciding whether to
    start a batch needs to see.
    """
    for ceiling in ceilings:
        if ceiling.exceeded:
            return ceiling
    if not ceilings:
        return None
    # min() is stable, so a tie goes to the earlier entry — the global ceiling.
    return min(ceilings, key=lambda ceiling: ceiling.headroom)


def evaluate(
    environ: dict[str, str] | None = None, *, workload: str | None = None
) -> BudgetStatus:
    """Work out where spend stands against the ceilings that apply. Never raises.

    This is the whole decision, with none of the consequences. :func:`enforce` turns the
    verdict into an exception; exposed publicly as ``llm_gateway.budget_status`` so a
    consumer can decide not to start a batch rather than discover the ceiling part-way in.

    Without a ``workload`` this answers about the global ceiling only. Per-workload
    ceilings are a question about a particular label, and there is no honest way to answer
    it for a label that was not named.
    """
    env = os.environ if environ is None else environ
    month = _current_month()

    limit, problem = _read_limit(env)
    if problem is not None:
        return BudgetStatus(
            verdict=VERDICT_MISCONFIGURED_LIMIT,
            limit_gbp=None,
            spent_gbp=0.0,
            unpriced_calls=0,
            month=month,
            fault=problem,
        )

    workload_limits, problem = _read_workload_limits(env)
    if problem is not None:
        return BudgetStatus(
            verdict=VERDICT_MISCONFIGURED_LIMIT,
            limit_gbp=None,
            spent_gbp=0.0,
            unpriced_calls=0,
            month=month,
            fault=problem,
            scope=SCOPE_WORKLOAD,
        )

    if limit is None and not workload_limits:
        # No ceiling asked for. The ledger is not touched at all, so the feature costs
        # nothing — not even a stat — when it is switched off.
        return BudgetStatus(
            verdict=VERDICT_NO_LIMIT,
            limit_gbp=None,
            spent_gbp=0.0,
            unpriced_calls=0,
            month=month,
        )

    path = cost_log.log_path(environ=env)
    if path is None:
        # Whichever kind of ceiling was configured, the cost log is the only thing it could
        # be enforced against. Name the one that is set, so the message says what to fix.
        global_ceiling = limit is not None
        return BudgetStatus(
            verdict=VERDICT_MISCONFIGURED_NO_LOG,
            limit_gbp=limit,
            spent_gbp=0.0,
            unpriced_calls=0,
            month=month,
            fault=f"{cost_log.ENV_VAR} is not set, so there is no record of spend",
            scope=SCOPE_GLOBAL if global_ceiling else SCOPE_WORKLOAD,
        )

    tracked = frozenset(workload_limits)
    keys = _matching_keys(workload, tracked)

    spent = 0.0
    unpriced = 0
    workload_spent: dict[str, float] = {}
    workload_unpriced: dict[str, int] = {}
    fault: str | None = None

    try:
        with _lock:
            existing = _ledgers.setdefault(path, _Ledger())
            if existing.tracked == tracked:
                ledger = _refresh(path)
            else:
                # Attribution happens as lines are folded, so totals built for a different
                # set of configured keys are incomplete for this one rather than stale.
                # Rebuild in a detached ledger and publish it only once the read succeeds.
                ledger = _read_into(_Ledger(tracked=tracked), path)
                _ledgers[path] = ledger
            spent = ledger.totals.get(month, 0.0)
            unpriced = ledger.unpriced.get(month, 0)
            # Copied because the lock is released before these are read.
            workload_spent = dict(ledger.by_workload.get(month, {}))
            workload_unpriced = dict(ledger.unpriced_by_workload.get(month, {}))
            corrupt = ledger.corrupt_lines
        if corrupt:
            fault = f"{corrupt} unreadable line(s) in {path}; their spend is not counted"
    except Exception as exc:
        # Fail open. A fault here is ours, and it must not stop the application's call.
        fault = f"could not read {path}: {type(exc).__name__}: {exc}"
        # Fall open only as far as necessary. Whatever was already folded in is still known
        # to have been spent, and a stale total is far closer to the truth than zero — which
        # would mean a transient read error silently lifted the ceiling.
        #
        # If the fault interrupted a rescan, these buckets are the *previous* key set's. A
        # ceiling configured moments ago then reads as unspent for one call, which is the
        # state it was in anyway before it was configured — and the global total, which was
        # not discarded, still holds.
        with _lock:
            cached = _ledgers.get(path)
            if cached is not None:
                spent = cached.totals.get(month, 0.0)
                unpriced = cached.unpriced.get(month, 0)
                workload_spent = dict(cached.by_workload.get(month, {}))
                workload_unpriced = dict(cached.unpriced_by_workload.get(month, {}))

    if fault is not None:
        _warn_once(fault)

    # Order matters: _decide reports the first refusal it finds, and `keys` is already
    # most-specific first.
    ceilings: list[_Ceiling] = []
    if limit is not None:
        ceilings.append(_Ceiling(SCOPE_GLOBAL, None, limit, spent, unpriced))
    for key in keys:
        ceilings.append(
            _Ceiling(
                SCOPE_WORKLOAD,
                key,
                workload_limits[key],
                workload_spent.get(key, 0.0),
                workload_unpriced.get(key, 0),
            )
        )

    decided = _decide(ceilings)
    if decided is None:
        # Per-workload ceilings are configured, none of them governs this label, and there
        # is no global one. Nothing caps this call — but the month's measured spend was
        # read and is worth reporting rather than throwing away.
        return BudgetStatus(
            verdict=VERDICT_NO_LIMIT,
            limit_gbp=None,
            spent_gbp=round(spent, 10),
            unpriced_calls=unpriced,
            month=month,
            fault=fault,
        )

    return BudgetStatus(
        verdict=VERDICT_EXCEEDED if decided.exceeded else VERDICT_WITHIN,
        limit_gbp=decided.limit_gbp,
        spent_gbp=round(decided.spent_gbp, 10),
        unpriced_calls=decided.unpriced_calls,
        month=month,
        fault=fault,
        scope=decided.scope,
        scope_key=decided.key,
    )


def _unpriced_note(status: BudgetStatus) -> str:
    if not status.unpriced_calls:
        return ""
    return (
        f" {status.unpriced_calls} call(s) this month had no cost figure and are not in"
        " that total."
    )


def _refusal(error: GatewayError, status: BudgetStatus) -> GatewayError:
    """Attach the status that produced a refusal, so the caller need not parse the message."""
    error.status = status
    return error


def enforce(
    environ: dict[str, str] | None = None, *, workload: str | None = None
) -> BudgetStatus:
    """Refuse the call if a ceiling says so, otherwise return the status.

    Raises :class:`BudgetExceeded` or :class:`BudgetMisconfigured`, with the deciding
    :class:`BudgetStatus` attached as ``.status``. Must be called *before* the provider
    call, never after: a ceiling enforced after the money is spent is a log entry, not a
    ceiling.

    ``workload`` is the label the call will be recorded under, and brings that workload's
    own ceiling into the decision alongside the global one.
    """
    status = evaluate(environ, workload=workload)

    if status.verdict == VERDICT_MISCONFIGURED_LIMIT:
        remedy = (
            "Set it to a non-negative number of pounds, or leave it empty to run without a"
            " ceiling."
            if status.scope == SCOPE_GLOBAL
            else 'Set it to a JSON object like {"web-auditor":5}, or leave it empty to run'
            " without per-workload ceilings."
        )
        raise _refusal(BudgetMisconfigured(f"{status.fault}. {remedy}"), status)

    if status.verdict == VERDICT_MISCONFIGURED_NO_LOG:
        configured = ENV_VAR if status.scope == SCOPE_GLOBAL else WORKLOAD_ENV_VAR
        raise _refusal(
            BudgetMisconfigured(
                f"{configured} is set, but {cost_log.ENV_VAR} is not, so there is no record"
                f" of spend to enforce it against. Set {cost_log.ENV_VAR}, or unset"
                f" {configured} to run without a ceiling."
            ),
            status,
        )

    if status.verdict == VERDICT_EXCEEDED:
        if status.scope == SCOPE_WORKLOAD:
            message = (
                f"llm-gateway refused the call: £{status.spent_gbp:.4f} of measured spend by"
                f" workload {status.scope_key!r} in {status.month} has reached the"
                f" £{status.limit_gbp:.2f} ceiling set for it by {WORKLOAD_ENV_VAR}."
                f"{_unpriced_note(status)}"
            )
        else:
            message = (
                f"llm-gateway refused the call: £{status.spent_gbp:.4f} of measured spend in"
                f" {status.month} has reached the £{status.limit_gbp:.2f} ceiling set by"
                f" {ENV_VAR}.{_unpriced_note(status)}"
            )
        raise _refusal(BudgetExceeded(message), status)

    return status
