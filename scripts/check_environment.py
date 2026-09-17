#!/usr/bin/env python3
"""Fail a push when the local checkout does not match what CI will see, or when tracked
Python reads or writes text without saying which encoding it means.

Why this exists
---------------
Two defects that both present as something else, measured on this setup rather than
assumed:

1. Line endings. On Windows `core.autocrlf=true` is commonly set in the *system*
   gitconfig, so it applies to every repo on the machine and appears in no repo's own
   config. Without a `.gitattributes` catch-all, git checks text files out as CRLF while
   the index keeps LF. `check_context_budget.py` measures bytes on disk, which is
   correct, so the local gate reads one byte per line more than CI reads from the blob:
   the same committed `max_bytes` then means two different things on the two sides of
   the gate. Measured on web-auditor as a ~400-byte gap on a 398-line CLAUDE.md, and in
   the process repo as 26 of 36 tracked files drifting by +8,115 bytes in total. The
   same conversion silently breaks a shell script or a git hook on Linux.

2. Text encoding. `locale.getpreferredencoding(False)` is `cp1252` on this machine, so
   `open(path, "w")` with no `encoding=` raises UnicodeEncodeError on an em-dash - and
   leaves a zero-byte file behind. Observed here: the crash was then missed by a
   read-back check that compared the truncated output against a source that had failed
   to write for the same reason, found both empty, and reported a match. A write that
   produced nothing, confirmed correct by a verification that could not fail.

What this checks, and what it deliberately does not
---------------------------------------------------
It tests the symptom, not the cause. Asserting `core.autocrlf false` would be wrong in
both directions: a repo with `autocrlf=true` and correct attributes is fine, and one
with `autocrlf=false` and `eol=crlf` attributes is not. So it compares the index against
the working tree and reports the actual drift.

Usage
-----
    python scripts/check_environment.py             # enforce; exit 1 on a real problem
    python scripts/check_environment.py --report    # print findings, always exit 0
    python scripts/check_environment.py --fix       # rewrite drifting files from index
    python scripts/check_environment.py --selftest  # prove the detectors can fail

One symptom, two causes, and they need opposite advice. If a drifting file carries no
`eol=` attribute, `.gitattributes` does not cover it and the rule is what is missing. If
it does carry one, the rule is already right and the file was written into the working
tree before git could read it: git takes `.gitattributes` from the working tree rather
than the index, so anything written during that window falls back to `core.autocrlf`.
A Claude Code worktree hits this - it seeds `.gitignore` and `.claude/` ahead of the
full checkout, and those are the paths that drift. The second checkout does not undo it,
because CRLF clean-filters back to the same blob, so `git status` reports nothing while
the bytes on disk differ. `git config core.autocrlf false` closes the window; `--fix`
repairs what was already written.

Outside a git repository it prints a message and exits 0, so this is safe to wire into a
shared template before every project has adopted it.

All output is deliberately ASCII. This runs from a git hook and from CI, and on a Windows
console using a legacy code page a stray em-dash raises UnicodeEncodeError - which would
turn a clear failure into an unrelated crash.
"""

from __future__ import annotations

import ast
import locale
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# Values git reports for a file whose line endings are not meaningful: binary content,
# no newline at all, or explicitly marked `-text`. Comparing index against working tree
# for these says nothing.
EOL_NOT_APPLICABLE = {"none", "-text", ""}

# Modes that write. A mode containing "b" is binary and needs no encoding.
WRITING_MODE_CHARS = ("w", "a", "x", "+")


class GitUnavailable(Exception):
    """Not a git repository, or git is not on PATH."""


class Finding(NamedTuple):
    """One problem, with enough detail to fix it without re-running anything."""

    path: str
    line: int | None  # None for whole-file findings such as line-ending drift
    detail: str


