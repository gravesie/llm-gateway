#!/usr/bin/env python3
"""SessionStart hook: inject the standing opening checks, and the queue, for every
coding session.

The opening checks are prose in CLAUDE.md as well, but CLAUDE.md is advisory context
that a model drifts from over a long session. Emitting them as SessionStart
`additionalContext` means the harness puts them in front of the model every time, from
any client.

D2: THE QUEUE. `.claude/queue.json`, if present, lists this repo's open/blocked work
units -- see docs/queue-plan-V0-1.md for the schema and why it exists (prose hand-offs
going mutually stale across repos, in both directions, on 2026-09-08). A unit with an
open PR is live-verified against GitHub with `gh pr view`, because a cached status for a
unit is exactly the staleness bug this file exists to fix. This does not enforce
anything and does not write to the repo -- it reports, the session fixes the file.

A19: THE DISTRIBUTION REGISTER. If `scripts/check_distribution.py` exists -- it does in
dev-process and in no adopting repo, because it is not distributed -- it is run with
`--verify --json` and its answer summarised here. Same reason as the queue: what every
adopting repo carries is recorded in a committed file, and a recorded cross-repo claim is
the staleness bug one layer up unless something re-reads the other repo. This section is
the only thing that does. It prints ONE line when every adopter is current, so the steady
state costs almost nothing.

The logic stays in that script rather than here on purpose. This file is copied verbatim
into every adopting repo by `apply-dev-process`, and the register is a dev-process-only
concern -- inlining it would ship dead code to every project. That is a deliberate,
narrow exception to "a hook is a self-contained single file", which exists because hooks
travel; this shells out to something that does not.

A21: WHERE THIS FILE THINKS IT IS. Repo root used to come from the SessionStart payload's
`cwd` field (or `os.getcwd()`). Both are unreliable: `CLAUDE_PROJECT_DIR` is absent from
the desktop client's tool environment (measured 2026-09-13 -- 20+ other `CLAUDE_*` vars
are set and this one is not), so `${CLAUDE_PROJECT_DIR:-.}` in settings.json falls
through to `.`, and the harness has been observed resetting a session's tracked working
directory mid-session, unprompted, after Bash calls -- which is what the payload's `cwd`
reflects. `__file__` does not drift the way a harness-reported field can, so
`repo_root_of` is now seeded from this file's own location on disk (`hook_directory()`)
instead of the payload. That still can't be a fixed parent count: this file is authored
at `hooks/` here but distributed VERBATIM to `.claude/hooks/` in every adopting repo
(`.claude/rules/distributed-files.md`) -- one level shallower here than everywhere else
it runs. `repo_root_of` already delegates to `git rev-parse --show-toplevel`, which walks
up on its own, so seeding it from `hook_directory()` is correct at both depths without
counting anything. Backported from web-auditor's fork (closed by this change; see
`.claude/distribution.json`'s former exception for hooks/session_start.py) along with the
defensive current-branch read below -- same "don't trust the harness's self-reported
state" motive, applied to a stale VALUE (which branch) rather than a stale PATH.

FAILURE CONTRACT, split in two. The opening checks are unconditional: they cannot fail,
because they do not depend on repo state beyond the current branch (itself read
defensively -- see `_current_branch`). The queue and distribution sections can fail
(missing repo, malformed JSON, no `gh`, a network hiccup) and when they do they fail open
-- the session starts either way -- but *visibly*: a warning lands in
`additionalContext` rather than being swallowed, matching `stop_gate.py`'s "a gate that
has silently stopped running is worse than no gate." Not being able to reach GitHub for
one unit is not an error, only a narrower claim -- that unit is shown as not re-verified
this session rather than folded silently into "still open." This is a deliberate
narrowing of this hook's older claim to be unable to fail meaningfully -- true when this
file only printed a fixed string, and not true the moment it reads external state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

GH_TIMEOUT_SECONDS = 10

CONFIG_RELATIVE_PATH = os.path.join(".claude", "queue.json")

DISTRIBUTION_CHECKER_RELATIVE_PATH = os.path.join("scripts", "check_distribution.py")

# Generous: the checker makes one GitHub API call per adopter. Measured at 2.9s for six
# adopters on 2026-09-13, and a slow answer is better than a false "not re-verified".
DISTRIBUTION_TIMEOUT_SECONDS = 45


def _opening_checks(repo_root: str | None) -> str:
    return f"""\
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
{_branch_line(repo_root)}
- Once the plan is agreed, commit it into the repo as the branch's first commit, before \
any code. A session can end without warning — credit exhaustion has ended three — and \
wrap-session never runs, so anything held only in the conversation is lost. Plan mode's \
file under ~/.claude/plans/ does not count: it is outside the repo, so a fresh checkout \
and a claude.ai session cannot see it.
- The escalation list in CLAUDE.md governs what you do unattended and what stops and \
asks. Follow it literally rather than judging importance case by case.
- Merging to main is the deploy. Never SSH to production to deploy. Merging requires an \
explicit go from Pete in this session.
"""


class GateError(Exception):
    """A condition the queue reader cannot evaluate. Fails open, with a visible warning."""


# --------------------------------------------------------------------------- git


def hook_directory() -> str:
    """Where THIS file lives on disk, absolute. Isolated to one call site -- same reason
    as `gh_pr_view` -- so a self-test can substitute a fake location without needing a
    real copy of this file sitting there."""
    return os.path.dirname(os.path.abspath(__file__))


def repo_root_of(directory: str) -> str | None:
    """The work tree root containing `directory`, or None if it is not a repository."""
    if not directory or not os.path.isdir(directory):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else None


# ------------------------------------------------------------------------- branch


def _current_branch(repo_root: str | None) -> str | None:
    """Read the real git branch at hook time, so a stale session-start snapshot can't be
    mistaken for it. 2026-07-28 (web-auditor): a session trusted "on main" without
    checking and branched off another session's in-flight work instead. This is what
    would have caught that -- the snapshot was wrong, `git` itself is not."""
    if not repo_root:
        return None
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    branch = (result.stdout or "").strip()
    return branch or None


def _branch_line(repo_root: str | None) -> str:
    branch = _current_branch(repo_root)
    if branch is None:
        return (
            "- Could not read the current git branch automatically — run "
            "`git branch --show-current` yourself before branching."
        )
    if branch == "main":
        return "- HEAD is on `main`. Branch off it before editing anything."
    return (
        f"- HEAD is on `{branch}`, not `main` — this tree may be mid-work from a prior "
        "or concurrent session. Do not add commits onto it for a new work unit; branch "
        "off `main` instead (in a separate `git worktree` if another session might "
        "still be using this checkout)."
    )


# ------------------------------------------------------------------------- queue


def load_queue(repo_root: str) -> dict:
    """Read `.claude/queue.json`. Absent file means no queue yet -- not an error."""
    path = os.path.join(repo_root, CONFIG_RELATIVE_PATH)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise GateError(f"{CONFIG_RELATIVE_PATH} could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise GateError(f"{CONFIG_RELATIVE_PATH} must contain a JSON object")
    return data


def gh_pr_view(pr: int | str, repo: str) -> subprocess.CompletedProcess | None:
    """Run `gh pr view --json state,mergedAt`, or None if it could not be run at all.

    Isolated to one call site -- not because the logic is complex, but so a self-test
    can replace exactly this function and exercise `verify_unit`'s parsing for real,
    without needing a `gh` look-alike on PATH. (A batch-file stub would not even be
    found: Windows' CreateProcess only auto-appends `.exe` to an extension-less name,
    not `.bat`/`.cmd`, so `subprocess.run(["gh", ...])` would silently miss it there.)
    """
    try:
        return subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", str(repo), "--json", "state,mergedAt"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def verify_unit(unit: dict) -> dict:
    """Live-check a unit's PR against GitHub.

    Returns {"applicable": bool, "checked": bool, "merged": bool, "merged_at": str|None}.
    `applicable` is False for a unit with no PR yet -- nothing to verify. `checked` is
    False for every failure mode (no `gh`, no auth, a timeout, a bad response) -- all of
    them a pass, never a block, and reported as "not re-verified" rather than silently
    folded into "still open."
    """
    pr = unit.get("pr")
    if not pr:
        return {"applicable": False, "checked": False, "merged": False, "merged_at": None}

    repo = unit.get("repo")
    if not repo:
        return {"applicable": True, "checked": False, "merged": False, "merged_at": None}

    proc = gh_pr_view(pr, repo)
    if proc is None or proc.returncode != 0:
        return {"applicable": True, "checked": False, "merged": False, "merged_at": None}

    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {"applicable": True, "checked": False, "merged": False, "merged_at": None}

    merged_at = data.get("mergedAt") if isinstance(data, dict) else None
    return {"applicable": True, "checked": True, "merged": bool(merged_at), "merged_at": merged_at}


def render_queue_section(repo_root: str) -> str:
    """The 'Queue' block appended to `additionalContext`, or '' when there is nothing
    to show. Never raises -- a read failure becomes a visible warning line instead."""
    try:
        queue = load_queue(repo_root)
    except GateError as exc:
        return (
            f"\n\nQUEUE WARNING: {exc}. Session starting anyway; the queue is not "
            "available this turn -- fix .claude/queue.json before trusting it."
        )

    units = queue.get("units")
    if not isinstance(units, list) or not units:
        return ""

    lines = [
        "",
        "",
        f"Queue ({CONFIG_RELATIVE_PATH} — open/blocked units only, not the full plan):",
    ]
    corrections: list[str] = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        uid = unit.get("id", "?")
        status = unit.get("status", "?")
        title = unit.get("title", "")
        gated_on = unit.get("gated_on") or []
        gated_str = f" [gated on {', '.join(gated_on)}]" if gated_on else ""

        result = verify_unit(unit)
        suffix = ""
        if result["applicable"] and not result["checked"]:
            suffix = " (not re-verified this session — gh unavailable)"

        lines.append(f"- {uid} ({status}){gated_str}: {title}{suffix}")

        if result["checked"] and result["merged"]:
            corrections.append(
                f"{uid} (PR #{unit.get('pr')}, {unit.get('repo')}) shows "
                f"mergedAt={result['merged_at']} on GitHub but {CONFIG_RELATIVE_PATH} "
                f"still lists it as {status}. Remove it before relying on this list."
            )

    if corrections:
        lines.append("")
        lines.append(
            "STALE — verified against GitHub just now, queue.json disagrees. Fix "
            "before treating this list as current:"
        )
        lines.extend(f"- {line}" for line in corrections)

    return "\n".join(lines)


# ------------------------------------------------------------------ distribution


def run_distribution_check(repo_root: str) -> subprocess.CompletedProcess | None:
    """Run the distribution checker with `--verify --json`, or None if it could not run.

    Isolated to one call site for the same reason as `gh_pr_view`: a self-test can replace
    exactly this function and exercise the parsing below for real, without a look-alike
    script on disk or a network call.
    """
    checker = os.path.join(repo_root, DISTRIBUTION_CHECKER_RELATIVE_PATH)
    try:
        return subprocess.run(
            [sys.executable, checker, "--verify", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DISTRIBUTION_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def render_distribution_section(repo_root: str) -> str:
    """The 'Distribution' block, or '' when this repo does not carry the checker.

    Never raises. One line when every adopter is current, because this runs at the start
    of every session in this repo and the opening context cost is the thing Track A exists
    to hold down.
    """
    if not os.path.isfile(os.path.join(repo_root, DISTRIBUTION_CHECKER_RELATIVE_PATH)):
        return ""

    proc = run_distribution_check(repo_root)
    if proc is None:
        return (
            "\n\nDISTRIBUTION WARNING: scripts/check_distribution.py could not be run. "
            "What each adopting repo carries is not re-verified this session."
        )

    try:
        report = json.loads(proc.stdout)
    except ValueError:
        return (
            "\n\nDISTRIBUTION WARNING: scripts/check_distribution.py returned output this "
            "hook could not parse. Run it by hand before trusting the register."
        )
    if not isinstance(report, dict):
        return "\n\nDISTRIBUTION WARNING: the distribution report was not a JSON object."
    if report.get("error"):
        return (
            f"\n\nDISTRIBUTION WARNING: {report['error']} -- .claude/distribution.json is "
            "not usable this session."
        )

    adopters = report.get("adopters") or []
    unreachable = report.get("unreachable") or []
    failures = report.get("failures") or []

    drifting = [
        adopter for adopter in adopters
        if adopter.get("behind") or adopter.get("forked") or adopter.get("unrecorded")
    ]

    verified = "verified against GitHub just now"
    if unreachable:
        verified = f"{len(report.get('verified') or [])} of {len(adopters)} read from GitHub"

    if not drifting and not failures:
        return (
            f"\n\nDistribution: all {len(adopters)} adopting repos carry canonical "
            f"({verified})."
        )

    lines = [
        "",
        "",
        f"Distribution ({len(drifting)} of {len(adopters)} adopting repos behind "
        f"canonical — {verified}):",
    ]
    for adopter in drifting:
        parts = []
        for label in ("behind", "forked", "unrecorded"):
            if adopter.get(label):
                parts.append(f"{len(adopter[label])} {label}")
        units = ", ".join(adopter.get("outstanding") or []) or "nothing queued"
        lines.append(f"- {adopter.get('repo', '?')}: {', '.join(parts)} [{units}]")

    if failures:
        lines.append("")
        lines.append(
            "REGISTER INCONSISTENT — scripts/check_distribution.py is red, so CI here is "
            "red too. Fix it before re-seeding anything:"
        )
        lines.extend(f"- {failure.strip()}" for failure in failures)

    return "\n".join(lines)


def evaluate() -> dict:
    """Build the SessionStart output. Always returns a document -- never fails closed."""
    repo_root = repo_root_of(hook_directory())
    context = _opening_checks(repo_root)

    if repo_root:
        context += render_queue_section(repo_root)
        context += render_distribution_section(repo_root)

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def main() -> None:
    print(json.dumps(evaluate()))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let a hook failure stop a session from starting.
        sys.exit(0)
