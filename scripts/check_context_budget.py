#!/usr/bin/env python3
"""Fail a push or a CI run when an eagerly-loaded context file exceeds its budget.

Why this exists
---------------
`CLAUDE.md` is loaded into the model's context window before the session does anything,
and re-sent on every turn. It is the only project file with that property, which makes
its size a per-turn tax on every session for as long as the file stays that size.

Left ungoverned it accretes. Measured on web-auditor: 15 KB on 2026-08-01, 190 KB by
2026-09-01, cut to 89 KB by a manual split on 2026-09-04, back to 116 KB two days later.
A cut without a guard bought 72 hours. This is the guard.

The budget is a committed number, so raising it is a visible diff someone has to justify
in a pull request, rather than something that happens a few hundred bytes at a time.

Usage
-----
    python scripts/check_context_budget.py            # enforce; exit 1 if over
    python scripts/check_context_budget.py --report   # print the table, always exit 0
    python scripts/check_context_budget.py --selftest # calibrate the WARN/OVER checks

Configuration lives in `.claude/context-budget.json`. A repo with no config file is
skipped with a message and a zero exit, so this is safe to wire into a shared template
before every project has adopted it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG_RELATIVE_PATH = Path(".claude") / "context-budget.json"

# Rough bytes-per-token for English prose and Markdown. Only ever used to print a
# human-facing estimate alongside the real number; nothing branches on it.
APPROX_BYTES_PER_TOKEN = 4

# Fraction of max_bytes at which a file moves from "ok" to "WARN" -- a nudge before the
# hard cap, not a second cap. Illustrative in the queue note that raised this, not a
# request for per-budget tuning, and .claude/context-budget.json has exactly one entry
# today -- a plain constant is trivially raised later if that ever changes.
WARN_RATIO = 0.9


class ConfigError(Exception):
    """The budget config is missing required fields or holds an unusable value."""


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` to the directory containing `.git`.

    Falls back to `start` if there is no `.git` anywhere above it, so the script still
    does something sensible when run from an exported copy of the template.
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    return start


def load_budgets(config_path: Path) -> list[dict]:
    """Read and validate the budget config.

    Returns a list of validated entries. Raises ConfigError with a message naming the
    offending entry, because a config this small is not worth a schema library and a
    silent skip on a malformed budget would defeat the point of having one.
    """
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{config_path} could not be read: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must hold a JSON object at the top level.")

    budgets = raw.get("budgets")
    if not isinstance(budgets, list) or not budgets:
        raise ConfigError(
            f"{config_path} must contain a non-empty 'budgets' array. "
            "Delete the file rather than leaving it empty if the project opts out."
        )

    validated: list[dict] = []
    for index, entry in enumerate(budgets):
        where = f"{config_path} budgets[{index}]"

        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be an object.")

        path_value = entry.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ConfigError(f"{where} needs a non-empty string 'path'.")

        # Budgets address files inside this repo. An absolute path or a `..` segment
        # would let a config point somewhere it has no business checking, and would
        # quietly pass on a machine where that path happens not to exist.
        candidate = Path(path_value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError(
                f"{where} path must be relative to the repo root and must not "
                f"contain '..' (got {path_value!r})."
            )

        max_bytes = entry.get("max_bytes")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ConfigError(f"{where} needs a positive integer 'max_bytes'.")

        validated.append({
            "path": candidate,
            "max_bytes": max_bytes,
            "note": entry.get("note", ""),
        })

    return validated


def classify_verdict(actual_bytes: int, max_bytes: int) -> str:
    """"ok" / "WARN" / "OVER" for one file's size against its budget.

    WARN starts as soon as actual passes WARN_RATIO of max_bytes, and deliberately
    includes a file sitting exactly at max_bytes (100%, not yet over) -- that boundary
    used to read as "ok", silently, which is the gap this function closes. Only actually
    exceeding the cap is OVER.
    """
    if actual_bytes > max_bytes:
        return "OVER"
    if actual_bytes > max_bytes * WARN_RATIO:
        return "WARN"
    return "ok"


def measure(repo_root: Path, entry: dict) -> dict:
    """Measure one budgeted file. Missing files are an error, not a skip.

    Size is taken from the bytes on disk rather than from decoded text. On Windows a
    CRLF file read as text collapses its line endings, so a decoded length understates
    the real file by one byte per line - and a check that quietly under-reports is worse
    than no check.
    """
    target = repo_root / entry["path"]
    result = {
        "path": entry["path"],
        "max_bytes": entry["max_bytes"],
        "note": entry["note"],
        "missing": False,
        "actual_bytes": 0,
        "verdict": None,
    }

    if not target.is_file():
        result["missing"] = True
        return result

    result["actual_bytes"] = os.path.getsize(target)
    result["verdict"] = classify_verdict(result["actual_bytes"], result["max_bytes"])
    return result


def format_row(result: dict) -> str:
    """One aligned line per budgeted file."""
    name = str(result["path"])

    if result["missing"]:
        return f"  {name:<28} MISSING - budgeted but not present in the repo"

    actual = result["actual_bytes"]
    limit = result["max_bytes"]
    percent = (actual / limit) * 100
    tokens = actual // APPROX_BYTES_PER_TOKEN

    return (
        f"  {name:<28} {actual:>8,} / {limit:>8,} bytes "
        f"({percent:5.1f}%, ~{tokens:,} tokens)  {result['verdict']}"
    )


def format_warning(result: dict) -> str:
    """One ASCII-only line nudging a file that has passed WARN_RATIO but isn't OVER yet.

    ASCII-only, hyphens not em-dashes, for the reason the OVER message below already is:
    this runs from a Windows console on a legacy code page, and a stray non-ASCII
    character has been observed there to turn a clear result into an unrelated
    UnicodeEncodeError instead.
    """
    actual = result["actual_bytes"]
    limit = result["max_bytes"]
    percent = (actual / limit) * 100
    return (
        f"context budget: {result['path']} is at {percent:.1f}% of budget "
        f"({actual:,} / {limit:,} bytes) - approaching the cap. Move record content "
        "into docs/doctrine/ before it goes over."
    )


# -------------------------------------------------------------------- self-test


# Each case: (name, actual_bytes, max_bytes, expected_verdict). The exactly-at-cap case
# is the one this unit actually changes -- it used to read "ok"; the empty-file and
# 1-byte-budget cases are there so the `* WARN_RATIO` float math is exercised, not just
# assumed to behave, at both ends of the range.
SELFTEST_CASES: list[tuple[str, int, int, str]] = [
    ("well within budget", 1_000, 10_000, "ok"),
    ("just under the warn line", 8_999, 10_000, "ok"),
    ("just over the warn line", 9_001, 10_000, "WARN"),
    ("sitting exactly at max_bytes", 10_000, 10_000, "WARN"),
    ("one byte over max_bytes", 10_001, 10_000, "OVER"),
    ("empty file", 0, 10_000, "ok"),
    ("1-byte budget, empty file", 0, 1, "ok"),
    ("1-byte budget, at the cap", 1, 1, "WARN"),
]


def evaluate_cases(classifier) -> list[str]:
    """Run every case through `classifier`, returning one message per case that
    disagreed with its expected verdict.

    Taking the classifier as an argument, rather than calling classify_verdict directly,
    is what makes the control below possible without mutating anything global.
    """
    failures: list[str] = []
    for name, actual_bytes, max_bytes, expected in SELFTEST_CASES:
        got = classifier(actual_bytes, max_bytes)
        if got != expected:
            failures.append(f"  {name}: expected {expected}, got {got}")
    return failures


def run_selftest() -> int:
    """Calibrate classify_verdict. Exit 0 only if every case lands the way it must."""
    failures = evaluate_cases(classify_verdict)

    # The control. Without it, eight green rows are equally consistent with a classifier
    # that has silently stopped distinguishing WARN/OVER from ok -- this script's own
    # failure mode, so it must not go unmeasured. Neuter the classifier; the WARN/OVER
    # cases above must then go red.
    neutered_failures = evaluate_cases(lambda actual_bytes, max_bytes: "ok")
    if not neutered_failures:
        failures.append(
            "  CONTROL: the classifier was disabled and every case still passed -- "
            "these cases are not actually measuring anything"
        )

    if failures:
        print("check_context_budget selftest FAILED:")
        print("\n".join(failures))
        return 1
    print(
        f"check_context_budget selftest: {len(SELFTEST_CASES)} cases passed; "
        f"control turned {len(neutered_failures)} of them red."
    )
    return 0


# ------------------------------------------------------------------ entry point


def main(argv: list[str]) -> int:
    arguments = argv[1:]
    if "--selftest" in arguments:
        return run_selftest()
    report_only = "--report" in arguments

    repo_root = find_repo_root(Path.cwd().resolve())
    config_path = repo_root / CONFIG_RELATIVE_PATH

    if not config_path.is_file():
        print(
            f"context budget: no {CONFIG_RELATIVE_PATH.as_posix()} in {repo_root} "
            "- nothing to check."
        )
        return 0

    try:
        budgets = load_budgets(config_path)
    except ConfigError as exc:
        print(f"context budget: {exc}", file=sys.stderr)
        # A broken config fails closed. The whole point is that the number is governed;
        # treating an unreadable config as "no budget" would hand back the loophole.
        return 0 if report_only else 1

    results = [measure(repo_root, entry) for entry in budgets]

    print("context budget:")
    for result in results:
        print(format_row(result))

    missing = [r for r in results if r["missing"]]
    over = [r for r in results if not r["missing"] and r["verdict"] == "OVER"]
    warn = [r for r in results if not r["missing"] and r["verdict"] == "WARN"]

    # Printed in both modes, right after the table and ahead of the pass/fail branch --
    # a warning is informational, not a verdict, so it doesn't wait on --report and
    # doesn't get skipped by an early return below.
    for result in warn:
        print(format_warning(result))

    if not missing and not over:
        print("context budget: within budget.")
        return 0

    if report_only:
        return 0

    # Flush before switching streams, so the table above still reads in order when both
    # streams land in one terminal or one CI log.
    sys.stdout.flush()
    print("", file=sys.stderr)

    for result in missing:
        print(
            f"context budget: {result['path']} is budgeted but does not exist. "
            "If it was renamed or removed, update "
            f"{CONFIG_RELATIVE_PATH.as_posix()} in the same commit.",
            file=sys.stderr,
        )

    for result in over:
        excess = result["actual_bytes"] - result["max_bytes"]
        print(
            f"context budget: {result['path']} is {excess:,} bytes over budget "
            f"({result['actual_bytes']:,} > {result['max_bytes']:,}).",
            file=sys.stderr,
        )
        if result["note"]:
            print(f"  {result['note']}", file=sys.stderr)

    if over:
        # Deliberately ASCII. This runs from a git hook and from CI, and on a Windows
        # console using a legacy code page a stray em-dash raises UnicodeEncodeError --
        # which would turn a clear budget failure into an unrelated crash. Observed on
        # this setup during the change that added the check.
        print(
            "\nMove the excess into a lazily-loaded file - docs/doctrine/<topic>.md, or a\n"
            "skill - and link to it. Only rules that must hold in every session belong in\n"
            "an eagerly-loaded file.\n"
            "\n"
            "Raising the budget is a deliberate act: edit max_bytes in\n"
            f"{CONFIG_RELATIVE_PATH.as_posix()} and say why in the commit message. The\n"
            "number is committed so that the increase shows up in a diff.",
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