class EolRecord(NamedTuple):
    """One line of `git ls-files --eol`: what the index holds, what the working tree
    holds, and which attributes git applied when it decided.

    The attribute text is kept because it separates the two causes behind one symptom.
    A drifting file carrying no `eol=` attribute means `.gitattributes` does not cover
    it, and the fix is to add the rule. A drifting file that *does* carry `eol=lf` means
    the rule is already right and the file was written before git could read it - the
    worktree bootstrap window described in WORKTREE_EOL_ADVICE. The two need opposite
    advice, and telling someone to add a rule that is already there sends them looking
    in the wrong file.
    """

    index_eol: str
    worktree_eol: str
    attrs: str
    path: str


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def run_git(args: list[str], cwd: Path | None = None, stdin: bytes | None = None) -> str:
    """Run a git command and return stdout, or raise GitUnavailable.

    Decodes as UTF-8 explicitly rather than letting Python pick the locale codec - the
    whole point of this script is that the locale codec is not what anyone meant.

    `stdin` is bytes rather than str because the one caller that uses it feeds a
    NUL-separated pathspec list, where encoding the separator through a text codec is
    the sort of detail that goes wrong quietly.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            input=stdin,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise GitUnavailable("git is not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        raise GitUnavailable(f"git {' '.join(args)} failed: {stderr}") from exc

    return completed.stdout.decode("utf-8", errors="replace")


def find_repo_root() -> Path:
    """The top level of the repository containing the current directory."""
    return Path(run_git(["rev-parse", "--show-toplevel"]).strip())


# ---------------------------------------------------------------------------
# Check 1: line-ending drift between the index and the working tree
# ---------------------------------------------------------------------------


def parse_eol_records(raw: str) -> list[EolRecord]:
    """Parse `git ls-files --eol -z` output into EolRecords.

    Each NUL-separated record looks like:

        i/lf    w/crlf  attr/text=auto eol=lf <TAB>PROCESS.md

    The attribute field can contain spaces, so the path is taken from the last tab
    rather than by splitting on whitespace, and the attributes are taken as everything
    after `attr/` rather than as a single whitespace-delimited field. Records that do
    not parse are skipped rather than guessed at.
    """
    records: list[EolRecord] = []

    for chunk in raw.split("\0"):
        if not chunk.strip():
            continue

        info, tab, path = chunk.rpartition("\t")
        if not tab or not path:
            continue

        index_eol = worktree_eol = ""
        for field in info.split():
            if field.startswith("i/"):
                index_eol = field[2:]
            elif field.startswith("w/"):
                worktree_eol = field[2:]

        if not index_eol and not worktree_eol:
            continue

        _, marker, attrs = info.partition("attr/")
        records.append(EolRecord(
            index_eol=index_eol,
            worktree_eol=worktree_eol,
            attrs=attrs.strip() if marker else "",
            path=path,
        ))

    return records


def drifting_paths(records: list[EolRecord]) -> list[EolRecord]:
    """Records where the working tree does not agree with the index.

    Two distinct problems, both reported:
      - index and working tree disagree (the CRLF-on-checkout case);
      - the working tree is `mixed`, which is wrong regardless of what the index holds.
    """
    drifting = []

    for record in records:
        if record.worktree_eol == "mixed":
            drifting.append(record)
            continue

        if (
            record.index_eol in EOL_NOT_APPLICABLE
            or record.worktree_eol in EOL_NOT_APPLICABLE
        ):
            continue

        if record.index_eol != record.worktree_eol:
            drifting.append(record)

    return drifting


def attributes_already_cover(drifting: list[EolRecord]) -> bool:
    """True when every drifting file already carries an explicit `eol=` attribute.

    That is the worktree bootstrap case: `.gitattributes` says the right thing, and the
    file drifted anyway because it was written into the working tree before git could
    read the rule. False - including for an empty list, which no caller asks about -
    means at least one file is not covered, so adding the rule is still the first fix.
    """
    return bool(drifting) and all("eol=" in record.attrs for record in drifting)


def blob_sizes(repo_root: Path, paths: list[str]) -> dict[str, int]:
    """Byte size of each path's staged blob, in one `git cat-file --batch-check` call.

    One subprocess for the whole set rather than one per file, because a large repo can
    have thousands of tracked files and this runs on every push. Paths whose size cannot
    be determined are omitted; the caller treats a missing entry as "size unknown"
    rather than as zero.
    """
    # --batch-check reads one newline-terminated request per line and has no NUL-input
    # mode, so a path containing a newline cannot be asked about without desynchronising
    # every later request from its answer. Such paths are legal in git and vanishingly
    # rare; drop them rather than mis-attribute a size to the wrong file.
    paths = [path for path in paths if "\n" not in path]
    if not paths:
        return {}

    stdin = "".join(f":{path}\n" for path in paths).encode("utf-8")

    try:
        completed = subprocess.run(
            ["git", "cat-file", "--batch-check"],
            cwd=str(repo_root),
            input=stdin,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {}

    sizes: dict[str, int] = {}
    output = completed.stdout.decode("utf-8", errors="replace").splitlines()

    for path, line in zip(paths, output, strict=True):
        parts = line.split()
        # "<sha> blob <size>"; anything else (e.g. "<name> missing") has no size.
        if len(parts) == 3 and parts[1] == "blob" and parts[2].isdigit():
            sizes[path] = int(parts[2])

    return sizes


class EolResult(NamedTuple):
    """What the line-ending check found.

    `drifting` is carried alongside the findings because --fix needs the paths, and
    re-deriving them from the formatted findings would mean parsing text this script
    just finished printing.
    """

    findings: list[Finding]
    total_drift: int | None
    drifting: list[EolRecord]


def check_line_endings(repo_root: Path) -> EolResult:
    """Findings for line-ending drift, plus the total byte drift across them.

    The drift total is None when no blob size could be read at all, so the caller can
    stay silent about it rather than print a confident "+0 bytes" next to a list of
    files that plainly do differ.
    """
    records = parse_eol_records(run_git(["ls-files", "--eol", "-z"], cwd=repo_root))
    drifting = drifting_paths(records)

    if not drifting:
        return EolResult([], 0, [])

    paths = [record.path for record in drifting]
    sizes = blob_sizes(repo_root, paths)

    findings: list[Finding] = []
    total_drift = 0
    sized = 0

    for record in drifting:
        blob_size = sizes.get(record.path)

        try:
            worktree_size = (repo_root / record.path).stat().st_size
        except OSError:
            worktree_size = None

        if blob_size is not None and worktree_size is not None:
            delta = worktree_size - blob_size
            total_drift += delta
            sized += 1
            size_note = f", {delta:+,} bytes vs the index"
        else:
            size_note = ""

        findings.append(Finding(
            path=record.path,
            line=None,
            detail=(
                f"index is {record.index_eol}, working tree is {record.worktree_eol}"
                f"{size_note}"
            ),
        ))

    return EolResult(findings, (total_drift if sized else None), drifting)


# ---------------------------------------------------------------------------
# Check 2: text I/O with no explicit encoding
# ---------------------------------------------------------------------------


def _string_constant(node: ast.AST | None) -> str | None:
    """The value of a literal string node, or None if it is not a literal string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _has_star_kwargs(call: ast.Call) -> bool:
    """True if the call forwards **kwargs, so its arguments cannot be read statically."""
    return any(keyword.arg is None for keyword in call.keywords)


