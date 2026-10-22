#!/usr/bin/env python3
"""Decide whether a push touches nothing but the repo's declared bookkeeping files.

Why this exists
---------------
The pre-push hook runs the full test suite on every push. A push that changes only files
no test reads (a loop's queue and to-do list, say) gains nothing from that and can cost
ten minutes. This script is the hook's one question: is every path in the pushed range a
declared bookkeeping path? CI still runs the full suite on the PR and on `main`, so a wrong
answer here costs a red PR, never a red `main`.

It fails closed. Exit 0 means "skip the tests" and is returned only when every case is
settled and every changed path is listed. Exit 1 means "run the tests", and so does any
other exit and any crash: the hook treats anything but 0 as "run them".

Usage (the hook does this; git supplies the stdin)
-----
    python scripts/check_push_scope.py <remote name> [<remote url>] < pre-push stdin

stdin is git's pre-push feed, one line per ref:
    <local ref> <local sha> <remote ref> <remote sha>

The list: `.claude/bookkeeping.json`
-----------------------------------
    {"paths": [".claude/queue.json", "PETE-TODO.md"]}

An entry is an exact repo-relative path, `dir/**` (everything under `dir/`), or
`dir/*.ext` (the files directly in `dir/` ending `.ext`, never nested; `dir` is required, so
a bare `*.md` is invalid). Nothing else: any other wildcard, an absolute path, `..`, a
backslash or an empty entry makes the whole file invalid, and an invalid file is "run the
tests". A repo with no list never skips.

An optional `"except"` list names exact paths that never count as bookkeeping, even when an
entry matches them: `{"paths": ["docs/*.md"], "except": ["docs/roadmap.md"]}`. It takes
exact paths only (no wildcard, no `/**`), and an invalid one invalidates the file. Any key
other than `paths` and `except` does too.

What counts as "changed", per ref line
--------------------------------------
The question is whether the tree being pushed differs from the tree already on the remote
(which CI tested) in anything but bookkeeping, so this diffs trees, not commit file lists.
- delete (local sha all zeros): contributes nothing;
- existing remote ref whose sha is in the local object store: diff remote sha..local sha;
- new remote ref: diff from the merge-base with the remote's default branch
  (`refs/remotes/<remote>/HEAD`, else `<remote>/main`, else `<remote>/master`);
- remote sha not in the local store, or no base to diff from: undecidable, run the tests.
`--no-renames`, so a rename counts both its old and its new path.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LIST_RELATIVE_PATH = Path(".claude") / "bookkeeping.json"

SKIP = 0
RUN_TESTS = 1
FAILED = 2  # the check itself broke; the hook treats it exactly like RUN_TESTS

# How many paths the skip message names before it says "+N more".
MAX_NAMED = 8

_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_WILDCARDS = "*?[]"
# The one wildcard segment allowed besides a trailing `/**`: a star and a plain extension.
_FLAT_NAME = re.compile(r"^\*\.[A-Za-z0-9]{1,10}$")
_LIST_KEYS = {"paths", "except"}


class ConfigError(Exception):
    """`.claude/bookkeeping.json` is present and unusable."""


class Undecidable(Exception):
    """The pushed range cannot be settled here, so the tests must run."""


@dataclass(frozen=True)
class Entry:
    """One list entry: an exact `path`; when `is_dir`, everything under it; when `ext` is
    set (`.md`), the files directly in directory `path` that end with it."""

    path: str
    is_dir: bool
    ext: str | None = None

    def matches(self, changed: str) -> bool:
        if self.ext is not None:
            parent, _, name = changed.rpartition("/")
            return parent == self.path and name.endswith(self.ext) and len(name) > len(self.ext)
        if self.is_dir:
            return changed.startswith(self.path + "/")
        return changed == self.path

    def __str__(self) -> str:
        if self.ext is not None:
            return f"{self.path}/*{self.ext}"
        return f"{self.path}/**" if self.is_dir else self.path


class EntryList(list):
    """The list's entries, plus the exact paths its `except` list takes back out."""

    excepted: frozenset[str] = frozenset()


# ---- the list ------------------------------------------------------------------------


def parse_entry(raw: object) -> Entry:
    """One list entry, or ConfigError saying why it is not in the grammar."""
    if not isinstance(raw, str):
        raise ConfigError(f"entry {raw!r} is not a string")
    if not raw or raw != raw.strip():
        raise ConfigError(f"entry {raw!r} is empty or has leading or trailing whitespace")
    if "\\" in raw or any(ord(ch) < 32 for ch in raw):
        raise ConfigError(f"entry {raw!r} has a backslash or a control character")
    if raw.startswith("/") or _DRIVE.match(raw):
        raise ConfigError(f"entry {raw!r} is absolute; paths are repo-relative")

    is_dir = raw.endswith("/**")
    path = raw[: -len("/**")] if is_dir else raw
    ext = None
    parent, _, last = raw.rpartition("/")
    if not is_dir and parent and _FLAT_NAME.match(last):
        path, ext = parent, last[1:]
    if not path:
        raise ConfigError(f"entry {raw!r} names no directory")
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise ConfigError(f"entry {raw!r} has an empty, '.' or '..' path segment")
        if any(ch in segment for ch in _WILDCARDS):
            raise ConfigError(
                f"entry {raw!r} has a wildcard; only a trailing '/**' or a final '*.ext' is allowed"
            )
    return Entry(path=path, is_dir=is_dir, ext=ext)


