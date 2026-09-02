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

Routing and model escalation are not implemented. See CLAUDE.md for the rules this package
holds to, and docs/decisions.md for why it is shaped this way.
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

__version__ = "0.2.0"