def _encoding_is_explicit(call: ast.Call) -> bool:
    """True if `encoding=` is passed with something other than a literal None.

    `encoding=None` is spelled out here as unsatisfied: it is the locale default written
    down, which is exactly the defect, not an exemption from it.
    """
    for keyword in call.keywords:
        if keyword.arg != "encoding":
            continue
        if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
            return False
        return True
    return False


def _open_mode(call: ast.Call) -> tuple[str, bool]:
    """Return (mode, known) for a call to open().

    `known` is False when the mode is computed rather than literal. An unknown mode is
    never reported: a false positive here blocks a push, and a rule that cries wolf gets
    turned off, which costs more than the case it missed.
    """
    if len(call.args) >= 2:
        mode = _string_constant(call.args[1])
        return (mode, True) if mode is not None else ("", False)

    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = _string_constant(keyword.value)
            return (mode, True) if mode is not None else ("", False)

    # open() with no mode argument defaults to "r" - a text read, which needs an
    # encoding just as much as a write does.
    return "r", True


def scan_source_for_encoding(source: str, label: str) -> list[Finding]:
    """Findings for text I/O with no explicit encoding in one Python source string.

    Pure: takes text, returns findings, touches nothing. That is what lets --selftest
    prove the detector fires on a known-positive and stays silent on a known-negative
    without writing fixture files.

    Deliberately limited to bare `open(...)` and `.write_text(...)` / `.read_text(...)`.
    Attribute calls named `open` - `path.open("w")`, `zf.open(name, "w")` - are not
    flagged: the receiver's type is not knowable from the syntax tree, and zipfile's
    `open` takes no encoding at all, so flagging it would be a false positive that
    blocks a push. A stated gap is better than an unreliable check.
    """
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError as exc:
        return [Finding(label, exc.lineno, f"could not be parsed: {exc.msg}")]

    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _has_star_kwargs(node):
            continue

        if isinstance(node.func, ast.Name) and node.func.id == "open":
            mode, known = _open_mode(node)
            if not known or "b" in mode:
                continue
            if _encoding_is_explicit(node):
                continue

            writing = any(char in mode for char in WRITING_MODE_CHARS)
            findings.append(Finding(
                path=label,
                line=node.lineno,
                detail=(
                    f"open(..., {mode!r}) {'writes' if writing else 'reads'} text with "
                    "no encoding=, so it uses the locale codec"
                ),
            ))
            continue

        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "write_text",
            "read_text",
        }:
            if _encoding_is_explicit(node):
                continue
            findings.append(Finding(
                path=label,
                line=node.lineno,
                detail=(
                    f".{node.func.attr}(...) with no encoding=, so it uses the locale "
                    "codec"
                ),
            ))

    return findings


