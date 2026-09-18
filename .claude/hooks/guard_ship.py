#!/usr/bin/env python3
"""PreToolUse hook: surface a visible warning on the irreversible git actions.

Everything up to opening a pull request runs unattended. Merging, pushing straight to a
protected branch, committing straight to one, force-moving a branch ref, mutating a working
tree that is not this session's own, and touching production are the points of no return,
so this hook names the reason -- the actual repo, branch and problem -- every time one of
them is attempted.

MEASURED 2026-09-08, twice, on two different command shapes (see
docs/doctrine/path-scoped-rules.md): this hook fires and returns
`permissionDecision: "ask"` correctly, but in an auto-accept permission mode the action
proceeds anyway, with no blocking prompt. What actually reaches the user is the
`systemMessage` this hook also returns below, which the harness renders naming the repo,
branch and reason, in both auto-accept and manual permission modes. So this is a working
NOTIFIER, not a gate -- the escalation list in CLAUDE.md still depends on the model
stopping on its own when it sees the warning.

THREE KINDS OF CHECK LIVE HERE.

  * Text patterns (GUARDED). Pure string matching on the command, no git involved. They
    fire regardless of which repo the command targets, which is exactly what is wanted
    for `gh pr merge`, a force-push, `deploy.sh` and an SSH to production.

  * MCP tool calls (MCP_MERGE_TOOLS, MCP_MAIN_WRITE_TOOLS, MCP_AUTO_MERGE_TOOLS). The
    same guarded actions reached through an MCP server instead of a shell: a merge, the
    file writes that can target a protected branch with no PR at all, and enabling
    auto-merge on the desktop app's own merge path. Matched on tool_name and (for the
    latter two) a field in tool_input, not on any command string -- there is no command
    string. The first two are ported from web-auditor#224, which exists because
    web-auditor PR #185 was merged through `mcp__github__merge_pull_request` on
    2026-07-28 and this hook, then registered only against `Bash`, never fired.
    MCP_AUTO_MERGE_TOOLS (A18) closes the same shape of hole for `mcp__ccd_pr__set_auto_merge`,
    found by grep rather than by an incident: this repo's own canonical copy had never
    named it. settings.json's PreToolUse matcher names all of these tools explicitly
    alongside `Bash`; the two have to be changed together.

  * Repo-state checks (REPO_CHECKS). These resolve *which repository the command will
    actually run in* -- from a `git -C <path>`, from a `cd <path> &&` earlier in the same
    command, or failing both from the payload's own `cwd` -- and then read that repo's
    real state. They exist because the session's own project root is not the only repo a
    session touches: the D2 session committed into three, and nothing forced a branch in
    the two it did not start in. See docs/guard-ship-commit-check-plan-V0-1.md.

    A path can only be resolved from what the command text says. `NAME=value` set earlier
    in the same command, `$HOME` and `$CLAUDE_PROJECT_DIR` are known; anything else the
    shell works out when it runs -- another variable, `$(...)`, a backtick -- is not. That
    used to resolve to a non-existent literal directory and silence every check, which is
    how six commits got through on `$SP` (A11,
    docs/guard-ship-unresolved-and-history-ops-plan-V0-1.md).

    A23: this is the TARGET root, and it deliberately still comes from the command text
    and, failing that, the payload's `cwd` -- NOT from `hook_directory()` (below). That is
    a separate, different root: `Session.own_root` answers "which repo is this session
    itself rooted in", used only to tell a foreign primary checkout from this session's
    own, and that one now DOES come from `hook_directory()`, the same fix A21 made to
    session_start.py's repo root, for the same reason (see `Session` below). The two
    roots answer different questions and only one can be pinned to where this file lives
    on disk: the target root has to stay whatever the command says, because catching a
    command aimed at a repository other than this session's own is the entire point of
    this layer -- pinning it to `hook_directory()` would make it blind to exactly that.

WHY THIS FILE AND NOT stop_gate.py. The Stop hook fires after the turn, sees only the one
directory the shell happens to be sitting in at that instant, and cannot undo a commit
that has already landed. This hook fires *before* the command runs, on the command text
itself, so it can ask first. Prevention beats detection.

FAILURE POSTURE, IN THREE LAYERS. Read them as one design, not three fixes.

  1. A check that cannot evaluate stays SILENT. Every repo-state check is written that
     way, the same posture stop_gate.py takes -- with one recorded exception,
     check_unresolved_target: a mutating command whose target the shell decides asks
     rather than passing, because silence there is exactly the failure this hook exists
     to prevent. This layer is unchanged.

  2. A crash inside the hook now ASKS, but only when the payload looks risky.
     crash_document() re-scans the raw stdin text for CRASH_RECHECK_WORDS and emits an
     ask naming the exception; on anything else it stays silent. This reverses the
     previous "fails open by design", and the reversal is deliberate (Pete, 2026-09-12,
     recorded in docs/doctrine/decisions-taken.md). The old reasoning was that the git
     and CI gates are the ones that must fail closed while this one is "only" a
     confirmation prompt. What that misses is stated a few paragraphs up: in an
     auto-accept mode this hook is a NOTIFIER, and a notifier that crashes is silent --
     indistinguishable from "nothing needed warning about". Across 1055 lines that shell
     out to git on every Bash call, that is the one failure mode a fail-open guard
     cannot be noticed failing in.

     Why the risk filter and not a flat ask: this hook fires before EVERY Bash command.
     A systematic bug under unconditional fail-closed would prompt on `ls`, which is not
     one extra confirmation but an unusable session. The filter bounds the blast radius
     to the commands worth being nagged about while a broken guard is fixed.

  3. A crash inside crash_document() itself falls open, at __main__. Last resort only.

The layer above all three is not in this file: settings.json's hook command ends in a
`|| printf` of a literal ask document, so a hook that cannot start at all -- no
interpreter on PATH, this file missing or holding a syntax error -- still produces one.
None of the layers covers another; all four are needed.

The git helpers below are DUPLICATED from hooks/stop_gate.py rather than imported. Hooks
in this repo are deliberately self-contained single files, copied one by one into other
projects by the apply-dev-process skill; hooks/session_start.py duplicates the same
helpers for the same reason. The duplication is the cost of the distribution model and is
a recorded decision, not an oversight.

Calibrated by hooks/guard_ship_selftest.py, run from CI.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time

# A single git call should take milliseconds. The timeout is for a hung or prompting git
# (credential helper, filesystem lock), not for slow work. Tighter than stop_gate.py's
# because this hook fires before *every* Bash command, not once per turn.
GIT_TIMEOUT_SECONDS = 5

# Total budget for the repo-state checks. Measured real cost (A11, #48): 8 git
# subprocesses, +260-360 ms on a commit/checkout -b or merge onto a protected branch.
# Overrunning means staying silent, never blocking.
WALL_CLOCK_BUDGET_SECONDS = 3.0

# Read from the target repo, not from this one: a cross-repo command should be judged by
# the conventions of the repo it lands in. Same file stop_gate.py reads, so a project that
# overrides its protected branches gets one answer from both hooks rather than two.
CONFIG_RELATIVE_PATH = os.path.join(".claude", "turn-gate.json")

# git's own global options, which sit between `git` and the subcommand. Walking past them
# is what makes `git -C /some/repo commit` parse as the subcommand `commit` rather than as
# the option value `/some/repo`.
GIT_GLOBAL_OPTIONS_TAKING_A_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--super-prefix", "--config-env", "--attr-source",
})

# Characters shlex(punctuation_chars=True) groups into their own tokens. A token made
# entirely of them ends the current command: `&&`, `||`, `|`, `;`, and redirections.
SHELL_PUNCTUATION = set(";&|<>()")

# Only used when the command cannot be lexed at all (an unbalanced quote). Splitting the
# raw text this way would break `-m "a; b"`, which is why it is the fallback and not the
# main path.
CRUDE_SEGMENT_SEPARATORS = re.compile(r"\|\||&&|[|;&\n]")

# A shell word that sets a variable. Only honoured when a whole segment is made of them
# (optionally after `export`): in `SP=/x git -C "$SP" ...` bash expands `$SP` *before* the
# prefix assignment takes effect, so recording it there would resolve to the wrong place.
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

# `$NAME` and `${NAME}`. Anything fancier -- `${X:-y}`, `$1`, `$$` -- is left unmatched, so
# it keeps its `$` and counts as unresolved.
VARIABLE_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

# What each `$(...)` and backtick span becomes before lexing. It keeps a `$` so a path built
# from it stays unresolved, and `[` makes it impossible to match as a variable name, so no
# inline assignment can ever give it a value.
SUBSTITUTION_PLACEHOLDER = "$[substitution]"


# (compiled pattern, reason shown in the confirmation prompt)
#
# The `git push ... main` pattern that used to live here has moved into check_push()
# below, so that `master` and a repo's real resolved trunk are covered by the same
# definition of "protected" that stop_gate.py uses. The literal text match survives there
# as the fallback for when the repo cannot be resolved.
#
# CORRECTION. This comment used to end "so nothing this list caught before is caught less
# now", and that was false. The move dropped `git push origin +main`: the old regex matched
# `\bmain\b` anywhere in the segment, so it caught the force shorthand by accident, and a
# membership test against branch *names* does not. `+master` and `+refs/heads/main` went
# with it, and `+feature` was never caught by either arm. Found post-merge by C1's
# calibration run against #28, not by the self-tests, which used no `+` refspec at all.
# Both arms now cover it -- the `+` strip in check_push, and the alternation below -- and
# cases 33-38 hold it there.
GUARDED = [
    (
        re.compile(r"\bgh\s+pr\s+merge\b"),
        "Merging to main deploys to production. This needs an explicit go from Pete in "
        "this session -- a green test suite on its own is not authorisation.",
    ),
    (
        # `\s\+\S` is the `+<refspec>` force shorthand, which reaches what the flags do
        # not: `git push origin +feature` rewrites history on a branch check_push has no
        # reason to protect, so this arm is the only one that can speak for it.
        #
        # Known over-match, recorded rather than left to be rediscovered: a `+` in a
        # trailing comment within the same segment -- `git push origin main # a + b` --
        # matches too. That is one extra reason on a command already being asked about,
        # and this fix exists because a missing ask cost more than a redundant one.
        re.compile(r"\bgit\s+push\b[^|;&]*(?:--force\b|-f\b|\s\+\S)"),
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
    """Return the reason to prompt on for the text patterns, or None if unguarded."""
    for pattern, reason in GUARDED:
        if pattern.search(command):
            return reason
    return None


# ------------------------------------------------------------------ MCP tool calls
#
# Ported from web-auditor#224 (`b797a4a`, 2026-08-10). It had not reached this copy, so
# every project apply-dev-process seeded from here was carrying the hole #224 closed.
#
# These are matched on tool_name, never on a command string, because an MCP tool call
# has none. The repo-state checks below cannot speak for them either: those resolve a
# local working directory, and an MCP call reaches GitHub without touching one.

# GitHub MCP tools that merge a PR outright. Guarded unconditionally -- the tool has no
# argument that makes a merge safe, and asking on a merge to some other repo costs one
# extra confirmation, not a false allow.
MCP_MERGE_TOOLS = frozenset({"mcp__github__merge_pull_request"})
MCP_MERGE_REASON = (
    "This calls the GitHub MCP tool's merge_pull_request, the MCP equivalent of `gh pr "
    "merge`. Merging to main deploys to production. This needs an explicit go from Pete "
    "in this session -- a green test suite on its own is not authorisation."
)

# GitHub MCP tools that write a file straight to a branch, no PR involved. Guarded when
# the target branch is main, and also when tool_input carries no branch at all: these
# tools take a required `branch` in the GitHub MCP server as published, so an absent one
# means the payload is not the shape this guard was written against, and it asks rather
# than assuming the target is safe.
#
# UNVERIFIED NAMES, carried over from #224 rather than resolved here. Three of these
# four are taken from the published GitHub MCP server tool set, not observed: no GitHub
# MCP server is configured on this machine either, so this port could not check them any
# more than #224 could. Only `merge_pull_request` has direct evidence behind it
# (web-auditor issue #188 records PR #185 being merged through it). If a name is wrong
# the hook silently does not fire for that tool. guard_ship_selftest.py pins these sets
# to the settings.json matcher, which catches the two drifting apart but cannot catch
# both being wrong together.
#
# Deliberately NOT generalised to a prefix match on `mcp__github__`: a read-only tool
# like get_file_contents would then prompt, and a guard that cries wolf on reads is a
# guard people learn to click through.
MCP_MAIN_WRITE_TOOLS = frozenset({
    "mcp__github__push_files",
    "mcp__github__create_or_update_file",
    "mcp__github__delete_file",
})
MCP_MAIN_WRITE_REASON = (
    "This writes directly to main via the GitHub MCP tool, the MCP equivalent of `git "
    "push ... main`. This bypasses the pull request and CI review. Confirm this is "
    "deliberate."
)


def _mcp_main_write_reason(tool_input: dict) -> str | None:
    """Reason for an MCP file-write tool, or None when it targets a safe branch.

    `main` is hard-coded rather than run through resolve_protected_branches(): that reads
    a local repo's config to learn its trunk, and an MCP call names a GitHub repo this
    machine may not even have a clone of. Under-covering a repo whose trunk is `master`
    is the known limit, recorded rather than papered over -- closing it needs the tool's
    `owner`/`repo` arguments resolved against the remote, which is a bigger change than
    #224's port.
    """
    branch = tool_input.get("branch")
    if branch is None or branch == "main":
        return MCP_MAIN_WRITE_REASON
    return None


# A18: the desktop app's own merge path. Found 2026-09-12 while doing #56 -- canonical
# guard_ship.py had no reference to this tool at all, so it was guarded by NO copy of the
# hook anywhere, and it is the client Pete actually merges from, unlike the two arms
# above, which are ports of a GitHub MCP server that is not even configured on this
# machine. Verifiable directly, the same way #224's `merge_pull_request` name is not: the
# tool is present in this client's own tool list.
#
# Guarded on `enabled`, not unconditionally like MCP_MERGE_TOOLS, and not by branch like
# MCP_MAIN_WRITE_TOOLS: the tool's own description says enabling "lands code without
# another look", but disabling "also leaves a merge queue" -- it is not itself
# irreversible, so asking on it would be a guard that cries wolf on a safe call, the exact
# failure MCP_MAIN_WRITE_TOOLS's prefix-match note above warns against. `enabled` absent
# is treated the same as `True`: the tool's own schema always includes it in practice, so
# an absent key means the payload is not the shape this guard expects, and it asks rather
# than assuming the call is a safe disable -- same posture as `_mcp_main_write_reason`'s
# absent `branch`.
MCP_AUTO_MERGE_TOOLS = frozenset({"mcp__ccd_pr__set_auto_merge"})
MCP_AUTO_MERGE_REASON = (
    "This enables auto-merge on the bound pull request, the desktop app's own merge "
    "path -- the PR lands as soon as checks pass, with no further look. Merging to main "
    "deploys to production. This needs an explicit go from Pete in this session -- a "
    "green test suite on its own is not authorisation."
)


def _mcp_auto_merge_reason(tool_input: dict) -> str | None:
    """Reason for set_auto_merge, or None when it is disabling auto-merge."""
    if tool_input.get("enabled") is False:
        return None
    return MCP_AUTO_MERGE_REASON


# ----------------------------------------------------------------------- paths


def same_path(left: str | None, right: str | None) -> bool:
    """Path equality that survives Windows' case-insensitive, separator-agnostic paths."""
    if not left or not right:
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
        os.path.normpath(right)
    )


