# llm-gateway: project development rules

Supplements the global `~/.claude/CLAUDE.md`. Everything here is committed, so it
applies from any client — including a session started on claude.ai, which cannot see
anything in `~/.claude`.

## The deploy contract

**Merging to `main` is the deploy.** CI runs lint and the full suite on every pull
request; on merge it runs them again and then deploys. There is no manual deploy step
and no reason to SSH to production. See `DEPLOY.md` — that file is the single source of
truth, and if it ever disagrees with `.github/workflows/ci.yml`, the workflow is right
and the doc is the bug.

Merging itself requires an explicit go from Pete in the session. A green suite is not
authorisation to merge.

## Escalation list

The point of this list is that unattended work has *known bounds*, rather than depending
on a case-by-case judgement about what counts as important. Follow it literally.

**Proceed without asking:**

- Read anything in the repo.
- Write and edit code, tests and docs within the session's agreed work unit.
- Run the test suite, the linter, and local database migrations.
- Restart the local dev server.
- Create a branch, commit locally, push the session branch, open the pull request.
- Create a git worktree for this session instead of reusing a shared checkout.
- Install a dependency that is already pinned in the project's lockfile or manifest.

**Stop and ask:**

- Adding a new third-party dependency.
- Creating a new database migration.
- Anything touching authentication, payments, secrets, or a spend cap.
- Deleting or rewriting an existing test.
- Work outside the agreed scope of this session — flag it, don't absorb it.
- `gh pr merge`, pushing to `main`, force-pushing.
- Any action against production.
- Changing CI/CD configuration.
- Reading or rotating a secret.

The last five are also enforced by a PreToolUse hook (`.claude/hooks/guard_ship.py`),
so they prompt even under an auto-accept permission mode.

## Concurrent sessions

One working tree per session — never run two sessions against the same checkout. Verify
`git branch --show-current` before branching; the session-start snapshot can be stale if
another session moved the tree since it was taken. Use `git worktree add -b <branch>
<sibling-path> main`. What a fresh worktree needs beyond the checkout itself —
dependency installs, `.env`, local data directories — is project-specific; see
"Fresh worktree" below.

## Planning

Enter plan mode before writing code when the change is multi-file, or touches database
schema, auth, payments or deploy. Single-file, clearly-scoped changes do not need it —
state the understanding and approach in a sentence or two and go.

## Verifying a change

- Tests: `python -m pytest` — the full suite. A red test is a real regression, not noise.
- Lint: `python -m ruff check src tests` — must be clean.
- No dev server, no port. This is a library, exercised through its test suite.
- After changing the package, reinstall it in the venv (`pip install -e .`) before
  running anything that imports it. A stale install is this project's version of a
  stale dev server.
- Provider calls in tests are mocked. A test that makes a real API call costs money
  every time the suite runs and must not be merged.

## Session shape

One session = one work unit = one PR. When the work unit is done, wrap up rather than
starting the next job in the same thread.

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

**The one real caveat is version drift, and it is not the worktree's fault.** There is no
lockfile, so a fresh install resolves whatever is newest that day. The test tree came up on
ruff 0.16.6 against the primary checkout's 0.16.5; litellm, pytest, openai and pydantic
matched. CI installs the same floating way (`pip install -e ".[dev]"`), so a fresh worktree is
*closer* to CI than a long-lived checkout is. If a lint error shows up in one tree and not the
other, this is why — and CI will agree with the fresher one.