def check_encoding_calls(repo_root: Path) -> list[Finding]:
    """Scan every tracked Python file for text I/O with no explicit encoding."""
    raw = run_git(["ls-files", "-z", "--", "*.py"], cwd=repo_root)
    paths = [chunk for chunk in raw.split("\0") if chunk.strip()]

    findings: list[Finding] = []

    for path in paths:
        try:
            source = (repo_root / path).read_bytes().decode("utf-8")
        except OSError:
            # Tracked but not present in the working tree; the line-ending check
            # already covers whether the checkout is intact.
            continue
        except UnicodeDecodeError:
            findings.append(Finding(path, None, "is not valid UTF-8"))
            continue

        findings.extend(scan_source_for_encoding(source, path))

    # ast.walk is breadth-first, so findings come out in tree order rather than line
    # order. Sort them: this is read by a person fixing the file top to bottom.
    return sorted(findings, key=lambda finding: (finding.path, finding.line or 0))


# ---------------------------------------------------------------------------
# Check 3: the interpreter's own default (advisory)
# ---------------------------------------------------------------------------


def interpreter_encoding_note() -> str | None:
    """A warning if Python's default text encoding is not UTF-8, else None.

    Advisory rather than fatal: this is machine configuration, not repo state, so it is
    not fixable in the commit being pushed and CI does not share it. It is printed
    because it explains why check 2 matters on this machine specifically.
    """
    preferred = locale.getpreferredencoding(False)

    if preferred.lower().replace("-", "") == "utf8":
        return None

    return (
        f"Python's default text encoding here is {preferred}, not UTF-8. Any open() "
        "without encoding= will use it. Set PYTHONUTF8=1 to change the default; the "
        "explicit encoding= in the code is what actually fixes it."
    )


# ---------------------------------------------------------------------------
# Repair: rewrite drifting files from the index
# ---------------------------------------------------------------------------


