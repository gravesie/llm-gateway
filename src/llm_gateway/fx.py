"""USD to GBP conversion for cost records.

An exchange rate goes stale faster than a token price, so this module does two things to
keep that visible rather than hidden. The rate is overridable from the environment, and
whichever rate was used is written into every record alongside the figure it produced. A
GBP number in the log can therefore always be recomputed, and a run made with a stale rate
can be identified after the fact instead of quietly polluting a total.

There is no network lookup here on purpose. This library runs inside someone else's
process; a cost logger that makes its own HTTP calls is a new failure mode and a new
latency source for no gain.
"""

from __future__ import annotations

import os

__all__ = [
    "ENV_VAR",
    "DEFAULT_USD_PER_GBP",
    "DEFAULT_SOURCE_URL",
    "DEFAULT_RATE_DATE",
    "resolve_rate",
    "usd_to_gbp",
]

ENV_VAR = "LLM_GATEWAY_USD_GBP_RATE"

# US dollars per pound sterling. Federal Reserve H.10 release, rate for 2026-08-28,
# re-read 2026-09-07 and still the latest published figure — the release carrying it is
# dated 2026-08-31, so the rate date below is correct and is not the same thing as the
# date this was checked. Override with LLM_GATEWAY_USD_GBP_RATE rather than editing this.
DEFAULT_USD_PER_GBP = 1.3555
DEFAULT_SOURCE_URL = "https://www.federalreserve.gov/releases/h10/current/"
DEFAULT_RATE_DATE = "2026-08-28"


def resolve_rate(environ: dict[str, str] | None = None) -> tuple[float, str]:
    """Return ``(usd_per_gbp, source)``.

    ``source`` is ``"env"``, ``"default"``, or ``"default_env_invalid"`` when an override
    was set but could not be used. The last of those is deliberately distinct: a typo in a
    consumer's environment should show up in the data, not silently fall back and look
    identical to never having set it.
    """
    env = os.environ if environ is None else environ
    raw = env.get(ENV_VAR)

    if raw is None or not raw.strip():
        return DEFAULT_USD_PER_GBP, "default"

    try:
        rate = float(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_USD_PER_GBP, "default_env_invalid"

    # A non-positive or non-finite rate would produce a nonsense or infinite GBP figure.
    if not rate > 0 or rate != rate or rate in (float("inf"), float("-inf")):
        return DEFAULT_USD_PER_GBP, "default_env_invalid"

    return rate, "env"


def usd_to_gbp(amount_usd: float | None, usd_per_gbp: float) -> float | None:
    """Convert a USD amount to GBP, or return ``None`` if there was no USD figure."""
    if amount_usd is None:
        return None
    if not usd_per_gbp > 0:
        return None
    return round(amount_usd / usd_per_gbp, 10)
