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
"Project-specific rules" below.

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

Untested as of 2026-09-02. The expected recipe is a venv, an editable install of the dev
extra, and a copy of `.env` from the primary checkout. Confirm it works and then record
what actually happened here, replacing this paragraph. Do not treat the above as tested.
