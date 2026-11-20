# Development process rules

Carried verbatim from dev-process: never edit this copy, the daily sync replaces it.
Project facts and project-only rules are in this project's `CLAUDE.md`, which may add
stops but never remove one. Everything here is committed, so it applies from any client.

## The deploy contract

**Merging to `main` is the deploy.** CI runs lint and the full suite on every pull
request; on merge it runs them again and then deploys. There is no manual deploy step
and no reason to SSH to production. See `DEPLOY.md` — that file is the single source of
truth, and if it ever disagrees with `.github/workflows/ci.yml`, the workflow is right
and the doc is the bug.

Merging itself requires an explicit go from Pete in the session. A green suite is not
authorisation to merge. The exception is Flow (`docs/flow-and-programmes.md`): an approved
programme's units, and, where the project's session switch is on, a session's own PR. Where
`"merge": "github"` is set the GitHub-side workflow merges it and the session never does;
otherwise the session merges its own PR once the Flow check says Flow, CI is green and
`pr-adversary` returns APPROVE (the desktop merge).

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
- At wrap-session, the dev-dashboard refresh exactly as its Step 2d specifies (path,
  `collect.py`, `render.py`, `publish.py`, clean `main` only, `publish.py` sends
  `out/index.html` and `out/health.json` only). Not "an action against production" for the list below; nothing
  else in dev-dashboard or on `devdash.goyande.ai` is covered.

**Stop and ask:**

- Adding a third-party dependency the approved plan does not name.
- Anything touching payments, secrets, or a spend cap.
- Deleting data, or a destructive migration.
- Anything that sends a message to a real person (email, SMS, social post, notification).
- Deleting or rewriting an existing test.
- Work outside the agreed scope of this session: queue it and carry on, don't absorb it.
- `gh pr merge` (unless the Flow check says Flow), pushing to `main`, force-pushing — and the same actions taken through
  the GitHub MCP server rather than a shell (`merge_pull_request`; `push_files`,
  `create_or_update_file`, `delete_file` against `main`) or through the desktop app's own
  merge path (`set_auto_merge`, enabling). Reaching for a different tool does not make it
  a different action.
- Committing onto `main` or `master`, in this repo or any other.
- Any git-mutating command against a checkout this session is not rooted in — another
  repo's, or this repo's own primary tree when the session is in a worktree.
- Logging on to production, or a deploy in a project without the deploy switch (merging
  where `"deploys": "flow"` is set is an ordinary merge).
- Changing CI/CD configuration.
- Reading or rotating a secret.

`.claude/hooks/guard_ship.py`, a PreToolUse hook, watches for the git and production
items above — merging, pushing to a protected branch, force-pushing, writing history onto
a protected branch (`commit`, `merge`, `cherry-pick`, `revert`, `rebase`, `pull`) or
force-moving one (`branch -f`/`-M`, `checkout -B`, `switch -C`), `update-ref` on any
branch, the history writers plus `reset`, `checkout`/`switch`, `restore`, `stash` and
`clean` in someone else's primary checkout, running the deploy script by hand, and SSH to
production. It resolves the repo the command actually targets, so a `cd` or `git -C` into
another project is covered too; when the shell decides the target (an unknown variable,
`$(…)`), it asks. It also covers the GitHub MCP equivalents by tool name —
`merge_pull_request` always, and `push_files`/`create_or_update_file`/`delete_file` when
the target branch is `main` or absent — and the desktop app's own `set_auto_merge`, when
it is enabling auto-merge rather than disabling it — which is why its `settings.json`
matcher names those tools alongside `Bash`. Change the two together; a tool the matcher
omits never invokes the hook at all. Not covered: `reset` on your own protected branch
(the routine sync), deleting a branch, `add`/`rm`/`mv`/`apply` in another checkout, and
any merge path that is neither a shell command nor one of those five MCP tools — your
client may have its own. **It warns, it does not block**: measured,
it fires and names the repo, branch and reason as a visible message in both auto-accept
and manual permission modes, but in an auto-accept permission mode the action proceeds
regardless of the `ask` it returns. Treat its message as a cue to stop yourself, not as
something that stops for you. Changing CI config and handling secrets are on you to stop
for; no hook can recognise them from a command line.

