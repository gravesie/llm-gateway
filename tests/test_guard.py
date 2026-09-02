"""Proof that the network guard in conftest.py actually raises.

These tests exist because a guard that has never been watched to fail is an assumption.
If any of them starts passing for the wrong reason, the rest of the suite could be making
real, billable provider calls without anyone noticing.
"""

from __future__ import annotations

import socket

import pytest

from conftest import NetworkCallBlocked


def test_litellm_completion_is_blocked():
    import litellm

    with pytest.raises(NetworkCallBlocked):
        litellm.completion(model="claude-haiku-4-5", messages=[])


def test_litellm_acompletion_is_blocked():
    import litellm

    with pytest.raises(NetworkCallBlocked):
        litellm.acompletion(model="claude-haiku-4-5", messages=[])


def test_httpx_is_blocked():
    import httpx

    with pytest.raises(NetworkCallBlocked):
        httpx.Client().send(httpx.Request("GET", "https://example.invalid"))


def test_raw_sockets_are_blocked():
    with pytest.raises(NetworkCallBlocked):
        socket.socket().connect(("example.invalid", 443))


def test_gateway_complete_cannot_reach_a_provider():
    """The wrapper itself, unstubbed, must hit the guard rather than a provider."""
    from llm_gateway import complete

    with pytest.raises(NetworkCallBlocked):
        complete(model="claude-haiku-4-5", messages=[], workload="test")
