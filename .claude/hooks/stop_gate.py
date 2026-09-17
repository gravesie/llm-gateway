#!/usr/bin/env python3
"""Stop hook: check the turn-level invariants at the end of every turn, and block once.

The pre-push hook fires after the turn has ended and after the claim in it has already
been read. A Stop hook fires at the end of *every* turn and can block it, which is what
turns "a session Pete watches" into "a session Pete can leave".

WHAT THIS DOES NOT DO. A deterministic command hook cannot judge whether code is
correct -- running a full suite every turn is the only thing that would, and that blows
the time budget. What it enforces is the pair of invariants that make an unwatched
session *recoverable*: the work is on a branch, and the plan is in the repo before the
code is. If the session then dies, `main` is clean and the intent is in git. The
correctness half belongs to a `prompt`-flavour gate and to the PR-adversary agent, and
this file must not be recorded as covering it. See docs/turn-level-gate-plan-V0-1.md.

BLOCKS EXACTLY ONCE PER STOP-CHAIN. Claude Code sets `stop_hook_active` on the input
after a block; this hook returns silently while that is true, so the turn ends whether
or not the session fixed anything. That is deliberate. Blocking repeatedly would fight
the harness and lose anyway: measured in the shipped binary (v2.1.236), the turn loop
reads `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP ?? 8` and overrides the hook on the *ninth*
consecutive block. One nudge, recorded in the transcript, is the whole mechanism.

Fails open, but visibly: an unexpected error returns a `systemMessage` and exits 0 rather
than blocking. A gate that has silently stopped running is worse than no gate, because it
reads as handled.

A23: WHERE THIS FILE THINKS IT IS. `evaluate()` used to resolve its repo root from
`payload.get("cwd") or os.getcwd()`, the same source session_start.py's A21 fix replaced:
`CLAUDE_PROJECT_DIR` is absent from the desktop client's tool environment, and the harness
has been observed resetting a session's tracked working directory mid-session, unprompted,
after Bash calls -- which is what both `cwd` sources reflect. Unlike guard_ship.py, this
hook only ever reasons about ONE repo -- the one the session's turn just worked in, never
a second target parsed out of a command string -- so there is no second root that has to
stay dynamic here; the fix is a direct, unqualified port of A21's `hook_directory()`.

Calibrated by hooks/stop_gate_selftest.py -- a known-positive and a known-negative for
each check, plus a control proving the harness can report red. Run from CI.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import time

# A single git call should take milliseconds. The timeout is for a hung or prompting
# git (credential helper, filesystem lock), not for slow work.
GIT_TIMEOUT_SECONDS = 10

# Total budget for all checks. The pre-push hook has blown a tool timeout once, so this
# is stated rather than assumed. Expected real cost is well under 100 ms.
WALL_CLOCK_BUDGET_SECONDS = 5.0

# fnmatch's `*` spans `/`, so this one pattern also covers docs/workpackages/WP-x-plan.md
# and anything else nested under docs/. Matching is case-insensitive.
DEFAULT_PLAN_GLOBS = ("docs/*plan*.md",)

# A recorded skip, not a silent one -- the posture SKIP_PREPUSH takes in the pre-push
# hook. The reason stays in git history for as long as the commit does.
PLAN_EXEMPT_TRAILER = re.compile(r"^Plan-exempt:[ \t]*(\S.*)$", re.MULTILINE)

CONFIG_RELATIVE_PATH = os.path.join(".claude", "turn-gate.json")


class GateError(Exception):
    """A condition the hook cannot evaluate. Fails open, with a visible warning."""


# --------------------------------------------------------------------------- git


def git(repo_root: str, *args: str) -> tuple[int, str]:
    """Run a git command in `repo_root`. Returns (exit code, stripped stdout).

    Never raises on a non-zero exit -- several callers use failure as information
    (no upstream configured, detached HEAD). A missing git binary or a hang does raise,
    because those mean the hook cannot do its job and should say so.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:  # git not on PATH
        raise GateError("git is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"git {' '.join(args)} timed out") from exc
    return proc.returncode, (proc.stdout or "").strip()


def hook_directory() -> str:
    """Where THIS file lives on disk, absolute. Isolated to one call site so a self-test
    can substitute a fake location without needing a real copy of this file sitting there.
    A23, ported from session_start.py's A21."""
    return os.path.dirname(os.path.abspath(__file__))


def repo_root_of(directory: str) -> str | None:
    """The work tree root containing `directory`, or None if it is not a repository."""
    if not directory or not os.path.isdir(directory):
        return None
    code, out = git(directory, "rev-parse", "--show-toplevel")
    return out if code == 0 and out else None


def current_branch(repo_root: str) -> str | None:
    """The checked-out branch, or None when HEAD is detached."""
    code, out = git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out if code == 0 and out else None


def ref_exists(repo_root: str, ref: str) -> bool:
    code, _ = git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return code == 0


# ------------------------------------------------------------------ configuration


def load_config(repo_root: str) -> dict:
    """Read .claude/turn-gate.json if present.

    Absent means on with defaults, so the gate works the moment the hook is copied into
    a project. A malformed file is a real problem and is surfaced rather than ignored:
    silently falling back to defaults would hide a disabled check.
    """
    path = os.path.join(repo_root, CONFIG_RELATIVE_PATH)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise GateError(f"{CONFIG_RELATIVE_PATH} could not be read: {exc}") from exc
    if not isinstance(config, dict):
        raise GateError(f"{CONFIG_RELATIVE_PATH} must contain a JSON object")
    return config


def check_enabled(config: dict, name: str) -> bool:
    checks = config.get("checks")
    if not isinstance(checks, dict):
        return True
    return checks.get(name, True) is not False


def resolve_default_branch(repo_root: str, config: dict) -> str:
    """The trunk, used as the base that a work branch is measured against.

    Config wins; then whatever origin says its HEAD is; then `main`.
    """
    configured = config.get("default_branch")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    code, out = git(repo_root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if code == 0 and out.startswith("origin/"):
        return out[len("origin/"):]
    return "main"


def resolve_protected_branches(config: dict, default_branch: str) -> tuple[str, ...]:
    """The branches work must not accumulate on. A set, deliberately, not one name.

    Resolving a single trunk from `origin/HEAD` is right for a normal clone and wrong in
    a real case that bit this gate on its first live test: a clone taken from a checkout
    that was sitting on a feature branch inherits `origin/HEAD -> origin/<feature>`, so
    `main` stopped being the default and edits on `main` sailed through. Protecting the
    resolved trunk *and* the two conventional names costs nothing -- nobody works
    directly on a `main` that is not their trunk -- and removes a silent-failure mode
    that fourteen green self-test cases had not noticed.

    `protected_branches` in the config replaces the set outright, for the repo where that
    assumption really is wrong.
    """
    configured = config.get("protected_branches")
    if isinstance(configured, list) and configured:
        names = tuple(str(item).strip() for item in configured if str(item).strip())
        if names:
            return names
    return tuple(dict.fromkeys((default_branch, "main", "master")))


def plan_globs(config: dict) -> tuple[str, ...]:
    configured = config.get("plan_globs")
    if isinstance(configured, list) and configured:
        globs = tuple(str(item) for item in configured if str(item).strip())
        if globs:
            return globs
    return DEFAULT_PLAN_GLOBS


def is_plan_file(path: str, globs: tuple[str, ...]) -> bool:
    normalised = path.replace("\\", "/").lower()
    return any(fnmatch.fnmatchcase(normalised, glob.lower()) for glob in globs)


# ----------------------------------------------------------------------- checks
#
# Each check returns a reason string when the invariant is broken, or None when it holds
# or cannot be evaluated. Cannot-evaluate is always a pass: a gate that blocks because it
# is confused trains the session to ignore it.


def check_branch_hygiene(repo_root: str, config: dict, default_branch: str) -> str | None:
    """Work must not accumulate on a protected branch.

    Untracked files never count. A session that has generated a scratch file, or a
    reviewer's stray .docx sitting in docs/, has not started work on main.
    """
    branch = current_branch(repo_root)
    if branch is None or branch not in resolve_protected_branches(config, default_branch):
        return None

    problems: list[str] = []

    # `diff --name-only HEAD` rather than `status --porcelain`: it covers staged and
    # unstaged alike, excludes untracked, and returns bare paths. Porcelain's "XY PATH"
    # needs the leading status column intact, and git() strips its output -- which
    # silently removed the first character of every filename it reported.
    code, dirty = git(repo_root, "diff", "--name-only", "HEAD")
    if code == 0 and dirty:
        changed = [line for line in dirty.splitlines() if line.strip()]
        shown = ", ".join(changed[:5]) + ("…" if len(changed) > 5 else "")
        problems.append(f"{len(changed)} tracked file(s) modified ({shown})")

    upstream = "@{upstream}"
    code, _ = git(repo_root, "rev-parse", "--verify", "--quiet", upstream)
    if code != 0:
        remote_ref = f"origin/{default_branch}"
        upstream = remote_ref if ref_exists(repo_root, remote_ref) else ""
    if upstream:
        code, count = git(repo_root, "rev-list", "--count", f"{upstream}..HEAD")
        if code == 0 and count.isdigit() and int(count) > 0:
            problems.append(f"{count} commit(s) ahead of {upstream}")

    if not problems:
        return None

    # Base the advice on the branch actually checked out, not on the resolved trunk:
    # when origin/HEAD is stale those differ, and naming the wrong ref here would send
    # the session somewhere worse than where it started.
    base = f"origin/{branch}" if ref_exists(repo_root, f"origin/{branch}") else branch

    return (
        f"You are on '{branch}', which the turn gate protects, with "
        + " and ".join(problems) + ".\n"
        "One session = one work unit = one PR, and it starts by branching off main.\n"
        f"Fix: git checkout -b <branch> {base}   "
        "(uncommitted changes come with you; commits need cherry-picking, then "
        f"reset {branch} back to {base})."
    )


def check_plan_first(repo_root: str, config: dict, default_branch: str) -> str | None:
    """The branch's first commit must be the plan, alone, before any code.

    A6's rule. Three sessions have died after planning was done, and what died with them
    was the plan -- plan mode's file lives outside the repo, so a fresh checkout and a
    cloud session cannot see it.

    Exempt by the rule's own terms: single-file branches (PROCESS.md requires plan mode
    for *multi-file* work), branches with nothing committed yet, and a first commit
    carrying a `Plan-exempt: <reason>` trailer.
    """
    branch = current_branch(repo_root)
    if branch is None or branch in resolve_protected_branches(config, default_branch):
        return None

    base_ref = f"origin/{default_branch}"
    if not ref_exists(repo_root, base_ref):
        base_ref = default_branch
        if not ref_exists(repo_root, base_ref):
            return None

    code, base = git(repo_root, "merge-base", "HEAD", base_ref)
    if code != 0 or not base:
        return None

    code, listed = git(repo_root, "rev-list", "--reverse", "--topo-order", f"{base}..HEAD")
    commits = listed.split()
    if code != 0 or not commits:
        return None

    code, diff = git(repo_root, "diff", "--name-only", base, "HEAD")
    changed = [line for line in diff.splitlines() if line.strip()]
    if code != 0 or len(changed) < 2:
        return None

    first = commits[0]

    # A merge as the first commit ahead of base is a rebase artefact or an integration,
    # not the case this rule is about, and `git show --name-only` prints nothing for one.
    code, parents = git(repo_root, "rev-list", "--parents", "-n", "1", first)
    if code == 0 and len(parents.split()) > 2:
        return None

    code, message = git(repo_root, "log", "-1", "--format=%B", first)
    exempt = PLAN_EXEMPT_TRAILER.search(message) if code == 0 else None
    if exempt:
        return None

    code, shown = git(repo_root, "show", "--name-only", "--format=", first)
    first_files = [line for line in shown.splitlines() if line.strip()]
    globs = plan_globs(config)
    if first_files and all(is_plan_file(path, globs) for path in first_files):
        return None

    listed_files = ", ".join(first_files[:5]) + ("…" if len(first_files) > 5 else "")
    return (
        f"The first commit on '{branch}' ({first[:8]}) is not the plan.\n"
        f"It touches: {listed_files or '(nothing)'}\n"
        f"The plan must be committed on its own, before any code, matching "
        f"{' or '.join(globs)} -- a session can end without warning, and what dies with "
        "it is whatever was held only in the conversation.\n"
        "Fix: write the plan doc and rebase it to the front, or record a deliberate "
        "skip with a `Plan-exempt: <reason>` trailer on that first commit."
    )


CHECKS = (
    ("branch_hygiene", check_branch_hygiene),
    ("plan_first", check_plan_first),
)


# ------------------------------------------------------------------- evaluation


def evaluate(payload: dict) -> dict | None:
    """Return the JSON document to print, or None to stay silent.

    Kept separate from main() so the self-test can drive the whole decision path without
    a subprocess, and so main() has nothing in it but I/O.
    """
    if payload.get("hook_event_name") != "Stop":
        return None

    # Blocked once already this stop-chain. Let the turn end.
    if payload.get("stop_hook_active"):
        return None

    started = time.monotonic()

    try:
        repo_root = repo_root_of(hook_directory())
        if repo_root is None:
            return None

        # An empty repository has no HEAD to reason about.
        if not ref_exists(repo_root, "HEAD"):
            return None

        config = load_config(repo_root)
        default_branch = resolve_default_branch(repo_root, config)

        reasons: list[str] = []
        for name, check in CHECKS:
            if time.monotonic() - started > WALL_CLOCK_BUDGET_SECONDS:
                return {
                    "systemMessage": (
                        "Turn gate: gave up after "
                        f"{WALL_CLOCK_BUDGET_SECONDS:.0f}s without finishing. Not blocking."
                    )
                }
            if not check_enabled(config, name):
                continue
            reason = check(repo_root, config, default_branch)
            if reason:
                reasons.append(reason)
    except GateError as exc:
        return {"systemMessage": f"Turn gate did not run: {exc}. Not blocking."}
    except Exception as exc:  # noqa: BLE001 -- fail open on anything, but say so
        return {"systemMessage": f"Turn gate errored: {exc!r}. Not blocking."}

    if not reasons:
        return None

    body = "\n\n".join(reasons)
    return {
        "decision": "block",
        "reason": (
            "The turn-level gate blocked this turn. Fix the following, then finish.\n\n"
            f"{body}\n\n"
            "This gate blocks once per turn -- if it is wrong, say so and continue; "
            "the next stop will not be blocked."
        ),
        "systemMessage": f"Turn gate blocked the turn ({len(reasons)} issue(s)).",
    }


def main() -> None:
    document = evaluate(json.load(sys.stdin))
    if document is not None:
        print(json.dumps(document))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Last resort. A hook that cannot even parse its input must not end a session
        # with a traceback; the visible-warning path above covers everything reachable.
        pass
    sys.exit(0)