def parse_porcelain_paths(raw: str) -> set[str]:
    """Parse `git status --porcelain -z` output into the set of changed paths.

    Porcelain v1 with -z: "XY <path>" NUL-terminated, and for a rename an extra
    NUL-terminated origin path follows. Reading only the entries that start with a
    two-character status field and a space skips those origin paths. Pulled out as its
    own function so --selftest can prove it against known output without a real git
    repo: this is the decision that keeps --fix from destroying an uncommitted edit,
    so it is the one piece of the repair path worth pinning down independently of
    delete-and-checkout, which needs a live repo to exercise at all.
    """
    changed: set[str] = set()
    for entry in raw.split("\0"):
        if len(entry) > 3 and entry[2] == " ":
            changed.add(entry[3:])

    return changed


def paths_with_local_changes(repo_root: Path, paths: list[str]) -> set[str]:
    """Of `paths`, the ones git reports as modified or staged.

    Repairing a file means deleting it and checking it back out, which destroys any
    uncommitted edit it holds. Line-ending drift on its own does not appear here - a
    CRLF working copy clean-filters back to the same LF blob, so git calls it
    unmodified - and that is what makes this usable as a guard: anything git *does*
    report has real content changes and must be left alone.
    """
    if not paths:
        return set()

    raw = run_git(
        ["status", "--porcelain", "-z", "--"] + paths,
        cwd=repo_root,
    )

    return parse_porcelain_paths(raw)


def repair_line_endings(
    repo_root: Path,
    drifting: list[EolRecord],
) -> tuple[list[str], list[str]]:
    """Rewrite drifting files from the index. Returns (repaired, skipped).

    Each file is deleted before it is checked out again. `git checkout -- <path>` on its
    own is a no-op here: git already considers a CRLF copy of an LF blob unmodified, so
    it sees nothing to restore. The file has to be missing for git to write it.

    This changes no committed content - the index is already correct - so it is a
    working-tree refresh, not a commit.
    """
    paths = [record.path for record in drifting]
    skipped = sorted(paths_with_local_changes(repo_root, paths))
    repairable = [path for path in paths if path not in set(skipped)]

    if not repairable:
        return [], skipped

    for path in repairable:
        try:
            (repo_root / path).unlink()
        except FileNotFoundError:
            # Already absent; the checkout below writes it either way.
            pass
        except OSError as exc:
            raise GitUnavailable(f"could not remove {path}: {exc}") from exc

    # --pathspec-from-file avoids both the command-line length limit on a large repo and
    # any quoting question about paths with spaces; -file-nul makes the separator
    # unambiguous for paths that contain a newline.
    run_git(
        ["checkout", "--pathspec-from-file=-", "--pathspec-file-nul"],
        cwd=repo_root,
        stdin=b"\0".join(path.encode("utf-8") for path in repairable),
    )

    return repairable, skipped


# ---------------------------------------------------------------------------
# Self-test: prove the detectors can fail
# ---------------------------------------------------------------------------

# (source, expected number of findings). A detector that has never been shown to fire on
# a known-positive, and to stay silent on a known-negative, has not earned the right to
# have its silence read as a pass.
ENCODING_FIXTURES: list[tuple[str, int]] = [
    # Known-positive: must fire.
    ("open(p, 'w').write(s)", 1),
    ('open(p, "a").write(s)', 1),
    ("open(p, 'x').write(s)", 1),
    ("open(p, mode='w').write(s)", 1),
    ("data = open(p).read()", 1),
    ("data = open(p, 'r').read()", 1),
    ("open(p, 'w', encoding=None).write(s)", 1),
    ("p.write_text(s)", 1),
    ("p.read_text()", 1),
    ("open(a, 'w').write(s)\nopen(b).read()", 2),
    # Known-negative: must stay silent.
    ("open(p, 'wb').write(b)", 0),
    ("open(p, 'rb').read()", 0),
    ("open(p, 'w', encoding='utf-8').write(s)", 0),
    ("open(p, 'r', encoding='utf-8').read()", 0),
    ("open(p, mode='wb').write(b)", 0),
    ("p.write_text(s, encoding='utf-8')", 0),
    ("p.read_text(encoding='utf-8')", 0),
    ("open(p, mode).write(s)", 0),
    ("open(p, **kw).write(s)", 0),
    ("f.write(s)", 0),
    ("zf.open(name, 'w').write(b)", 0),
]