# --------------------------------------------------------------------------- git


def git(directory: str, *args: str) -> tuple[int, str]:
    """Run a git command in `directory`. Returns (exit code, stripped stdout).

    Never raises. Unlike stop_gate.py's equivalent -- which raises so that a broken gate
    can announce itself -- this hook is a confirmation prompt in front of every Bash
    command, so a missing git binary or a hung filesystem must degrade to silence rather
    than to noise on every call. A non-zero code is the caller's signal to stop.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", directory, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def hook_directory() -> str:
    """Where THIS file lives on disk, absolute. Isolated to one call site -- same reason
    as the other single-call-site functions in this file -- so a self-test can substitute
    a fake location without needing a real copy of this file sitting there.

    A23: the anchor for `Session.own_root`, ported from session_start.py's A21 fix. See
    the module docstring's REPO_CHECKS section for why `RepoState.root` (the command's
    TARGET repo) is not changed the same way.
    """
    return os.path.dirname(os.path.abspath(__file__))


def repo_root_of(directory: str) -> str | None:
    """The work tree root containing `directory`, or None if it is not a repository."""
    if not directory or not os.path.isdir(directory):
        return None
    code, out = git(directory, "rev-parse", "--show-toplevel")
    if code != 0 or not out:
        return None
    return os.path.realpath(out)


def current_branch(repo_root: str) -> str | None:
    """The checked-out branch, or None when HEAD is detached or unreadable."""
    code, out = git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out if code == 0 and out else None


def _resolved_git_path(directory: str, flag: str) -> str | None:
    """`git rev-parse <flag>` in `directory`, as an absolute, symlink-resolved path.

    MEASURED, git 2.55.0.windows.3, because the raw strings cannot be compared directly:
    a primary checkout answers `.git` for both --git-dir and --git-common-dir, a worktree
    answers two absolute paths, and from a subdirectory --git-common-dir answers `../.git`
    -- relative to the directory git ran in, not to the work tree root. Resolving against
    that directory is what makes the three cases comparable.
    """
    code, out = git(directory, "rev-parse", flag)
    if code != 0 or not out:
        return None
    return os.path.realpath(os.path.join(directory, out))


def is_primary_checkout(directory: str) -> bool | None:
    """True when `directory` is a repo's original checkout rather than a `worktree add`.

    None when it cannot be told, which callers treat as "stay silent".
    """
    git_dir = _resolved_git_path(directory, "--git-dir")
    common_dir = _resolved_git_path(directory, "--git-common-dir")
    if git_dir is None or common_dir is None:
        return None
    return same_path(git_dir, common_dir)


def shared_git_dir(directory: str) -> str | None:
    """The `.git` directory shared by every worktree of one repo -- the repo's identity.

    Comparing work-tree paths would call a worktree of our own repository a stranger;
    comparing this does not.
    """
    return _resolved_git_path(directory, "--git-common-dir")


# ------------------------------------------------------------------ configuration


def load_config(repo_root: str) -> dict:
    """Read the target repo's .claude/turn-gate.json, or {} if absent or unreadable.

    stop_gate.py surfaces a malformed file, because a silently-defaulted gate is the
    failure mode it exists to catch. Here the file is consulted only for the *names* of
    protected branches, and defaulting to (trunk, main, master) protects more rather than
    less, so an unreadable file degrades quietly.
    """
    path = os.path.join(repo_root, CONFIG_RELATIVE_PATH)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def resolve_default_branch(repo_root: str, config: dict) -> str:
    """The trunk. Config wins; then whatever origin says its HEAD is; then `main`."""
    configured = config.get("default_branch")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    code, out = git(
        repo_root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
    )
    if code == 0 and out.startswith("origin/"):
        return out[len("origin/"):]
    return "main"


def resolve_protected_branches(config: dict, default_branch: str) -> tuple[str, ...]:
    """The branches work must not land on directly. A set, deliberately, not one name.

    Same rule as stop_gate.resolve_protected_branches, and for the same measured reason: a
    clone taken from a checkout sitting on a feature branch inherits
    `origin/HEAD -> origin/<feature>`, so `main` quietly stops being the default. Guarding
    the resolved trunk *and* the two conventional names costs nothing.
    """
    configured = config.get("protected_branches")
    if isinstance(configured, list) and configured:
        names = tuple(str(item).strip() for item in configured if str(item).strip())
        if names:
            return names
    return tuple(dict.fromkeys((default_branch, "main", "master")))


# ------------------------------------------------------------- command parsing


class GitInvocation:
    """One `git <subcommand>` found in a Bash command, with the directory it will run in.

    `directory` is already absolute: the `-C` path if the invocation carried one, else the
    directory a preceding `cd` left the shell in, else the payload's `cwd`. It is None when
    the shell decides it at run time, and `unresolved_word` then holds the word that did it.
    """

    def __init__(
        self,
        subcommand: str,
        args: list[str],
        directory: str | None,
        unresolved_word: str | None = None,
    ) -> None:
        self.subcommand = subcommand
        self.args = args
        self.directory = directory
        self.unresolved_word = unresolved_word

    def has_flag(self, *flags: str) -> bool:
        return any(arg in flags for arg in self.args)

    def positional_args(self) -> list[str]:
        """Arguments that are not options. Approximate, and used only where that is safe.

        An option taking a separate value (`git push -o ci.skip ...`) leaves that value
        here. The one caller that counts these reads a larger count as "there is a
        refspec, stay quiet", so the error direction is a missed prompt, never a spurious
        one.
        """
        return [arg for arg in self.args if not arg.startswith("-")]


def _lex_line(line: str) -> list[str] | None:
    """Shell-tokenise one line, keeping `&&`, `|`, `;` as tokens. None if unparseable.

    `punctuation_chars=True` is what keeps a separator *inside* a quoted string from
    splitting the command: `git commit -m "a; git push origin main"` must not look like a
    push. POSIX quoting, because the Bash tool runs Git Bash -- so an unquoted Windows
    path loses its backslashes, which is correct, since bash would eat them too and the
    command would not have worked either.
    """
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:  # unbalanced quote
        return None


def swallow_substitutions(command: str) -> str:
    """Replace each `$(...)` and backtick span with SUBSTITUTION_PLACEHOLDER.

    Two reasons, both measured by the self-test. Unquoted, `git -C $(pwd) commit` splits at
    `(` under punctuation_chars, leaving `-C` with no subcommand after it -- silent for a
    parse reason before resolution is even reached (case 65). And a heredoc message inside
    `"$(cat <<'EOF' ... EOF)"` would otherwise be lexed line by line as if every line of
    the message were a command (case 66). An unbalanced `$(` or backtick is left exactly
    as it was, for the lexer's existing fallback to deal with.
    """
    pieces: list[str] = []
    index = 0
    while index < len(command):
        if command.startswith("$(", index):
            end = _matching_parenthesis(command, index + 1)
        elif command[index] == "`":
            end = command.find("`", index + 1)
        else:
            pieces.append(command[index])
            index += 1
            continue
        if end == -1:
            pieces.append(command[index:])
            break
        pieces.append(SUBSTITUTION_PLACEHOLDER)
        index = end + 1
    return "".join(pieces)


def _matching_parenthesis(text: str, opening: int) -> int:
    """Index of the `)` closing the `(` at `opening`, or -1 if it is never closed."""
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def command_tokens(command: str) -> list[str]:
    """The whole command as shell words, with `;` standing in for every line break."""
    tokens: list[str] = []
    for index, line in enumerate(command.splitlines()):
        if index:
            tokens.append(";")
        lexed = _lex_line(line)
        if lexed is not None:
            tokens.extend(lexed)
            continue
        # Unparseable: fall back to crude text splitting rather than seeing nothing.
        for part_index, part in enumerate(CRUDE_SEGMENT_SEPARATORS.split(line)):
            if part_index:
                tokens.append(";")
            tokens.extend(part.split())
    return tokens


def _is_separator(token: str) -> bool:
    return bool(token) and all(character in SHELL_PUNCTUATION for character in token)


def _is_git_word(token: str) -> bool:
    base = os.path.basename(token.replace("\\", "/")).lower()
    return base in ("git", "git.exe")


def _variable_value(name: str, variables: dict[str, str]) -> str | None:
    """A variable's value if this hook can know it for certain, else None.

    Inline assignments first, so `HOME=/x; cd "$HOME"` means what bash means. Then the two
    the hook knows exactly: `HOME` through expanduser, and `CLAUDE_PROJECT_DIR`, which the
    harness sets for this hook and for the Bash tool alike. Nothing else is read from the
    environment -- the hook's environment is not the Bash tool's shell, and a wrong answer
    here is a silent miss, the very failure A11 exists to remove.
    """
    if name in variables:
        return variables[name]
    if name == "HOME":
        return os.path.expanduser("~")
    if name == "CLAUDE_PROJECT_DIR":
        return os.environ.get("CLAUDE_PROJECT_DIR") or None
    return None


def _expand_word(word: str, variables: dict[str, str]) -> str | None:
    """`word` with every known variable substituted, or None if the shell must decide it."""

    def substitute(match: re.Match) -> str:
        value = _variable_value(match.group(1) or match.group(2), variables)
        return match.group(0) if value is None else value

    expanded = VARIABLE_REFERENCE.sub(substitute, word)
    if "$" in expanded or "`" in expanded:
        return None
    return expanded


def _record_assignments(segment: list[str], variables: dict[str, str]) -> bool:
    """Record a segment made only of `NAME=value` words. False if it is anything else.

    A value that cannot itself be expanded un-sets the name rather than recording a guess,
    so a later `$NAME` is correctly reported as unresolved.
    """
    words = segment[1:] if segment[0] == "export" else segment
    matches = [ASSIGNMENT.match(word) for word in words]
    if not words or not all(matches):
        return False
    for match in matches:
        value = _expand_word(match.group(2), variables)
        if value is None:
            variables.pop(match.group(1), None)
        else:
            variables[match.group(1)] = value
    return True


def _join_directory(
    base: str | None, addition: str, variables: dict[str, str]
) -> str | None:
    """Apply a `cd` or `-C` path to a base directory, an absolute one overriding.

    None when `addition` cannot be expanded, or when it is relative to a base that is
    itself unknown.
    """
    expanded = _expand_word(addition, variables)
    if expanded is None:
        return None
    expanded = os.path.expanduser(expanded)
    if base is None and not os.path.isabs(expanded):
        return None
    return os.path.abspath(os.path.join(base or "", expanded))


def parse_git_invocations(command: str, base_directory: str) -> list[GitInvocation]:
    """Every `git <subcommand>` in `command`, each tagged with where it will run.

    Segments are walked in order so that a `cd` in one applies to the git calls after it,
    which is how every cross-repo command in the incident this hook answers was written.
    Once a `cd` goes somewhere the shell decides, the directory stays unknown until a
    later `cd` names an absolute path again.
    """
    invocations: list[GitInvocation] = []
    shell_directory: str | None = base_directory
    unresolved_word: str | None = None
    variables: dict[str, str] = {}
    segment: list[str] = []

    def flush() -> None:
        nonlocal shell_directory, unresolved_word
        if not segment:
            return
        if _record_assignments(segment, variables):
            return
        if segment[0] == "cd" and len(segment) >= 2 and not segment[1].startswith("-"):
            if _expand_word(segment[1], variables) is None:
                shell_directory, unresolved_word = None, segment[1]
            else:
                shell_directory = _join_directory(shell_directory, segment[1], variables)
                if shell_directory is not None:
                    unresolved_word = None
            return
        for index, token in enumerate(segment):
            if not _is_git_word(token):
                continue
            parsed = _parse_one_invocation(
                segment, index, shell_directory, unresolved_word, variables
            )
            if parsed is not None:
                invocations.append(parsed)
            return  # one command per segment; the first `git` in it is that command

    for token in command_tokens(swallow_substitutions(command)):
        if _is_separator(token):
            flush()
            segment = []
            continue
        segment.append(token)
    flush()

    return invocations


def _parse_one_invocation(
    tokens: list[str],
    git_index: int,
    shell_directory: str | None,
    unresolved_word: str | None,
    variables: dict[str, str],
) -> GitInvocation | None:
    """Walk past git's global options to the subcommand. None if there isn't one."""
    directory = shell_directory
    index = git_index + 1

    while index < len(tokens):
        token = tokens[index]
        if token in GIT_GLOBAL_OPTIONS_TAKING_A_VALUE:
            if index + 1 >= len(tokens):
                return None
            if token == "-C":
                # Repeated -C is cumulative in git, and an absolute one wins outright --
                # which is exactly what os.path.join does.
                word = tokens[index + 1]
                if _expand_word(word, variables) is None:
                    directory, unresolved_word = None, word
                else:
                    directory = _join_directory(directory, word, variables)
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return GitInvocation(
            token,
            tokens[index + 1:],
            directory,
            unresolved_word if directory is None else None,
        )

    return None


# ------------------------------------------------------------------- repo state


class RepoState:
    """Everything the checks need about one target directory, read at most once."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.root = repo_root_of(directory)
        self.branch: str | None = None
        self.protected: tuple[str, ...] = ()
        self.primary: bool | None = None
        self.shared_git_dir: str | None = None
        if self.root is None:
            return
        config = load_config(self.root)
        self.branch = current_branch(self.root)
        self.protected = resolve_protected_branches(
            config, resolve_default_branch(self.root, config)
        )
        self.primary = is_primary_checkout(self.root)
        self.shared_git_dir = shared_git_dir(self.root)

    @property
    def is_repo(self) -> bool:
        return self.root is not None

    @property
    def on_protected_branch(self) -> bool:
        return bool(self.branch) and self.branch in self.protected


