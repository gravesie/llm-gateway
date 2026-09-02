"""The monthly spend ceiling, checked before a call is made rather than after it.

``LLM_GATEWAY_MONTHLY_BUDGET_GBP`` is a hard ceiling in GBP for the current calendar month,
across every provider. When measured spend reaches it, :func:`enforce` refuses the call by
raising :class:`BudgetExceeded`, and nothing is sent to a provider. A cap that is checked
after the money has been spent is not a cap.

The ledger is the cost log. That is the only record this library has of what it has spent,
which has two consequences worth stating plainly rather than discovering later.

**A ceiling with no cost log is a control that does nothing.** Setting the budget without
also setting ``LLM_GATEWAY_COST_LOG`` is therefore refused outright, rather than quietly
allowing every call. A control that is visible in configuration but unenforced is worse
than no control, because it reads as handled.

**The ceiling only sees spend it could price.** ``cost_gbp`` is null for streaming calls,
unknown models, and every modifier ``pricing`` declines to guess at. Those calls are billed
by the provider and are invisible here. They are counted, in ``BudgetStatus.unpriced_calls``
and in the refusal message, but never estimated: a guessed figure inside a spend cap is the
one thing this library cannot afford. A workload that is entirely streaming will never trip
the ceiling.

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
    "VERDICT_EXCEEDED",
    "VERDICT_MISCONFIGURED_LIMIT",
    "VERDICT_MISCONFIGURED_NO_LOG",
    "VERDICT_NO_LIMIT",
    "VERDICT_WITHIN",
    "enforce",
    "evaluate",
    "reset_cache",
]

_log = logging.getLogger(__name__)

ENV_VAR = "LLM_GATEWAY_MONTHLY_BUDGET_GBP"

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


class BudgetExceeded(GatewayError):
    """Measured spend for the current month has reached the configured ceiling."""


class BudgetMisconfigured(GatewayError):
    """A ceiling is configured but cannot be enforced as configured."""


@dataclass(frozen=True)
class BudgetStatus:
    """What the ceiling thinks, at one moment, without making a call."""

    verdict: str
    limit_gbp: float | None
    spent_gbp: float
    unpriced_calls: int
    month: str
    fault: str | None = None

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

    def clear(self) -> None:
        self.offset = 0
        self.totals.clear()
        self.unpriced.clear()
        self.corrupt_lines = 0


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

    cost = record.get("cost_gbp")
    if cost is None:
        # Real spend that could not be priced honestly. Counted, never estimated.
        ledger.unpriced[month] = ledger.unpriced.get(month, 0) + 1
        return

    if not isinstance(cost, int | float) or isinstance(cost, bool) or not math.isfinite(cost):
        ledger.corrupt_lines += 1
        return

    ledger.totals[month] = ledger.totals.get(month, 0.0) + float(cost)


def _refresh(path: Path) -> _Ledger:
    """Bring the cached ledger for ``path`` up to date with the file on disk.

    Reads only the bytes appended since last time. Raises on I/O the caller cannot ignore;
    a missing file is not one of those.
    """
    ledger = _ledgers.setdefault(path, _Ledger())

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


def evaluate(environ: dict[str, str] | None = None) -> BudgetStatus:
    """Work out where spend stands against the ceiling. Never raises.

    This is the whole decision, with none of the consequences. :func:`enforce` turns the
    verdict into an exception; exposed publicly as ``llm_gateway.budget_status`` so a
    consumer can decide not to start a batch rather than discover the ceiling part-way in.
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

    if limit is None:
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
        return BudgetStatus(
            verdict=VERDICT_MISCONFIGURED_NO_LOG,
            limit_gbp=limit,
            spent_gbp=0.0,
            unpriced_calls=0,
            month=month,
            fault=f"{cost_log.ENV_VAR} is not set, so there is no record of spend",
        )

    spent = 0.0
    unpriced = 0
    fault: str | None = None

    try:
        with _lock:
            ledger = _refresh(path)
            spent = ledger.totals.get(month, 0.0)
            unpriced = ledger.unpriced.get(month, 0)
            corrupt = ledger.corrupt_lines
        if corrupt:
            fault = f"{corrupt} unreadable line(s) in {path}; their spend is not counted"
    except Exception as exc:
        # Fail open. A fault here is ours, and it must not stop the application's call.
        fault = f"could not read {path}: {type(exc).__name__}: {exc}"
        # Fall open only as far as necessary. Whatever was already folded in is still known
        # to have been spent, and a stale total is far closer to the truth than zero — which
        # would mean a transient read error silently lifted the ceiling.
        with _lock:
            cached = _ledgers.get(path)
            if cached is not None:
                spent = cached.totals.get(month, 0.0)
                unpriced = cached.unpriced.get(month, 0)

    if fault is not None:
        _warn_once(fault)

    return BudgetStatus(
        verdict=VERDICT_EXCEEDED if spent >= limit else VERDICT_WITHIN,
        limit_gbp=limit,
        spent_gbp=round(spent, 10),
        unpriced_calls=unpriced,
        month=month,
        fault=fault,
    )


def _unpriced_note(status: BudgetStatus) -> str:
    if not status.unpriced_calls:
        return ""
    return (
        f" {status.unpriced_calls} call(s) this month had no cost figure and are not in"
        " that total."
    )


def enforce(environ: dict[str, str] | None = None) -> BudgetStatus:
    """Refuse the call if the ceiling says so, otherwise return the status.

    Raises :class:`BudgetExceeded` or :class:`BudgetMisconfigured`. Must be called *before*
    the provider call, never after: a ceiling enforced after the money is spent is a log
    entry, not a ceiling.
    """
    status = evaluate(environ)

    if status.verdict == VERDICT_MISCONFIGURED_LIMIT:
        raise BudgetMisconfigured(
            f"{status.fault}. Set it to a non-negative number of pounds, or leave it empty"
            " to run without a ceiling."
        )

    if status.verdict == VERDICT_MISCONFIGURED_NO_LOG:
        raise BudgetMisconfigured(
            f"{ENV_VAR} is set to {status.limit_gbp}, but {cost_log.ENV_VAR} is not, so"
            " there is no record of spend to enforce it against. Set"
            f" {cost_log.ENV_VAR}, or unset {ENV_VAR} to run without a ceiling."
        )

    if status.verdict == VERDICT_EXCEEDED:
        raise BudgetExceeded(
            f"llm-gateway refused the call: £{status.spent_gbp:.4f} of measured spend in"
            f" {status.month} has reached the £{status.limit_gbp:.2f} ceiling set by"
            f" {ENV_VAR}.{_unpriced_note(status)}"
        )

    return status
