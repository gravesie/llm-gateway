#!/usr/bin/env python3
"""Stop hook: once a session's context passes a size, block once and ask for a hand-off.

HS3 (docs/hs3-context-size-warning-plan-V0-1.md). The heaviest-sessions analysis
(eval/heaviest-sessions-2026-10-19/README.md section 3) found the sessions past 300k
context holding about 70% of a week's fresh tokens, because every turn re-reads all of it.
Pete chose a global end-of-turn hook: at the threshold it blocks the turn once and tells
Claude to finish the step it is on, write the hand-off and give Pete the opening message
for a fresh session. Pete sees a one-line note on every turn at or past the threshold, and
nothing below it.

LOCAL: THE PLUGIN. The master copy and its self-test live in dev-process; the hook ships in
the `dev-process` plugin (plugin/hooks/hooks.json registers it under `Stop`), so a merge with
a plugin version bump is the release. Record: docs/doctrine/claude-config-under-git.md.

CLOUD: CARRIED. The plugin does not load in a cloud session (CC11b), so since CC11c this file
is also registered verbatim in .claude/distribution.json (target `.claude/hooks/`), and the
carried stop_gate.py runs it when `CLAUDE_CODE_REMOTE` is `true`. It is not registered in any
adopter's settings.json. Record: docs/doctrine/cloud-sessions.md.

CONTEXT SIZE is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` of the
LAST main-thread assistant entry in the transcript -- the formula of
eval/heaviest-sessions-2026-10-19/loop_split.py. The last, not the peak: after /compact the
context really is smaller. Subagent entries (`isSidechain: true`) are skipped; a subagent's
context is not the session's. An entry whose three fields sum to zero is skipped too: the
app writes synthetic assistant entries (an interrupt, an API error) with all-zero usage, and
reading one as "context is 0" would delete the marker and re-arm the block mid-session.
Only the tail of the file is read (TAIL_CHUNK_BYTES, doubling up to the whole file when no
usage line is found), so a 20 MB transcript costs milliseconds.

BLOCKS ONCE PER CROSSING. Crossing the threshold claims a marker file per session_id with
`O_CREAT | O_EXCL`, the first_prompt_time.py pattern; only the turn that creates it blocks.
A turn back below the threshold deletes it, so a session that compacts and grows again is
told again. The hook never blocks without a recorded claim, so it cannot block twice for one
crossing and never approaches the harness's consecutive-block cap (8,
docs/doctrine/harness-limits.md).

`stop_hook_active` IS IGNORED, deliberately, unlike stop_gate.py. If the turn gate blocked
the same turn, the next stop carries that flag, and honouring it would skip a real first
crossing. The marker is this hook's own once-only guard.

FAILS OPEN, VISIBLY. No transcript, no usage line, a bad argument, an unusable session_id, a
marker that cannot be written, or any exception: a `systemMessage` saying so and "Not
blocking", exit 0. Always exit 0 -- an exit 2 from a Stop hook blocks the turn, and a crash
must never do that.

    python context_size_warning.py --threshold 300000 < stop-payload.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

DEFAULT_THRESHOLD = 300_000

# Module constants rather than literals so the self-test can point them at temp values.
MARKER_DIRECTORY = os.path.join(tempfile.gettempdir(), "claude-context-warning")
TAIL_CHUNK_BYTES = 4 * 1024 * 1024

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
THRESHOLD_PATTERN = re.compile(r"^[0-9]+$")
USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class HookInputError(Exception):
    """An input this hook cannot work with. The message is shown to Pete as-is."""


def did_not_run(why: str) -> dict:
    return {"systemMessage": f"Context-size warning did not run: {why}. Not blocking."}


def thousands(tokens: int) -> str:
    """312345 -> '312k'; 1500 -> '1.5k'. Whole thousands at the sizes this hook talks about."""
    if tokens >= 10_000 or tokens % 1000 == 0:
        return f"{tokens // 1000:,}k"
    return f"{tokens / 1000:.1f}k"


def parse_threshold(argv: list[str]) -> int:
    """Read `--threshold <tokens>` from argv. Absent means the default.

    Hand-rolled rather than argparse: argparse exits 2 on a bad argument, and an exit 2
    from a Stop hook blocks the turn.
    """
    if not argv:
        return DEFAULT_THRESHOLD
    if len(argv) != 2 or argv[0] != "--threshold":
        raise HookInputError(f"expected '--threshold <tokens>', got {' '.join(argv)!r}")
    raw = argv[1]
    if not THRESHOLD_PATTERN.match(raw) or int(raw) <= 0:
        raise HookInputError(f"--threshold must be a positive whole number, got {raw!r}")
    return int(raw)


def context_size_of_line(raw: bytes) -> int | None:
    """The context size recorded on one transcript line, or None if it records none."""
    if b'"usage"' not in raw:
        return None
    try:
        entry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None  # a malformed line, or the half-written last line of a live file
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    if entry.get("isSidechain") is True:
        return None
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    total = 0
    for field in USAGE_FIELDS:
        value = usage.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        total += value
    return total or None  # all-zero usage is a synthetic entry, not a real call


def last_context_size(transcript_path: str) -> int | None:
    """The context size of the transcript's last main-thread call, or None if it has none.

    Reads backwards from the end in chunks that double in size. The first line of a chunk
    that does not start at byte 0 may be cut, so it is dropped; if it held the answer, the
    next, larger chunk holds it whole.
    """
    file_size = os.path.getsize(transcript_path)
    chunk = TAIL_CHUNK_BYTES
    with open(transcript_path, "rb") as handle:
        while True:
            start = max(0, file_size - chunk)
            handle.seek(start)
            lines = handle.read(file_size - start).split(b"\n")
            if start > 0:
                lines = lines[1:]
            for raw in reversed(lines):
                size = context_size_of_line(raw)
                if size is not None:
                    return size
            if start == 0:
                return None
            chunk *= 2


def marker_path(session_id: str) -> str:
    return os.path.join(MARKER_DIRECTORY, f"{session_id}.marker")


def claim_marker(session_id: str) -> str:
    """Atomically record this crossing. Returns 'claimed', 'exists', or an error message."""
    try:
        os.makedirs(MARKER_DIRECTORY, exist_ok=True)
        descriptor = os.open(marker_path(session_id), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if os.path.isfile(marker_path(session_id)):
            return "exists"
        return f"{marker_path(session_id)} exists but is not a file"
    except OSError as exc:
        return f"{exc.strerror or exc!r} ({exc.filename or MARKER_DIRECTORY})"
    os.close(descriptor)
    return "claimed"


def release_marker(session_id: str) -> str | None:
    """Delete this session's marker so the next crossing blocks. Returns an error or None."""
    try:
        os.remove(marker_path(session_id))
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{exc.strerror or exc!r} ({exc.filename or marker_path(session_id)})"
    return None