class Session:
    """The repo the session itself is rooted in, plus a cache of every repo seen.

    Its own root is resolved lazily. `git status`, `git diff` and `git log` are far and
    away the commonest git commands a session runs, none of them reach a check that needs
    this, and resolving it eagerly measured at roughly 70 ms on every one of them.

    A23: OWN ROOT COMES FROM `hook_directory()`, NOT `CLAUDE_PROJECT_DIR`. It used to be
    `repo_root_of(os.environ.get("CLAUDE_PROJECT_DIR") or "")`, and that variable is
    measured ABSENT from the desktop client's tool environment (session_start.py's A21
    docstring; reconfirmed from a live session here, not only from the Bash tool's own
    environment). An absent variable meant `own_root` was `None` on every desktop-app
    session -- which does not merely under-report, it silently disabled
    `check_foreign_primary_tree` outright, the one check written specifically to catch
    the A10/A11 primary-checkout collision this file exists to prevent. `hook_directory()`
    does not depend on any harness-reported value, the same fix A21 applied to
    session_start.py's repo root and for the same reason. `RepoState.root` (the command's
    TARGET repo, above) is deliberately NOT changed the same way -- see the module
    docstring's REPO_CHECKS section.
    """

    def __init__(self, base_directory: str) -> None:
        self.base_directory = base_directory
        self._states: dict[str, RepoState] = {}
        self._own_resolved = False
        self._own_root: str | None = None
        self._own_shared_git_dir: str | None = None

    def _resolve_own(self) -> None:
        if self._own_resolved:
            return
        self._own_resolved = True
        self._own_root = repo_root_of(hook_directory())
        if self._own_root:
            self._own_shared_git_dir = shared_git_dir(self._own_root)

    @property
    def own_root(self) -> str | None:
        self._resolve_own()
        return self._own_root

    @property
    def own_shared_git_dir(self) -> str | None:
        self._resolve_own()
        return self._own_shared_git_dir

    def state(self, directory: str) -> RepoState:
        key = os.path.normcase(os.path.abspath(directory))
        if key not in self._states:
            self._states[key] = RepoState(directory)
        return self._states[key]