EOL_FIXTURE = (
    "i/lf    w/crlf  attr/text=auto eol=lf \tPROCESS.md\0"
    "i/lf    w/lf    attr/text=auto eol=lf \tscripts/run.sh\0"
    "i/none  w/none  attr/                 \tdocs/logo.png\0"
    "i/lf    w/mixed attr/                 \tnotes/mixed.md\0"
    # Both sides mixed. This is the only case the explicit `mixed` branch catches on its
    # own - every other mixed file is already caught by index != working tree. It is in
    # the fixture because without it, deleting that branch leaves the self-test passing:
    # found by mutation-testing the self-test rather than by reading it.
    "i/mixed w/mixed attr/                 \tlegacy/both-mixed.md\0"
    "i/crlf  w/crlf  attr/text eol=crlf    \twin/only.bat\0"
)

EXPECTED_EOL_RECORDS = 6
EXPECTED_DRIFTING = {"PROCESS.md", "notes/mixed.md", "legacy/both-mixed.md"}

# The attribute text has to survive parsing, because it is what picks between the two
# remedies. Spaces inside it are the reason it is taken as everything after `attr/`
# rather than as one whitespace-delimited field: `text=auto eol=lf` is two fields.
EXPECTED_ATTRS = {
    "PROCESS.md": "text=auto eol=lf",
    "docs/logo.png": "",
    "win/only.bat": "text eol=crlf",
}

# Every drifting file carries eol=, so this is the worktree bootstrap case. Kept
# separate from EOL_FIXTURE, whose drifting set deliberately mixes covered and
# uncovered files: without a fixture that is uniformly covered, a broken
# attributes_already_cover that always returned False would still pass.
#
# win/drift.bat carries eol=crlf, not eol=lf, and is the reason this fixture is not
# uniformly eol=lf: a check narrowed to the literal substring "eol=lf" instead of the
# general "eol=" still passed every other case here and in EOL_FIXTURE, and was only
# caught by mutation-testing this function directly.
COVERED_FIXTURE = (
    "i/lf    w/crlf  attr/text=auto eol=lf \t.gitignore\0"
    "i/lf    w/crlf  attr/text=auto eol=lf \t.claude/settings.json\0"
    "i/lf    w/crlf  attr/text eol=crlf    \twin/drift.bat\0"
    "i/lf    w/lf    attr/text=auto eol=lf \tPROCESS.md\0"
)

# `git status --porcelain -z` output, for parse_porcelain_paths: an unstaged modify, a
# staged modify, a rename (whose second, NUL-terminated entry is the origin path and
# must not be read as a changed path in its own right), and an untracked file.
PORCELAIN_FIXTURE = (
    " M scripts/check_environment.py\0"
    "M  docs/readme.md\0"
    "R  new-name.txt\0old-name.txt\0"
    "?? untracked.txt\0"
)
EXPECTED_PORCELAIN_CHANGED = {
    "scripts/check_environment.py",
    "docs/readme.md",
    "new-name.txt",
    "untracked.txt",
}


