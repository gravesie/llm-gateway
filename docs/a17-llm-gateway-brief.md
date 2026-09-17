# A17 (part 2 of 2) — llm-gateway: bring the hook baseline up to canonical

Agreed brief, carried into the repo as the branch's first commit so it survives a session
that ends without warning. Branch: `a17-llm-gateway-baseline`.

Written and agreed by the dev-process session that planned this (2026-09-17); committed
here the same session. Verbatim below.

---

## Context

`dev-process`'s distribution register is behind on two repos (A17). pc-update's half
shipped 2026-09-14 (`pc-update#1`, `2c6f660`) — it had to commit an uncommitted onboarding
first, then upgrade it. llm-gateway is a different shape: it's a real, actively-developed
Python library (14 merged PRs, its own CI, its own test suite) that has simply never had
its `.claude/` hooks re-seeded since a very old baseline (pre-#224 in canonical terms: no
fail-closed fallback, no A11/A12 items, no MCP tool coverage, no Stop hook at all). This
unit brings it up to what web-auditor/lead-magnet/Personality-Analysis/goyande-scorecard
already carry, following the same file-for-file shape as pc-update's upgrade commit so the
two remain cross-checkable, per the standing note in `queue.json`.

Verified this session, current state:
- `.claude/hooks/`: `session_start.py` (65 lines), `guard_ship.py` (88 lines, no
  fail-closed, no MCP arm). No `stop_gate.py`.
- `.claude/settings.json`: matcher is `Bash` alone, no `|| printf` fallback, no Stop hook.
- No `scripts/check_environment.py`, no `scripts/check_context_budget.py`, no
  `.claude/context-budget.json`.
- `.claude/workflows/` still carries all four retired Workflow files (`check-sync.js`,
  `personas.js`, `progress-review.js`, `schemas.js`) — the arm is retired repo-wide
  (V2-0 §7); queue note says delete them in this pass rather than a separate round-trip.
- `CLAUDE.md` (9,719 b) already has real, tailored escalation bullets and correctly
  describes its CI (no false "no CI" claim to fix, unlike pc-update). It has only a
  one-line hook mention (lines 45–46) to expand, and **no live secret** — checked
  `CLAUDE.md` for credentials per the standing "check before starting" note; only policy
  text about not touching secrets, nothing live.
- `.gitattributes` already exists and is **out of scope** — A22 owns reconciling
  `.gitattributes` across adopters as its own unit; not touched here.
- `scripts/git-hooks/pre-push` and `scripts/install-hooks.sh` already exist and already
  run the *real* lint+test commands (unlike pc-update, which had a placeholder) — the
  checkers get added alongside, not used to replace a stub.
- Canonical blobs, re-verified against `dev-process` `main` (`6bdec7a`, current after
  A23 — the note that canonical moves each time a unit lands holds, but these three hook
  blobs are unchanged since the pc-update brief):
  `session_start.py 2d0021b7c18d777b5b5278e01874ee82f488b325` ·
  `guard_ship.py 9acb1d0ea29933cb8172f3297f5cf50e6c08e3d7` ·
  `stop_gate.py f748cbbd65823a1b7b17adeb5e20c8b172006233` ·
  `check_environment.py 11c73b589b094c2d2e4942a0c463ea7f8f34c8d5` ·
  `check_context_budget.py 07f99dda22abc3d4d9f8375cbfad2ff291fc3297`.

Branch: `a17-llm-gateway-baseline`, off `main` (currently `4246799`, local now fast-forwarded).
One session, one PR, into `gravesie/llm-gateway`. Do not touch `dev-process` or pc-update here
— the register update is a separate PR that must merge last, per the standing rule.

**This PR touches CI config**, which is on llm-gateway's own "Stop and ask" list — flagging
that here; plan approval is that go-ahead, same as it was for pc-update's brief.

## Commits

**1 — the brief.** Save this plan as `docs/a17-llm-gateway-brief.md` in llm-gateway and
commit it first, before any code — satisfies `stop_gate.py`'s plan-first invariant (once
that hook is live) and survives a session that ends without warning.

