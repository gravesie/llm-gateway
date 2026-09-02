#!/usr/bin/env python3
"""SessionStart hook: inject the standing opening checks for every coding session.

These checks are prose in CLAUDE.md as well, but CLAUDE.md is advisory context that a
model drifts from over a long session. Emitting them as SessionStart `additionalContext`
means the harness puts them in front of the model every time, from any client.

Takes no input and cannot fail meaningfully: it prints a fixed JSON document. Any
unexpected error still exits 0 with no output, so a broken hook can never stop a session
from starting.
"""

import json
import sys

OPENING_CHECKS = """\
Standing session-opening checks for this project. Do these BEFORE your first \
substantive action, in your first response, above everything else. Keep the whole thing \
to three short lines — it is a preamble, not a report.

1. MODEL CHECK. State the model you are running on and whether it fits this task. \
Routing: Opus or Fable for architecture, security-sensitive code, hard debugging and \
planning; Sonnet for ordinary feature, CRUD and spec-driven build work; Haiku for ops, \
deployment help, status and planning chat. Say "stay" or name the tier to switch to, in \
one line. You cannot change the model yourself — Pete does, with /model.

2. REMOTE CONTROL. If this session is not already remote-controlled, tell Pete to type \
/remote-control so he can pick the session up from his phone. It is an interactive \
slash command with a confirm dialog — you cannot enable it for him, and there is no \
setting behind it. One line. Skip this line entirely if the session is already \
remote-controlled.

3. RESUME STATE. If this project has a resume-state memory file, read it before \
planning anything, so you start from the last session's hand-off rather than \
re-deriving it.

Then, for the work itself:

- One session = one work unit = one PR. Do not roll a second job into this session; \
when the work unit is done, wrap up.
- Enter plan mode before writing code if the change is multi-file, or touches database \
schema, auth, payments or deploy. Single-file, clearly-scoped changes do not need it.
- Branch off main before editing anything.
- The escalation list in CLAUDE.md governs what you do unattended and what stops and \
asks. Follow it literally rather than judging importance case by case.
- Merging to main is the deploy. Never SSH to production to deploy. Merging requires an \
explicit go from Pete in this session.
"""


def main() -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": OPENING_CHECKS,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let a hook failure stop a session from starting.
        sys.exit(0)