def selftest() -> int:
    """Run every detector against known-positive and known-negative cases."""
    failures: list[str] = []

    for source, expected in ENCODING_FIXTURES:
        actual = len(scan_source_for_encoding(source, "<fixture>"))
        if actual != expected:
            failures.append(
                f"encoding detector: expected {expected} finding(s), got {actual}, "
                f"for {source!r}"
            )

    records = parse_eol_records(EOL_FIXTURE)
    if len(records) != EXPECTED_EOL_RECORDS:
        failures.append(
            f"eol parser: expected {EXPECTED_EOL_RECORDS} records, "
            f"parsed {len(records)}"
        )

    drifting = drifting_paths(records)
    actual_paths = {record.path for record in drifting}
    if actual_paths != EXPECTED_DRIFTING:
        failures.append(
            f"eol drift detector: expected {sorted(EXPECTED_DRIFTING)}, "
            f"got {sorted(actual_paths)}"
        )

    by_path = {record.path: record.attrs for record in records}
    for path, expected_attrs in EXPECTED_ATTRS.items():
        actual_attrs = by_path.get(path)
        if actual_attrs != expected_attrs:
            failures.append(
                f"eol attribute parser: expected {expected_attrs!r} for {path}, "
                f"got {actual_attrs!r}"
            )

    # Three distinct answers, one case each: a mixed set is not covered, a uniformly
    # covered set is, and an empty set is not - nothing drifting means there is no
    # remedy to choose between, and True there would print worktree advice for a repo
    # with nothing wrong.
    covered_cases: list[tuple[str, list[EolRecord], bool]] = [
        ("mixed set", drifting, False),
        ("all covered", drifting_paths(parse_eol_records(COVERED_FIXTURE)), True),
        ("nothing drifting", [], False),
    ]
    for label, sample, expected_covered in covered_cases:
        if attributes_already_cover(sample) is not expected_covered:
            failures.append(
                f"eol remedy selector: expected {expected_covered} for {label}, "
                f"got {not expected_covered}"
            )

    changed = parse_porcelain_paths(PORCELAIN_FIXTURE)
    if changed != EXPECTED_PORCELAIN_CHANGED:
        failures.append(
            f"porcelain parser: expected {sorted(EXPECTED_PORCELAIN_CHANGED)}, "
            f"got {sorted(changed)}"
        )

    if failures:
        print("check_environment selftest: FAILED", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    cases = len(ENCODING_FIXTURES) + 2 + len(EXPECTED_ATTRS) + len(covered_cases) + 1
    print(f"check_environment selftest: {cases} detector cases passed.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def format_finding(finding: Finding) -> str:
    """One indented line per finding, with a line number where there is one."""
    location = finding.path if finding.line is None else f"{finding.path}:{finding.line}"
    return f"  {location} - {finding.detail}"


EOL_BREAKAGE = """
  What this breaks: a size-based gate, such as scripts/check_context_budget.py, reads
  the working tree locally and the blob in CI, so the same committed limit means two
  different things on the two sides."""

EOL_ADVICE = EOL_BREAKAGE + """

  At least one file above carries no eol= attribute, so .gitattributes does not cover
  it yet.

  Fix: add `* text=auto eol=lf` to .gitattributes, then refresh the working tree with
  `python scripts/check_environment.py --fix`. The index is already correct, so this
  changes no committed content."""

# The same symptom, the opposite cause, so it gets its own text. Pointing someone at
# .gitattributes when .gitattributes is already right sends them to read the one file
# that has nothing wrong with it.
WORKTREE_EOL_ADVICE = EOL_BREAKAGE + """

  Every file above already carries an eol= attribute, so the .gitattributes rule is not
  what is missing. This is the checkout-order case: something wrote these paths into the
  working tree before .gitattributes was there to be read. Git reads that file from the
  working tree, not from the index, so during such a window the system-wide
  core.autocrlf=true wins and the file lands CRLF. The later full checkout then leaves
  it alone, because CRLF clean-filters back to the same blob - which is also why
  `git status` stays clean while the bytes on disk do not match.

  Measured shape of it: a Claude Code worktree seeds .gitignore and .claude/ ahead of
  the full checkout, and those are exactly the paths that drift.

  Fix, in this order:
    git config core.autocrlf false             # once per clone, stops it recurring
    python scripts/check_environment.py --fix  # rewrites the files already written

  Setting core.autocrlf is not a substitute for the .gitattributes rule and does not
  contradict it: attributes win wherever they apply, so this only changes what happens
  in the window before any have been read."""

ENCODING_ADVICE = """
  What this breaks: the locale codec is cp1252 on a default Windows install. Writing an
  em-dash through it raises UnicodeEncodeError and can leave a zero-byte file; reading a
  UTF-8 file through it corrupts the text silently.

  Fix: pass encoding="utf-8" explicitly, or use a binary mode where the content is not
  text."""


def run_fix(repo_root: Path) -> int:
    """Repair line-ending drift, re-check, and report what is left.

    Line endings only. Repairing an encoding finding means deciding what a file's bytes
    were meant to say, which is a code edit and not something a flag should guess at.
    """
    try:
        before = check_line_endings(repo_root)
    except GitUnavailable as exc:
        print(f"environment check --fix: {exc}", file=sys.stderr)
        return 1

    if not before.drifting:
        print("environment check --fix: no line-ending drift to repair.")
        return 0

    bootstrap_case = attributes_already_cover(before.drifting)

    try:
        repaired, skipped = repair_line_endings(repo_root, before.drifting)
        after = check_line_endings(repo_root)
    except GitUnavailable as exc:
        print(f"environment check --fix: {exc}", file=sys.stderr)
        return 1

    print(f"environment check --fix: repaired {len(repaired)} file(s) from the index.")
    for path in repaired:
        print(f"  {path}")

    if bootstrap_case:
        print(
            "\nnote: every repaired file already carried an eol= attribute, so this "
            "will recur on the next checkout unless the cause is set as well:\n"
            "  git config core.autocrlf false"
        )

    if skipped or after.drifting:
        # Same reason main() flushes here: stdout and stderr are separately buffered, so
        # without this the "what went wrong" half can surface above the "what I did"
        # half in a captured log.
        sys.stdout.flush()

    if skipped:
        print(
            f"\nenvironment check --fix: left {len(skipped)} file(s) alone - they hold "
            "uncommitted changes that a rewrite from the index would destroy. Commit "
            "them, then run --fix again.",
            file=sys.stderr,
        )
        for path in skipped:
            print(f"  {path}", file=sys.stderr)

    if after.drifting:
        print(
            f"\nenvironment check --fix: {len(after.drifting)} file(s) still drift. "
            "Run the check without --fix for the detail.",
            file=sys.stderr,
        )
        return 1

    print("\nenvironment check --fix: the working tree now matches the index.")
    return 0


def main(argv: list[str]) -> int:
    flags = argv[1:]

    if "--selftest" in flags:
        return selftest()

    report_only = "--report" in flags
    fix = "--fix" in flags

    try:
        repo_root = find_repo_root()
    except GitUnavailable as exc:
        print(f"environment check: {exc} Nothing to check.")
        return 0

    if fix:
        return run_fix(repo_root)

    try:
        eol_result = check_line_endings(repo_root)
        eol_findings, total_drift = eol_result.findings, eol_result.total_drift
        encoding_findings = check_encoding_calls(repo_root)
    except GitUnavailable as exc:
        # Fails closed. A check that could not run has not passed, and treating it as a
        # pass is the exact failure mode this script exists to argue against.
        print(f"environment check: {exc}", file=sys.stderr)
        return 0 if report_only else 1

    note = interpreter_encoding_note()

    print("environment check:")
    print(f"  line endings   {len(eol_findings)} file(s) drifting from the index")
    print(f"  text encoding  {len(encoding_findings)} call(s) with no explicit encoding")

    if not eol_findings and not encoding_findings:
        if note:
            print(f"\nnote: {note}")
        print("environment check: clean.")
        return 0

    sys.stdout.flush()
    print("", file=sys.stderr)

    if eol_findings:
        drift_clause = "" if total_drift is None else f", {total_drift:+,} bytes in total"
        print(
            f"environment check: {len(eol_findings)} file(s) differ between the index "
            f"and the working tree{drift_clause}.",
            file=sys.stderr,
        )
        for finding in eol_findings:
            print(format_finding(finding), file=sys.stderr)
        advice = (
            WORKTREE_EOL_ADVICE
            if attributes_already_cover(eol_result.drifting)
            else EOL_ADVICE
        )
        print(advice, file=sys.stderr)

    if encoding_findings:
        print(
            f"\nenvironment check: {len(encoding_findings)} call(s) do text I/O without "
            "saying which encoding.",
            file=sys.stderr,
        )
        for finding in encoding_findings:
            print(format_finding(finding), file=sys.stderr)
        print(ENCODING_ADVICE, file=sys.stderr)

    if note:
        print(f"\nnote: {note}", file=sys.stderr)

    return 0 if report_only else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
