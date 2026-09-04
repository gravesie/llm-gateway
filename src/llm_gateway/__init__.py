"""In-process LLM routing, fallback and cost instrumentation.

This package measures what calls cost, and refuses them once a monthly ceiling is reached.
:func:`complete` wraps ``litellm.completion``, checks the ceiling before calling the
provider, records what the call cost to the JSON-lines file named by
``LLM_GATEWAY_COST_LOG``, and otherwise gets out of the way.

    from llm_gateway import complete

    response = complete(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "..."}],
        workload="web-auditor:page-summary",
    )

Set ``LLM_GATEWAY_MONTHLY_BUDGET_GBP`` and a call is refused with :class:`BudgetExceeded`
once measured spend for the calendar month reaches it. The ceiling is enforced against the
cost log, so it requires ``LLM_GATEWAY_COST_LOG`` to be set as well; configured without
one, calls are refused rather than silently uncapped. :func:`budget_status` reports where
spend stands without making a call.

Give ``ladder`` an ordered list of models and ``escalate_when`` a predicate, and the cheap
model is tried first and the expensive one only when the cheap answer is not good enough::

    response = complete(
        messages=messages,
        workload="web-auditor:page-summary",
        ladder=["claude-haiku-4-5", "claude-sonnet-5"],
        escalate_when=lambda r: len(r.choices[0].message.content) < 200,
    )

Every attempt is billed by the provider and gets its own cost record; the records of one
call share a ``chain_id``. The spend ceiling is re-checked before each attempt.

Set ``LLM_GATEWAY_BYPASS=1`` to switch the routing off: a ladder is truncated to its first
rung, so the call reaching the provider is the one you would have made without this library
routing it. It is a diagnostic, and it stops there — the ceiling is still enforced and every
call is still recorded.

See CLAUDE.md for the rules this package holds to, and docs/decisions.md for why it is
shaped this way.
"""

from .budget import (
    BudgetExceeded,
    BudgetMisconfigured,
    BudgetStatus,
    GatewayError,
)
from .budget import evaluate as budget_status
from .completion import complete
from .cost_log import SCHEMA_VERSION, CostRecord

__all__ = [
    "SCHEMA_VERSION",
    "BudgetExceeded",
    "BudgetMisconfigured",
    "BudgetStatus",
    "CostRecord",
    "GatewayError",
    "__version__",
    "budget_status",
    "complete",
]

__version__ = "0.4.0"