def parse_except(raw: object) -> str:
    """One `except` path: an exact entry, nothing wider."""
    entry = parse_entry(raw)
    if entry.is_dir or entry.ext is not None:
        raise ConfigError(f"except entry {raw!r} must be an exact path, not a wildcard")
    return entry.path


def parse_list(text: str) -> EntryList:
    """The entries in the text of a bookkeeping file, or ConfigError."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"not valid JSON: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("paths"), list):
        raise ConfigError('expected an object with a "paths" list')
    unknown = sorted(set(data) - _LIST_KEYS)
    if unknown:
        raise ConfigError(f"unknown key {unknown[0]!r}; only 'paths' and 'except' are allowed")
    excepted = data.get("except", [])
    if not isinstance(excepted, list):
        raise ConfigError('"except" must be a list of exact paths')
    entries = EntryList(parse_entry(raw) for raw in data["paths"])
    entries.excepted = frozenset(parse_except(raw) for raw in excepted)
    return entries


def load_list(root: Path) -> EntryList | None:
    """The repo's entries, or None when it has no list (no skipping, ever)."""
    path = root / LIST_RELATIVE_PATH
    if not path.is_file():
        return None
    try:
        return parse_list(path.read_text(encoding="utf-8"))
    except ConfigError as error:
        raise ConfigError(f"{LIST_RELATIVE_PATH.as_posix()}: {error}") from error


def is_bookkeeping(changed: str, entries: EntryList) -> bool:
    if changed in entries.excepted:
        return False
    return any(entry.matches(changed) for entry in entries)


# ---- the pushed range ----------------------------------------------------------------


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise Undecidable(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def has_commit(root: Path, sha: str) -> bool:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=root,
        capture_output=True,
    )
    return probe.returncode == 0


def resolve_base(root: Path, remote: str) -> str:
    """The remote's default branch tip, as far as this clone knows it."""
    for ref in (
        f"refs/remotes/{remote}/HEAD",
        f"refs/remotes/{remote}/main",
        f"refs/remotes/{remote}/master",
    ):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return probe.stdout.strip()
    raise Undecidable(f"no default branch for remote {remote!r} to diff a new ref against")


def diff_paths(root: Path, older: str, newer: str) -> set[str]:
    out = run_git(root, "diff", "--name-only", "--no-renames", "-z", older, newer)
    return {path for path in out.split("\0") if path}


def paths_for_line(root: Path, remote: str, line: str) -> set[str]:
    """Paths one ref line puts on the remote, or Undecidable."""
    fields = line.split()
    if len(fields) != 4:
        raise Undecidable(f"unreadable ref line: {line!r}")
    _local_ref, local_sha, _remote_ref, remote_sha = fields
    for sha in (local_sha, remote_sha):
        if not _SHA.match(sha):
            raise Undecidable(f"not a commit id: {sha!r}")

    if set(local_sha) == {"0"}:
        return set()  # a delete puts no code anywhere
    if not has_commit(root, local_sha):
        raise Undecidable(f"pushed commit {local_sha[:12]} is not in this clone")
    if set(remote_sha) == {"0"}:
        base = run_git(root, "merge-base", local_sha, resolve_base(root, remote)).strip()
        return diff_paths(root, base, local_sha)
    if not has_commit(root, remote_sha):
        raise Undecidable(f"the remote's commit {remote_sha[:12]} is not in this clone")
    return diff_paths(root, remote_sha, local_sha)


def changed_paths(root: Path, remote: str, stdin_text: str) -> set[str] | None:
    """Union of paths over every ref line; None when there was no line to judge."""
    lines = [line for line in stdin_text.splitlines() if line.strip()]
    if not lines:
        return None
    changed: set[str] = set()
    for line in lines:
        changed |= paths_for_line(root, remote, line)
    return changed


# ---- decision ------------------------------------------------------------------------


def describe(paths: list[str]) -> str:
    named = ", ".join(paths[:MAX_NAMED])
    extra = len(paths) - MAX_NAMED
    return f"{named}, +{extra} more" if extra > 0 else named


def decide(root: Path, remote: str, stdin_text: str) -> tuple[int, str]:
    """(exit code, message). The message goes to stdout on SKIP, stderr otherwise."""
    entries = load_list(root)
    if entries is None:
        return RUN_TESTS, f"no {LIST_RELATIVE_PATH.as_posix()}, so every push runs the tests"
    try:
        changed = changed_paths(root, remote, stdin_text)
    except Undecidable as reason:
        return RUN_TESTS, f"cannot settle the pushed range ({reason}); running the tests"
    if changed is None:
        return RUN_TESTS, "no push on stdin to judge; running the tests"
    if not changed:
        return SKIP, "no files change on the remote"
    others = sorted(path for path in changed if not is_bookkeeping(path, entries))
    if others:
        return RUN_TESTS, f"not bookkeeping: {describe(others)}"
    return SKIP, describe(sorted(changed))


def find_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else Path.cwd()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_push_scope.py <remote name> [<remote url>] < stdin", file=sys.stderr)
        return FAILED
    try:
        code, message = decide(find_root(), argv[1], sys.stdin.read())
    except ConfigError as error:
        print(f"check_push_scope: {error}", file=sys.stderr)
        return FAILED
    except Exception as error:  # noqa: BLE001 - any crash must mean "run the tests"
        print(f"check_push_scope: unexpected {type(error).__name__}: {error}", file=sys.stderr)
        return FAILED
    print(message, file=sys.stdout if code == SKIP else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