## Autonomy: the build loop

Flow, Gated and programmes are defined once, in `docs/flow-and-programmes.md`, which is
carried verbatim: never edit it or restate it here. Before every merge the build loop runs
`python scripts/check_autonomy.py --unit <id> --pr <n> --permission-mode <mode>`, the mode
being what `get_session("self")` reports as `permissionMode`, never one typed in the prompt
(a failed call or any mode but `auto` is Gated). ship-it's session form is `--session
--pr <n>` (same mode rule). A gate on another project is only the `rollout:` form.

## Talking to Pete

Pete did not write the code. Anything addressed to him (a question, a decision, a
blocker, a review verdict, a to-do item, a PR summary, a session hand-off) is written for
one of two readers:
- Technical issue: an experienced CIO who knows how the system is used in the
  organisation, but not its internals.
- Business issue: the business owner, non-technical, who cares about customers, sales,
  cost and risk.

Rules:
- Be extremely brief and direct. Give enough detail for the decision or next task, but no more.
- No function, file, variable, table, test or library names. No line numbers, no code.
  Name things by what a user sees or does: "the sign-in email", "the approval screen",
  "this morning's Cornwall post".
- Every item follows this shape, in this order:
  1. What Claude needs from Pete: the decision, yes/no or action, in one line.
  2. What happens now: what a user sees or experiences today. Show an example if it helps.
  3. If we make this change: what users will see that is different, and what it costs in
     time or money.
  4. If we don't: the concrete problem: who hits it, how likely it is, how bad it is.
  5. Claude's recommendation.
- Technical detail (file paths, reproductions, line numbers) goes to whoever fixes it, or
  into the PR description. It does not go in the message to Pete. Give it if he asks.
- Never relay a subagent's report verbatim. Translate it into this shape first.
- Gate verdicts and decisions are in plain English. A pause for Pete's input has one `**Decision needed:** ...? yes/no` line, placed above the Next-Action line, which stays last.
- "Terse" means short, not technical. Keep every fact that bears on the decision; drop
  the internals.
- Any file or folder you point Pete to (to check, read or give feedback on), in a message,
  plan, table, summary or hand-off: give the full path starting with `C:\` on every
  mention, including each row of a table. Never a bare name, slug or relative path, and
  never "24 drafts in four folders" without the paths. This overrides the no-file-names
  rule above, because Pete needs to open it. Telling him a file has been created is the
  same case.
- Never use the words "you" or "I" when talking to Pete. It is unclear who each one is.
  Use "Claude" and "Pete" instead, so it is always clear who does what.

## Concurrent sessions

One working tree per session — never run two sessions against the same checkout. Verify
`git branch --show-current` before branching; the session-start snapshot can be stale if
another session moved the tree since it was taken. Use `git worktree add -b <branch>
<sibling-path> main`. What a fresh worktree needs beyond the checkout itself —
dependency installs, `.env`, local data directories — is project-specific; see
"Project-specific rules" in this project's `CLAUDE.md`.

This holds for **every** repo a session touches, not only this one. Needing a commit in
another project means `git worktree add` in that project too, or handing the work to a
session already rooted there. Branching inside another repo's primary checkout is not
enough: the tree itself is what a concurrent session is using.

## Planning

Enter plan mode before writing code when the change is multi-file, or touches database
schema, auth, payments or deploy. Single-file, clearly-scoped changes do not need it —
state the understanding and approach in a sentence or two and go.

Once the plan is agreed, commit it into the repo as the branch's first commit, before any
code. A session can end without warning, and when it does `wrap-session` never runs and
the hand-off is never written — anything held only in the conversation is lost. Plan
mode's file under `~/.claude/plans/` does not count: it is outside the repo, so a fresh
checkout and a claude.ai session cannot see it.

After a plan-mode plan is committed and pushed, the planning session stops and writes no
code. In the desktop app it offers a fresh Sonnet session in the same worktree, titled
`<ID> build`, whose brief starts "Build <ID> from <plan path>"; elsewhere it prints that
opening message. Pete may decline and build in place. A unit that needed no plan mode
carries on. Rule and card fields: `docs/doctrine/plan-build-handoff.md` (dev-process).

## LLM features

Any feature that sends a prompt to an LLM follows `docs/llm-features.md`: prompts visible
and admin-editable with versions, every call logged, evals where the output matters. A plan
that adds or changes an LLM call says how it meets that file.

## Verifying work: standing rules

- Tests: run the full suite with the command in this project's `CLAUDE.md`. A red test is
  a real regression, not noise.
  A green suite is evidence only for files a test reads. For each changed file, name what
  reads it (a test, a checker, the harness, or nothing), run that checker, and report it
  as the evidence; if nothing automated reads a file, say so. Never skip the suite on that
  judgement. Record: `docs/doctrine/verifying-your-own-work.md` (dev-process).
- Local ports: before giving or writing any command, config or doc that names a local port
  (the dev server, a database connection, or a port a container publishes), read it from `PORTS.md` on the dev-process repo's origin/main (`git show origin/main:PORTS.md`
  if the checkout is behind). Never quote a port from memory or from a project file alone. If
  this project's config disagrees with `PORTS.md`, say so. Never pick or change a port here
  without editing that file first. Record: `docs/doctrine/local-ports.md` (dev-process).
- Restart the dev server before handing off for manual or browser verification. A stale
  process serving old code is the specific failure this guards against.
- After writing a file, check its size on disk is non-zero and roughly what you meant.
  A failed write leaves an empty file, and two empty files compare equal — so a diff or
  a checksum against another artefact reports success. That has happened here.
- Write files with the Write tool, not by piping their contents into a Bash command.
  Heredocs themselves are byte-exact; it is the inline command that truncates once it
  gets long, which leaves the heredoc unterminated and bash running the body as code.
  Measured: 478 b inline fine, 21 KB inline failed, the same 21 KB run from a file
  round-tripped exactly. So the fix is the Write tool, not avoiding heredocs.
- Environment: `python scripts/check_environment.py --report` — line endings matching
  the index, and text I/O with an explicit encoding. Enforced by the pre-push hook.

## What belongs in CLAUDE.md

Rules that must hold in every session, and nothing else. CLAUDE.md is loaded eagerly —
it is in the context window before the session does anything and re-sent on every turn,
so every kilobyte in it is charged to every session for as long as it stays.

Records go elsewhere: why a decision was made, what a work unit measured, the live case
a rule was written against, all belong in `docs/doctrine/<topic>.md`, indexed by
`docs/doctrine/README.md` and read on demand. When you add a rule to CLAUDE.md, add its
record there and link to it — do not write the record in CLAUDE.md.

The ceiling is enforced. `.claude/context-budget.json` holds it, and
`scripts/check_context_budget.py` fails the push and the CI run when CLAUDE.md exceeds
it. Check where you stand at any time:

```bash
python scripts/check_context_budget.py --report
```

Raising the ceiling is allowed but deliberate: edit `max_bytes` and say why in the
commit message. If you find yourself wanting to, the answer is almost always that
something in CLAUDE.md is a record rather than a rule.

## Session shape

One session = one work unit = one PR. When the work unit is done, wrap up rather than
starting the next job in the same thread.

Wrap only after the PR has merged, or Pete says "pause". Once the next-session prompt is
written the session is closed: answer questions only, until Pete types `reopen this session`.
A side session (its brief starts `Role: side of <parent>`) ships its PR and reports back
without wrapping; put follow-ups on the queue rather than starting sessions for them.