# ---------------------------------------------------------------------- checks


# Every subcommand that writes history onto the checked-out branch, mapped to the flags
# that stop it doing so. Each exemption has a self-test case, and so does each flag that
# only LOOKS like one: merge's `-n` is --no-stat, and revert's --no-edit still commits.
HISTORY_WRITERS: dict[str, frozenset[str]] = {
    "commit": frozenset(),
    "merge": frozenset({"--abort", "--quit", "--ff-only"}),
    "cherry-pick": frozenset({"--abort", "--quit", "--no-commit", "-n"}),
    "revert": frozenset({"--abort", "--quit", "--no-commit", "-n"}),
    "rebase": frozenset({"--abort", "--quit", "--edit-todo", "--show-current-patch"}),
    "pull": frozenset({"--ff-only"}),
}

HISTORY_VERBS = {
    "commit": "commits",
    "merge": "merges",
    "cherry-pick": "cherry-picks",
    "revert": "reverts",
    "rebase": "rebases",
    "pull": "pulls",
}


def _writes_history(invocation: GitInvocation) -> bool:
    """True when this invocation lands new commits on whatever branch is checked out.

    Known over-match: `git rebase main feature`, run while sitting on main, rebases
    `feature`, not main -- and still asks. That is one redundant ask; this file's standing
    rule is that a missing ask costs more.
    """
    exemptions = HISTORY_WRITERS.get(invocation.subcommand)
    if exemptions is None:
        return False
    return not invocation.has_flag(*exemptions)


