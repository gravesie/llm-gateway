#!/usr/bin/env python3
"""PreToolUse(Bash) hook: force a confirmation prompt on the irreversible ship actions.

Everything up to opening a pull request runs unattended. Merging, pushing straight to
main, and touching production are the points of no return, so they must stop and ask --
and must do so even when the session is in an auto-accept permission mode. Returning
`permissionDecision: "ask"` is what makes that a guarantee rather than a convention the
model is trusted to remember.

Fails open by design: any parsing or matching error exits 0 with no output, so a broken
hook slows nothing down. The trade-off is deliberate -- the git and CI gates
(pre-push lint/tests, CI on the PR) are the ones that must fail closed; this one is a
confirmation prompt.
"""

from __future__ import annotations

import json
import re
import sys

# (compiled pattern, reason shown in the confirmation prompt)
GUARDED = [
    (
        re.compile(r"\bgh\s+pr\s+merge\b"),
        "Merging to main deploys to production. This needs an explicit go from Pete in "
        "this session -- a green test suite on its own is not authorisation.",
    ),
    (
        re.compile(r"\bgit\s+push\b(?![^|;&]*--dry-run)[^|;&]*\bmain\b"),
        "This pushes directly to main, bypassing the pull request and CI review. "
        "Confirm this is deliberate.",
    ),
    (
        re.compile(r"\bgit\s+push\b[^|;&]*(?:--force\b|-f\b)"),
        "Force-push rewrites history that may already be shared. Confirm this is "
        "deliberate.",
    ),
    (
        re.compile(r"deploy\.sh\b"),
        "Running the deploy script by hand bypasses CI. Merging to main is the deploy; "
        "the manual path is break-glass only, for when CI itself is broken.",
    ),
    (
        re.compile(r"\bssh\b[^|;&]*(?:\bdeploy\b|\bprod\b|\broot@)"),
        "This looks like an SSH connection to production. Deploys go through CI, not "
        "over SSH. Confirm what this is for.",
    ),
]


def find_reason(command: str) -> str | None:
    """Return the reason to prompt on, or None if the command is unguarded."""
    for pattern, reason in GUARDED:
        if pattern.search(command):
            return reason
    return None


def main() -> None:
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Bash":
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not command:
        return

    reason = find_reason(command)
    if reason is None:
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail open: never block a tool call because this hook misbehaved.
        pass
    sys.exit(0)
