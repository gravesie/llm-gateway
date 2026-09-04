"""Shared fixtures, and the guard that keeps the suite off the network.

A test that reaches a real provider costs money every time CI runs, and the failure is
silent — it looks like a passing test. So the block is global and autouse rather than
per-test: nothing here has to remember to mock the specific call it happens to hit.

Three layers, outermost first, because any one of them alone can be bypassed:

1. ``litellm.completion`` and its async twin are replaced with functions that raise.
2. ``httpx`` request sending is blocked, which catches anything reaching a provider by
   another route.
3. ``socket.socket.connect`` is blocked, which catches everything else.

``test_guard.py`` proves each layer actually raises. A guard nobody has watched fail is an
assumption, not a guard.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest


class NetworkCallBlocked(AssertionError):
    """Raised when a test tries to reach the outside world."""


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Refuse every route out of the process for the duration of a test."""

    def refuse(*_args, **_kwargs):
        raise NetworkCallBlocked(
            "A test tried to make a real network or provider call. Stub "
            "litellm.completion instead; the suite must never spend money."
        )

    import httpx
    import litellm

    monkeypatch.setattr(litellm, "completion", refuse)
    monkeypatch.setattr(litellm, "acompletion", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Start every test from a known environment.

    A developer's real ``.env`` must not be able to change what the suite asserts, and a
    test must not be able to append to a real cost log. A stray
    ``LLM_GATEWAY_MONTHLY_BUDGET_GBP`` would be worse still: it would make the suite refuse
    calls it expects to succeed, or pass them when it expects a refusal.
    """
    monkeypatch.delenv("LLM_GATEWAY_COST_LOG", raising=False)
    monkeypatch.delenv("LLM_GATEWAY_USD_GBP_RATE", raising=False)
    monkeypatch.delenv("LLM_GATEWAY_MONTHLY_BUDGET_GBP", raising=False)
    monkeypatch.delenv("LLM_GATEWAY_BYPASS", raising=False)


@pytest.fixture(autouse=True)
def clean_budget_ledger():
    """Drop the module-level ledger cache between tests.

    ``budget`` caches per-month totals and a file offset across calls on purpose; without
    this, one test's spend would still be counted in the next.
    """
    from llm_gateway import budget

    budget.reset_cache()
    yield
    budget.reset_cache()


@pytest.fixture(autouse=True)
def clean_routing_warnings():
    """Drop the module-level set of faults ``routing`` has already warned about.

    Same reason as ``clean_budget_ledger``: the suppression is deliberate in production, so
    a caller repeating a mistake in a loop warns once rather than once per call. In a test
    process it would mean the second test to trigger a fault saw no warning at all.
    """
    from llm_gateway import routing

    routing.reset_warnings()
    yield
    routing.reset_warnings()


@pytest.fixture
def cost_log_file(tmp_path, monkeypatch):
    """Point the cost log at a temporary file and return its path."""
    path = tmp_path / "costs" / "spend.jsonl"
    monkeypatch.setenv("LLM_GATEWAY_COST_LOG", str(path))
    return path


def build_response(
    *,
    model="claude-haiku-4-5",
    prompt_tokens=11200,
    completion_tokens=340,
    cached_tokens=8000,
    cache_creation_tokens=2000,
    ephemeral_1h_input_tokens=0,
    service_tier=None,
    response_id="chatcmpl-test-1",
    content="the completion text",
):
    """A stand-in for a litellm response, shaped the way litellm actually returns one.

    ``prompt_tokens`` defaults to the *inclusive* total (1200 uncached + 8000 cache read +
    2000 cache write), which is what litellm produces for Anthropic. Tests depend on that
    default being inclusive; see ``extract_usage``.
    """
    return SimpleNamespace(
        id=response_id,
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            service_tier=service_tier,
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=cached_tokens,
                cache_creation_tokens=cache_creation_tokens,
                cache_creation_token_details=SimpleNamespace(
                    ephemeral_1h_input_tokens=ephemeral_1h_input_tokens,
                    ephemeral_5m_input_tokens=cache_creation_tokens
                    - ephemeral_1h_input_tokens,
                ),
            ),
        ),
    )


@pytest.fixture
def response_factory():
    return build_response


@pytest.fixture
def stub_completion(monkeypatch):
    """Replace ``litellm.completion`` with a stub, and record how it was called."""
    import litellm

    def install(result=None, *, raises=None):
        calls = []

        def fake_completion(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            if raises is not None:
                raise raises
            return result

        monkeypatch.setattr(litellm, "completion", fake_completion)
        return calls

    return install