def check_history_on_protected(invocation: GitInvocation, session: Session) -> str | None:
    """History written onto a protected branch, in whichever repo it lands in.

    stop_gate.py sees turn-end state only, and only for wherever the shell happens to be
    sitting when the turn ends -- so a commit (or merge, or pull) straight onto main in a
    repo the session has since moved out of is silent and permanent.
    """
    if invocation.directory is None or not _writes_history(invocation):
        return None
    state = session.state(invocation.directory)
    if not state.is_repo or not state.on_protected_branch:
        return None
    verb = HISTORY_VERBS[invocation.subcommand]
    fast_forward_note = (
        " `--ff-only` would not ask: it adds no local commit."
        if invocation.subcommand in ("merge", "pull")
        else ""
    )
    return (
        f"This {verb} onto '{state.branch}', a protected branch, in {state.root}. "
        "Branch first -- `git checkout -b <name>` -- so the work stays reviewable and a "
        "session that dies leaves the trunk clean. Confirm only if writing history "
        f"straight to '{state.branch}' is deliberate.{fast_forward_note}"
    )


# `git stash` subcommands that leave the tree alone. A bare `git stash` is a push.
READ_ONLY_STASH_SUBCOMMANDS = frozenset({"list", "show", "create"})


def _mutates_a_working_tree(invocation: GitInvocation) -> bool:
    """The operations that change a checkout's branch, files, index or history.

    The history writers count here with ANY flags: `git rebase --abort` still rewrites the
    working tree of whichever session is sitting in that checkout. So does `reset` in every
    mode -- even a mixed reset rewrites the index another session has staged into -- and
    `checkout`/`switch` with any argument, an existing branch or a `-- <path>` alike (A12).
    Still not covered, recorded in docs/a12-guard-ship-honesty-and-coverage-plan-V0-1.md:
    `add`, `rm`, `mv`, `am`, `apply` and `worktree remove --force`.
    """
    subcommand = invocation.subcommand
    if subcommand in HISTORY_WRITERS or subcommand in ("reset", "restore"):
        return True
    if subcommand in ("checkout", "switch"):
        return bool(invocation.args)  # bare, they only report
    if subcommand == "stash":
        positional = invocation.positional_args()
        return not positional or positional[0] not in READ_ONLY_STASH_SUBCOMMANDS
    if subcommand == "clean":
        return not invocation.has_flag("-n", "--dry-run")
    return False


