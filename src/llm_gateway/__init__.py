"""In-process LLM routing, fallback and cost instrumentation.

At present this package does one thing: it measures. :func:`complete` wraps
``litellm.completion``, records what each call cost to the JSON-lines file named by
``LLM_GATEWAY_COST_LOG``, and gets out of the way.

    from llm_gateway import complete

    response = complete(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "..."}],
        workload="web-auditor:page-summary",
    )

Routing, model escalation and budget enforcement are not implemented. See CLAUDE.md for
the rules this package holds to, and docs/decisions.md for why it is shaped this way.
"""

from .completion import complete
from .cost_log import SCHEMA_VERSION, CostRecord

__all__ = ["complete", "CostRecord", "SCHEMA_VERSION", "__version__"]

__version__ = "0.1.0"
