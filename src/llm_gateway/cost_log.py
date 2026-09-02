"""The cost record, and the JSON-lines file it is written to.

One JSON object per line, appended to the path in ``LLM_GATEWAY_COST_LOG``. If that
variable is unset, nothing is written at all: this is a library running inside someone
else's process, and it should not start creating files in their working directory because
it was imported.

``CostRecord`` has a fixed, explicit field list. That is the structural guarantee that no
prompt or completion text can reach the log — a record is assembled field by field from
token counts and identifiers, never from ``messages`` or ``choices``. Adding a field that
carries model input or output would be a deliberate act, not an accident.

Concurrency: each record is one ``write()`` of one line to a file opened in append mode.
That is safe for many writers in one process. Interleaving between separate processes is
not guaranteed on Windows, and this module does not attempt to solve it; if two consuming
applications must share a log, give them a file each.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

__all__ = ["ENV_VAR", "SCHEMA_VERSION", "CostRecord", "log_path", "write_record"]

ENV_VAR = "LLM_GATEWAY_COST_LOG"

# Bump when the shape of a record changes in a way that a reader must know about, so that
# a log containing several generations of records can still be parsed correctly.
#
# 2 — "refused" joins the status values. The field list is unchanged, but a reader counting
#     calls by status would otherwise silently miss calls the spend ceiling declined.
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CostRecord:
    """One measured call. Field order here is the key order in the written JSON."""

    timestamp: str
    workload: str
    # "ok" — the provider was called and answered.
    # "error" — the provider was called and raised.
    # "refused" — no call was made; the spend ceiling declined it. See ``reason``.
    status: str
    measured: bool
    model: str | None
    model_resolved: str | None
    model_priced_as: str | None
    provider: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    latency_ms: float | None
    cost_usd: float | None
    cost_gbp: float | None
    pricing_source: str
    pricing_checked: str | None
    pricing_caveat: str | None
    fx_rate_usd_per_gbp: float
    fx_source: str
    reason: str | None = None
    error_type: str | None = None
    response_id: str | None = None
    schema: int = field(default=SCHEMA_VERSION)

    def to_json_line(self) -> str:
        """Serialise to a single line, newline included."""
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":")) + "\n"


def log_path(environ: dict[str, str] | None = None) -> Path | None:
    """The configured cost log, or ``None`` when logging is switched off."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_VAR)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()


def write_record(record: CostRecord, path: Path) -> None:
    """Append one record to ``path``, creating parent directories as needed.

    Raises on failure. The caller is responsible for making sure a failure here cannot
    reach the application that made the call.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" stops Windows translating the "\n" into "\r\n"; a JSON-lines file should
    # be byte-identical wherever it was produced.
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(record.to_json_line())
        handle.flush()