def check_foreign_primary_tree(invocation: GitInvocation, session: Session) -> str | None:
    """Any git-mutating command against a primary checkout that is not this session's.

    This is what the second incident actually was: the branch was correct, the *tree* was
    the problem. A primary checkout is a shared, contended resource -- another session may
    be sitting in it right now and will find its working tree on a branch it never asked
    for. A `git worktree add` tree is nobody else's, which is why the question asked here
    is whether the target is primary, not merely whether it is a different repo.
    """
    if invocation.directory is None or not _mutates_a_working_tree(invocation):
        return None
    if session.own_root is None:
        return None  # cannot tell whose tree this is; stay silent
    state = session.state(invocation.directory)
    if not state.is_repo or same_path(state.root, session.own_root):
        return None
    if state.primary is not True:
        return None  # a worktree: exactly the pattern this check steers towards

    same_repo = same_path(state.shared_git_dir, session.own_shared_git_dir)
    whose = (
        "this repository's own primary checkout, which this session is not rooted in"
        if same_repo
        else "another repository's primary checkout"
    )
    return (
        f"This mutates {state.root} -- {whose}. A primary checkout is shared: a "
        "concurrently-running session there will find its working tree on a branch it "
        "never asked for, which has already cost one session real time. Use "
        f"`git worktree add` under {state.root}, or hand the work to a session already "
        "rooted there."
    )