**2 — upgrade to canonical.** Mirrors the shape of pc-update's commit 3 so the two diffs
stay comparable:
1. `.claude/hooks/session_start.py` ← canonical, verbatim (blob `2d0021b7`)
2. `.claude/hooks/guard_ship.py` ← canonical, verbatim (blob `9acb1d0e`)
3. `.claude/hooks/stop_gate.py` (NEW) ← canonical, verbatim (blob `f748cbbd`)
4. `scripts/check_environment.py` (NEW) ← canonical, verbatim (blob `11c73b58`)
5. `scripts/check_context_budget.py` (NEW) ← canonical, verbatim (blob `07f99dda`)
6. `.claude/settings.json` — rebuild from `template/.claude/settings.json`: adds the MCP
   matcher (`merge_pull_request`/`push_files`/`create_or_update_file`/`delete_file`
   alongside `Bash`), the filtered `|| printf` fail-closed fallback, and the Stop hook.
   Hooks-only file, nothing project-specific to preserve.
7. `.claude/context-budget.json` (NEW) — author for this repo, same shape as pc-update's
   and lead-magnet's: budget `CLAUDE.md` only (not `DEPLOY.md` — read on demand, not
   loaded every turn). Measure `CLAUDE.md`'s byte count *after* commit 4's wording change
   below, then set `max_bytes` with headroom in the same ballpark as pc-update's ratio
   (~17% above measured, so the 90%-of-cap WARN band is a real nudge, not a trip-wire on
   arrival).

Expect this: items 1, 2 and 6 swap this session's own live guard underneath itself
mid-session — settings.json hook edits take effect immediately (measured on pc-update).
The guard gets stricter and may start prompting on git operations the old 88-line one
waved through. That's the guard working, not a fault.

**3 — wire the checkers in.**
- `scripts/git-hooks/pre-push` — insert the environment and context-budget checks
  *alongside* the existing lint+test steps (this repo's pre-push already runs the real
  `ruff check` and `pytest`, not a placeholder — don't replace them, add to them). Order:
  environment check first (validates the budget check's own input — a working tree that's
  drifted from the index makes local and CI measure different files), then budget, then
  the existing lint/test.
- `.github/workflows/ci.yml` — add a standalone `checks` job (env + budget,
  `actions/setup-python@v7`, python 3.13) alongside the existing `test` and `release`
  jobs — not folded into `test`, not a separate `checks.yml` file. Same call pc-update
  made: this repo already has a `ci.yml`, so a second workflow file would be noise.

**4 — CLAUDE.md.** Replace the one-line hook mention (lines 45–46, "The last five are also
enforced by a PreToolUse hook…") with two things:
- The canonical `guard_ship.py` paragraph, word-for-word from `template/CLAUDE.md`
  (already carried verbatim into goyande-scorecard) — what it watches, its MCP coverage,
  "it warns, it does not block."
- A `stop_gate.py` paragraph modelled on pc-update's (the canonical template doesn't
  itself document `stop_gate.py` — a gap, not this unit's to fix): the two turn-level
  invariants it checks (branch not `main`; first commit is the plan), that it blocks once
  per stop-chain, that it doesn't judge code correctness.

No other CLAUDE.md wording is wrong here (unlike pc-update, this repo's CI claim was
already accurate) — don't touch anything else in the file.

**5 — delete the four retired workflow files.** `.claude/workflows/check-sync.js`,
`personas.js`, `progress-review.js`, `schemas.js` — the Workflow arm is retired
(V2-0 §7). Separate commit from the doc/hook upgrade since it's an unrelated cleanup,
already in scope per the queue note ("since this unit is already in that repo").

## Verify

- After commit 2: re-run the canonical checker from the dev-process checkout if useful,
  but the real proof is `git hash-object` on each of the five distributed files against
  the blob ids above — byte-identical, not "looks right."
- `python -m ruff check src tests` and `python -m pytest` still clean after all commits
  (the pre-push hook will enforce this on push anyway, but check before pushing since it
  isn't installed in this clone by default — `./scripts/install-hooks.sh` first, or run
  the two checker scripts and the suite by hand).
- `python scripts/check_environment.py` and `python scripts/check_context_budget.py` both
  green.
- Confirm no config drift: `git status` clean, only the intended files touched.

## After merge

Report the five verified blob hashes back to the dev-process session (or leave them in
the PR description) — the register update (`queue.json` + `.claude/distribution.json`)
is a separate PR there and must merge last, same rule as pc-update's half.
