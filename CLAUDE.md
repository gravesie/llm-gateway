# llm-gateway: project development rules

The process rules (deploy contract, escalation list, Flow pointer, planning, verifying,
session shape) are in `.claude/rules/dev-process.md`, carried verbatim from dev-process and
loaded every session. Never edit that copy. This file holds only this project's facts and
rules; it may add stops but never remove one.

## Verifying a change

- Tests: `python -m pytest` — the full suite. A red test is a real regression, not noise.
- Lint: `python -m ruff check src tests` — must be clean.
- No dev server, no port. This is a library, exercised through its test suite.
- After changing the package, reinstall it in the venv (`pip install -e .`) before
  running anything that imports it. A stale install is this project's version of a
  stale dev server.
- Provider calls in tests are mocked. A test that makes a real API call costs money
  every time the suite runs and must not be merged.

## Project-specific rules

**This is a library, not a service.** It is imported into consuming applications and runs
inside their process. There is no server, no database, no container and no deploy target.
If a change starts to need one, that is a design decision to raise, not to implement.

**Provider credentials live in `.env` and nowhere else.** `.env.example` is the committed
contract. Never write a key into source, a test, a fixture, a log line or a commit
message. Cost records must not contain prompt or completion text.

**Never route requests through subscription credentials.** Anthropic, OpenAI and Google
all prohibit using consumer-plan OAuth tokens or session credentials from outside their
own applications, and all three enforce it with account bans. Researched 2026-09-02;
sources are in `docs/decisions.md`. If a future session spots an opportunity to "use the
Max plan instead of the API", the answer is no, and the reason is already written down.

**Never state a price or a token rate from memory.** Provider pricing changes. Every cost
constant carries a comment with its source URL and the date it was checked. A stale rate
produces a confident wrong number, which is worse than no number.

**The router must fail open.** A fault in routing, budgeting or cost logging must never
stop a consuming application from making its call. Every wrapper path needs a tested
fallback to a direct provider call.

**Cost figures are the product.** A bug that misattributes spend is as serious as one that
crashes. The only reason this library exists is to be believed.

`.claude/hooks/stop_gate.py`, a Stop hook, checks two turn-level invariants at the end of
every turn: the work is on a branch rather than on `main`, and the branch's first commit
is the plan, on its own, before any code (matching `docs/*plan*.md`, or carrying a
`Plan-exempt: <reason>` trailer). Those are the two things that make an unwatched session
recoverable — if it dies, `main` is clean and the intent is in git. It blocks exactly once
per stop-chain and then lets the turn end, so it is one recorded nudge, not a wall. It
does not judge whether the code is correct; nothing deterministic can.

What a fresh worktree needs beyond the checkout itself is in "Fresh worktree" below.

## Fresh worktree

Tested 2026-09-06 on Windows 11, cutting a worktree from the primary checkout at `0d74b37`.
The recipe works, and it is shorter than the one this section used to predict — three
commands, no `.env`:

```
git worktree add -b <branch> <sibling-path> main
cd <sibling-path> && python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

That is the whole thing. 249 tests passed and `ruff check src tests` came back clean in a tree
that had never seen a `.env`. Budget about three minutes: the worktree is instant, the venv
12s, the install 2m31s, the suite 15s.

**There is no `.env` to copy, and the suite does not want one.** The primary checkout has
never had one — only the committed `.env.example`. Provider calls in tests are mocked and
`tests/conftest.py` blocks the network at three layers, so a credential would have nothing to
do. The old claim that you copy `.env` across was wrong: it was reasoning from projects that
have a runtime, and this one does not. Copy a `.env` only to hand-run something against a real
provider, which is not something the suite ever does.

**Nothing else is missing either.** `.claude/` — hooks, `settings.json`, workflows — is
tracked, so the guard hook is live in a fresh worktree with no setup. There are no local data
directories to create: `costs/` exists in neither tree, and the cost log is opt-in via
`LLM_GATEWAY_COST_LOG`. The only untracked thing worth having is the venv, which step two
builds. Each worktree gets its own, so the editable install resolves to that worktree's `src`
and the two trees cannot shadow one another.

**The one caveat in the setup is version drift, and it is not the worktree's fault.** There is no
lockfile, so a fresh install resolves whatever is newest that day. The test tree came up on
ruff 0.16.6 against the primary checkout's 0.16.5; litellm, pytest, openai and pydantic
matched. CI installs the same floating way (`pip install -e ".[dev]"`), so a fresh worktree is
*closer* to CI than a long-lived checkout is. If a lint error shows up in one tree and not the
other, this is why — and CI will agree with the fresher one.

**Tearing down is where it bites: `gh pr merge --delete-branch` half-fails from inside a
worktree, and the failure reads as though the merge failed.** It stops with ``fatal: 'main' is
already used by worktree at <primary>``, because its post-merge local checkout cannot take
`main` while the primary tree holds it. **The merge has already succeeded by then.** The error
is the tidy-up, not the merge. Confirm that before reacting, and never retry the merge on the
strength of that message — from the primary checkout:

```
gh pr view <n> --json state,mergeCommit
git worktree remove <sibling-path>
git branch -d <branch>
git push origin --delete <branch>
```

Neither branch is deleted for you, so both of those last two are needed. `git worktree remove`
does not need `--force`; the gitignored venv does not block it.

**`git branch -d` after a squash merge goes one of two ways, and which one depends on whether
the remote tracking ref still exists.** `-d` deletes a branch that is fully merged into *its
upstream*, or into *HEAD* when no upstream is set. A squash merge never satisfies the HEAD
test — the commit on `main` is a different commit from the one on the branch — so:

- **Tracking ref still there** (the worktree case above, where `--delete-branch` half-failed
  and so never removed the remote branch): the local tip still equals `origin/<branch>`, the
  upstream test passes, and git deletes it while warning that it is "not yet merged to HEAD".
  That warning is the squash merge and is not a reason to reach for `-D`.
- **Tracking ref gone** (`--delete-branch` succeeded, or you have since run `git fetch
  --prune`): there is no upstream left to test against, git falls back to HEAD, and it
  **refuses** with "not fully merged", hinting at `-D`.

In the second case `-D` is correct, but confirm the content actually landed before forcing
anything — `git diff <branch> origin/main -- <the files>` should come back empty. Do not reach
for `-D` on the strength of the hint alone.

Observed 2026-09-06 on PR #10 (first case, from a worktree) and 2026-09-07 on PR #12 (second
case, from the primary checkout). PR #10 wrote this section and recorded only the behaviour it
happened to hit. Both cases were then reproduced deterministically with synthetic refs, so the
rule above is tested rather than inferred from two anecdotes.