def _protected_names(
    invocation: GitInvocation, session: Session
) -> tuple[tuple[str, ...], str, RepoState | None]:
    """The target repo's protected branch names, ' in <root>' for messages, and its state.

    When the repo cannot be resolved -- not a repository, or a directory the shell decides
    -- fall back to the conventional names, so a check never catches less than the literal
    `main` pattern check_push replaced.
    """
    state = session.state(invocation.directory) if invocation.directory else None
    if state is not None and state.is_repo:
        return state.protected, f" in {state.root}", state
    return ("main", "master"), "", state


def _update_ref_targets_a_branch(ref: str) -> bool:
    """Whether `git update-ref <ref>` moves a branch.

    A bare name does not: measured on git 2.55, `git update-ref main X` writes .git/main
    and leaves refs/heads/main alone.
    """
    return ref == "HEAD" or ref.startswith("refs/heads/")


def _update_ref_target(invocation: GitInvocation) -> str | None:
    """The ref an `update-ref` moves: its first positional, once `-m <reason>` is skipped."""
    args = invocation.args
    index = 0
    while index < len(args):
        if args[index] == "-m":
            index += 2
            continue
        if not args[index].startswith("-"):
            return args[index]
        index += 1
    return None


# The flag that force-creates a branch, taking its name as the next word.
FORCE_CREATE_FLAGS = {"checkout": ("-B",), "switch": ("-C", "--force-create")}


def _force_moved_branch(invocation: GitInvocation) -> str | None:
    """The branch a `branch -f/-M/-C`, `checkout -B` or `switch -C` overwrites, if any."""
    if invocation.subcommand == "branch":
        if invocation.has_flag("-d", "-D", "--delete"):
            return None  # deletion, not a move; recorded as out of scope
        positional = invocation.positional_args()
        if not positional:
            return None
        forced = invocation.has_flag("-f", "--force")
        renaming = invocation.has_flag("-m", "-c", "--move", "--copy")
        if invocation.has_flag("-M", "-C") or (forced and renaming):
            return positional[-1]  # [<old>] <new>: the new name is what gets overwritten
        return positional[0] if forced else None  # <name> [<start-point>]

    flags = FORCE_CREATE_FLAGS.get(invocation.subcommand, ())
    for index, arg in enumerate(invocation.args[:-1]):
        if arg in flags:
            return invocation.args[index + 1]
    return None


def check_ref_moves(invocation: GitInvocation, session: Session) -> str | None:
    """A branch ref moved without a merge (A12).

    Refs are shared by every worktree of a repository, so which tree the command runs in
    does not matter here, only which ref it moves. Two shapes, measured on git 2.55:

      * `update-ref` on any branch. It has no checked-out guard -- it moved a branch another
        worktree had checked out, without complaint -- so it asks whatever the branch.
      * `branch -f/-M/-C`, `checkout -B` and `switch -C` onto a protected branch. git
        refuses these for a branch checked out anywhere, so the harm left is a protected
        branch rewritten while no tree has it checked out.

    Deliberately not here: `reset` on the session's own protected branch. `reset --hard
    origin/main` is the routine sync, and a branch moved forward only reaches origin
    through a push, which check_push already reads.
    """
    if invocation.subcommand == "update-ref":
        if invocation.has_flag("--stdin"):
            target = "the refs it reads from stdin"
        else:
            ref = _update_ref_target(invocation)
            if ref is None or not _update_ref_targets_a_branch(ref):
                return None
            target = f"'{ref}'"
        return (
            f"This moves {target} with `git update-ref`, which does not refuse a branch "
            "checked out in another worktree -- unlike `git branch -f` -- so it can move "
            "another session's branch out from under it, or a protected one with no "
            "review. Confirm the ref and the commit are deliberate."
        )

    name = _force_moved_branch(invocation)
    if name is None:
        return None
    protected, where, _ = _protected_names(invocation, session)
    for candidate in (name, name.removeprefix("refs/heads/")):
        if candidate in protected:
            return (
                f"This force-moves '{candidate}', a protected branch{where}, to wherever "
                "the command points it -- no merge and no review. Confirm this is "
                "deliberate."
            )
    return None


def check_push(invocation: GitInvocation, session: Session) -> str | None:
    """`git push` that lands on a protected branch, by name or by implication."""
    if invocation.subcommand != "push":
        return None
    if invocation.has_flag("--dry-run", "-n"):
        return None

    protected, where, state = _protected_names(invocation, session)

    for arg in invocation.args:
        if arg.startswith("-"):
            continue
        # A refspec's destination is what matters: `HEAD:main` and `:main` both land on
        # main, and a bare `main` does too.
        #
        # The leading `+` comes off first. It is git's force shorthand, so `+main` is a
        # force-push onto main and the single most destructive command in this hook's
        # remit -- and it went silent here for the whole life of #28, because `"+main"`
        # was compared for membership against the protected set without stripping it.
        # `+HEAD:main` escaped only by accident, the rsplit below discarding the `+`
        # along with the source. removeprefix, not lstrip: git allows exactly one.
        target = arg.removeprefix("+").rsplit(":", 1)[-1]
        for name in (target, target.replace("refs/heads/", "", 1)):
            if name in protected:
                return (
                    f"This pushes directly to '{name}'{where}, bypassing the pull "
                    "request and CI review. Confirm this is deliberate."
                )

    # A push that names no destination pushes the current branch: a bare `git push`,
    # `git push <remote>`, and `git push <remote> HEAD` all do. Recorded in the plan as an
    # addition to it -- this shape matched neither the old literal pattern nor a
    # name-based generalisation of it, and it is the commonest form of all.
    #
    # The first positional is the remote, so the refspecs are what follows it. `HEAD` on
    # its own is a source with no explicit destination, which is why it counts here rather
    # than in the loop above.
    refspecs = invocation.positional_args()[1:]
    names_no_destination = all(spec == "HEAD" for spec in refspecs)
    if names_no_destination and state is not None and state.is_repo and state.on_protected_branch:
        return (
            f"This pushes the current branch, '{state.branch}', directly{where} -- "
            "bypassing the pull request and CI review. Confirm this is deliberate."
        )

    return None