def block_reason(size: int, threshold: int) -> str:
    n, t = thousands(size), thousands(threshold)
    return (
        f"This session's context is {n} tokens, past the {t} mark, and every further turn "
        "re-reads all of it. Finish the step you are on; do not start a new one. Then write "
        "the hand-off: if this project uses wrap-session, run it; otherwise say where the "
        "work stands. End by giving Pete the opening message for a fresh session. This "
        f"blocks once; it will not block again unless the context drops below {t} and "
        "crosses it again."
    )


def note_to_pete(size: int, threshold: int) -> str:
    return f"Context {thousands(size)}, past {thousands(threshold)}: wrap this session."


def evaluate(payload: object, threshold: int) -> dict | None:
    """Return the JSON document to print, or None to stay silent.

    Kept separate from main() so the self-test can drive the decision path without a
    subprocess, and so main() has nothing in it but I/O.
    """
    if not isinstance(payload, dict):
        return did_not_run("the hook input was not a JSON object")
    if payload.get("hook_event_name") != "Stop":
        return None

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.match(session_id):
        return did_not_run(f"session_id {session_id!r} is not usable as a file name")

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return did_not_run("the hook input has no transcript_path")
    try:
        size = last_context_size(transcript_path)
    except OSError as exc:
        return did_not_run(f"could not read the transcript ({exc.strerror or exc!r})")
    if size is None:
        return did_not_run("the transcript records no context size yet")

    if size < threshold:
        problem = release_marker(session_id)
        if problem:
            return {
                "systemMessage": (
                    f"Context-size warning could not re-arm: {problem}. The next crossing of "
                    f"{thousands(threshold)} will show a note but not block."
                )
            }
        return None

    claim = claim_marker(session_id)
    if claim == "claimed":
        return {
            "decision": "block",
            "reason": block_reason(size, threshold),
            "systemMessage": note_to_pete(size, threshold),
        }
    if claim == "exists":
        return {"systemMessage": note_to_pete(size, threshold)}
    return {
        "systemMessage": (
            f"{note_to_pete(size, threshold)} (Could not record the warning: {claim}. "
            "Not blocking.)"
        )
    }


def run(argv: list[str], stdin_text: str) -> dict | None:
    """The whole hook, inputs to output, with every failure turned into a visible note."""
    try:
        threshold = parse_threshold(argv)
        try:
            payload = json.loads(stdin_text)
        except ValueError:
            return did_not_run("the hook input was not JSON")
        return evaluate(payload, threshold)
    except HookInputError as exc:
        return did_not_run(str(exc))
    except Exception as exc:  # noqa: BLE001 -- fail open on anything, but say so
        return did_not_run(f"unexpected error {exc!r}")


def main() -> None:
    document = run(sys.argv[1:], sys.stdin.read())
    if document is not None:
        print(json.dumps(document))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        # Last resort: run() already turns every reachable failure into a note, so this is
        # stdin or stdout itself failing. Exiting 0 is the one thing that must still happen.
        pass
    sys.exit(0)