def _is_mutating(invocation: GitInvocation) -> bool:
    if _mutates_a_working_tree(invocation):
        return True
    if invocation.subcommand == "update-ref" or _force_moved_branch(invocation) is not None:
        return True
    return invocation.subcommand == "push" and not invocation.has_flag("--dry-run", "-n")


def check_unresolved_target(invocation: GitInvocation, session: Session) -> str | None:
    """A mutating command whose target directory the shell decides, not the command text.

    The one recorded exception to "cannot evaluate means stay silent" (see the module
    docstring and .claude/rules/hook-authoring.md). Every other check here needs a directory
    to read; without one they all go quiet, and that is how six commits got through on
    `$SP`. So this one speaks instead -- for mutating commands only, which is what keeps a
    `cd "$X" && git status` free of noise.
    """
    if invocation.directory is not None or not _is_mutating(invocation):
        return None
    word = invocation.unresolved_word or "the target path"
    return (
        f"This runs `git {invocation.subcommand}`, but this hook could not tell which "
        f"repository: '{word}' is only worked out by the shell when it runs (a variable "
        "set outside this command, `$(...)` or a backtick). Confirm the target is not a "
        "protected branch and not a checkout another session is using -- or write the "
        "path out literally so the check can read it."
    )


REPO_CHECKS = (
    check_unresolved_target,
    check_history_on_protected,
    check_ref_moves,
    check_foreign_primary_tree,
    check_push,
)


def repo_reasons(command: str, base_directory: str) -> list[str]:
    """Every repo-state reason this command earns. Empty on anything unevaluable."""
    if "git" not in command:
        return []  # the overwhelmingly common case: no subprocess, no cost

    deadline = time.monotonic() + WALL_CLOCK_BUDGET_SECONDS
    session = Session(base_directory)
    reasons: list[str] = []

    for invocation in parse_git_invocations(command, base_directory):
        for check in REPO_CHECKS:
            if time.monotonic() > deadline:
                return reasons
            reason = check(invocation, session)
            if reason and reason not in reasons:
                reasons.append(reason)

    return reasons


# ----------------------------------------------------------------- entry point


def _ask(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }


def evaluate(payload: dict) -> dict | None:
    """The hook's whole decision. None means stay silent and let the command run."""
    tool_name = payload.get("tool_name")

    # The MCP arms come first and return outright: they share no machinery with the Bash
    # path below, which parses a command string these tool calls do not have.
    if tool_name in MCP_MERGE_TOOLS:
        return _ask(MCP_MERGE_REASON)
    if tool_name in MCP_MAIN_WRITE_TOOLS:
        reason = _mcp_main_write_reason(payload.get("tool_input") or {})
        return _ask(reason) if reason else None
    if tool_name in MCP_AUTO_MERGE_TOOLS:
        reason = _mcp_auto_merge_reason(payload.get("tool_input") or {})
        return _ask(reason) if reason else None

    if tool_name != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not command:
        return None

    reasons: list[str] = []
    text_reason = find_reason(command)
    if text_reason:
        reasons.append(text_reason)

    # This is the TARGET root's starting point, not the session's own -- see the module
    # docstring's A23 paragraph in REPO_CHECKS. It stays sourced from the payload/harness
    # deliberately: `hook_directory()` cannot stand in for "where will the Bash tool's
    # shell actually run this command", only for "where does this session live".
    base_directory = (
        payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    )
    if isinstance(base_directory, str) and base_directory:
        reasons.extend(repo_reasons(command, base_directory))

    if not reasons:
        return None

    # One prompt carrying every reason, rather than the first one winning: a command that
    # commits onto main in someone else's primary checkout has two separate problems, and
    # fixing only the one it happened to mention would be worse than useless.
    return _ask("\n\n".join(reasons))


# The literal strings that make a crashed-on payload worth speaking up about. Substrings,
# not word patterns and not a regex: this list is scanned when the hook has ALREADY
# crashed once, so the scan itself must have as close to no failure modes as a scan can.
#
# Over-matching is the accepted cost -- "git" appears in plenty of harmless text, and
# `gh ` has a trailing space only so it does not fire on every word containing "gh".
# Every false hit here is one extra message on a guard that is already broken; every
# miss is the silence this layer exists to end.
CRASH_RECHECK_WORDS = (
    "git",
    "gh ",
    "ssh",
    "deploy.sh",
    "mcp__github__",
    "mcp__ccd_pr__",
)


def crash_document(raw: str, exc: BaseException) -> dict | None:
    """What to emit when evaluate() raised. None means the payload looked harmless.

    Scans the RAW stdin text, not a parsed command: parsing is one of the things that can
    have failed, so anything derived from the payload may not exist. See the failure
    posture in the module docstring for why this asks at all.
    """
    if not isinstance(raw, str):
        return None
    haystack = raw.lower()
    if not any(word in haystack for word in CRASH_RECHECK_WORDS):
        return None
    return _ask(
        f"guard_ship.py could not evaluate this tool call ({type(exc).__name__}: {exc})."
        " The call mentions git, gh, ssh or a deploy, so this asks rather than allowing:"
        " the guard failed, which is not the same as it confirming the call is safe."
        " Worth fixing the hook -- until it is, it is not protecting anything."
    )


def decide(raw: str) -> dict | None:
    """The whole hook, from stdin text to output document. None means stay silent.

    Separate from main() so the self-test can exercise the crash path, which it cannot
    reach through evaluate(): the main case runner calls evaluate() directly, precisely
    so that an unexpected crash in an ordinary case stays loud instead of being absorbed
    into an ask here.
    """
    try:
        return evaluate(json.loads(raw))
    except Exception as exc:
        return crash_document(raw, exc)


def main() -> None:
    document = decide(sys.stdin.read())
    if document is not None:
        print(json.dumps(document))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Layer 3: crash_document() itself misbehaved, or stdin could not be read. Fall
        # open -- there is nothing left to ask WITH at this point, and a hook that cannot
        # produce output should not also stop the session.
        pass
    sys.exit(0)
