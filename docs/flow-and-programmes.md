# Flow and programmes

The one written definition of Flow. It is carried verbatim into each project that runs the
build loop, at `docs/flow-and-programmes.md`; never edit a carried copy. Change it in
dev-process and let the sync deliver it. `CLAUDE.md` holds a pointer to this file and
nothing else about Flow.

Process logic is built once in dev-process and carried verbatim. A gate on another project
is only `rollout: <unit> working in <owner/repo>`.

## When it applies

While the `build-loop` skill (`.claude/skills/build-loop/SKILL.md`) is running, and for
one session's own PR on a `flow/<slug>` branch in a project whose session switch is on
(`"sessions": "flow"` in `.claude/autonomy.json`, read from `main`; see "The check"). Every
other session follows the escalation list in `CLAUDE.md` unchanged.

## The check

The loop works `.claude/queue.json` unit by unit. Before **every** merge it runs
`python scripts/check_autonomy.py --unit <id> --pr <n> --permission-mode <mode>`; the
answer is the mode for that one merge, never cached.

- **Flow** (exit 0): merge without asking, once CI is green on the PR **and** `pr-adversary`
  returned APPROVE. The ship guard's merge warning still fires; in Flow it is expected, not
  a stop. Creating a migration that is not destructive is lifted from "Stop and ask": it is
  reviewed and CI-tested before merge. A dependency is lifted only where the approved plan
  names it: a PR that changes a dependency manifest or lockfile is Gated unless the plan has
  a `Dependencies: <names>` line (not absent, not `none`). For a programme unit that line
  sits in the unit's `### <ID>:` section of the programme plan on `main`; for a session PR,
  in a `docs/**/*plan*.md` file the PR itself adds (a line inside a code fence does not
  count). The checker does not match package names: the reviewers do.
- **Gated** (exit 1, or any error): open the PR, add "merge go for <unit>" to Pete's list,
  move on. Never merge.

**A session's own PR** is judged by `check_autonomy.py --session --pr <n>` (CI:
`--ci --pr <n> --head flow/<slug>`): no unit, no programme, the PR's diff is the scope.
Gated if any of: the autonomy file is not `"mode": "programs"` with `"sessions": "flow"`;
the deploying-CI rule (not applied where the file says `"deploys": "flow"`), and the
permission-mode and override rules above; a protected path; an always-Pete path (a path
component such as `payments`, `billing`, `secrets`, `credentials`, `email`, `notifications`,
a `.env*`, `*.pem` or `*.key` file, or a prefix in the project's own `always_pete_paths`
list; sign-in and security paths are left to the security reviewer); an existing test
removed, renamed or modified with a deleted line; a migration removed, modified or added
with a destructive statement; a dependency manifest or lockfile changed without the plan's
`Dependencies:` line (above); an added line shaped like a live key; or a file whose diff
text GitHub omits. The verdict says `"session": true` only for Flow. Plan: dev-process
`docs/af2-interactive-flow-plan-V0-1.md`.

**A unit in no programme.** Where the session switch is on, `--unit <id>` for a unit in no
programme is judged by these session rules on the PR's diff, not gated for having no
programme. A unit marked `"mode": "gated"`, or in a gated programme, stays Gated.

`python scripts/check_autonomy.py --ceiling` reports which programmes could run in Flow, as
far as the repo's own files decide, for display (the board); it takes no PR or unit and
never decides a merge.

## Programmes

**Flow belongs to a programme**, never to the repo. A programme is one plan Pete approved
(`docs/programs/`), its numbered units recorded in `.claude/programs.json`. Pete's merge go
on the PR that adds it is the approval and the switch. Its units then merge in Flow until
none is left; every unit outside a Flow programme is Gated (unless the session switch is on:
see "A unit in no programme"), and so is every session PR where the session switch is off. A PR touching the programmes or autonomy files, the loop's
own machinery or CI config never merges in Flow. The one standing exception is the daily
adopter sync: its verbatim PRs merge on green CI with no go and no programme, a grant Pete
gave on 2026-11-15; the rules (kill switch, hold list, canary ring, loop claim) are in
`distribution-and-drift.md`.

**Numbered units.** Work that is to run in Flow is a programme, even of one unit (Pete,
2026-10-30), or, where the project's session switch is on, one session's own PR, with ids of a
prefix plus a number in run order (`M30`, `M31`; `DB-11`). Numbers are never reused or
renumbered; an insert takes a letter (`M31a`).

## Who merges

**The desktop session merge.** In a project whose session switch is on and `"merge":
"github"` is not, an interactive session merges its own PR. After the PR opens it runs
`check_autonomy.py --session --pr <n> --permission-mode <mode>`; on Flow it runs
`pr-adversary` on the PR and watches CI, and on APPROVE and green it runs `gh pr merge <n>
--squash --delete-branch`, confirms `mergedAt`, and watches `main`'s CI. On Gated, BLOCK, red
or a refused merge it says why in plain words and stops for Pete's go. The check needs no
`"merge": "github"`: that key is the GitHub-side switch only. A tick (an unattended
scheduled run) never merges from the desktop: the app's classifier refused it
(`harness-limits.md`), so ticks keep the GitHub-side merge. ship-it carries this path from
AP4; until it ships, a session in a project whose session switch is on runs these steps
itself and stops for the go only on Gated, BLOCK, red or a refused merge.

## The GitHub-side merge

Where a repo has switched it on, the loop's own session never merges: it pushes the
`loop/<id>` branch and opens the PR, and `.github/workflows/flow-merge.yml` does the rest.
An interactive session in such a repo does the same on a `flow/<slug>` branch: it never
merges either. Plan: dev-process `docs/flow-mode-programmes-plan-V0-7.md`.

- **The switch** is `"merge": "github"` in the repo's own `.claude/autonomy.json`, read from
  `main`. Anything else, or no key, is off, silently. Three repo secrets sit behind it:
  `CLAUDE_CODE_OAUTH_TOKEN` (a `claude setup-token` token), `FLOW_MERGE_APP_ID` and
  `FLOW_MERGE_APP_KEY` (a private GitHub App with contents write and pull requests write,
  installed on that repo only). Pete adds them. Off in dev-process.
- **The gate** runs when CI passes on a `loop/<id>`, `flow/<slug>` or `release/<tag>` PR: `check_autonomy.py
  --ci` (the same rules as the desktop check, without permission mode), and the PR must
  still be at the commit CI passed. Not clear: a comment says why and it is left for Pete.
  Any other branch starts no runner. The label `flow: hold` on a PR (loop or session) leaves
  it for Pete; Decide reads it, and Merge reads it again just before merging.
- **Reviewers** run as separate jobs: `pr-adversary`, `persona-security-red-teamer` and
  `persona-senior-developer` always, the other personas by what the PR touches
  (`scripts/flow_verdict.py select`). Each reads its agent file from `main`. Each must end
  with exactly `VERDICT: PASS`; anything else is BLOCK (`flow_verdict.py verdict`). For a
  session PR the prompt adds the Always Pete list below: a change touching any of it must be
  BLOCK, naming which. That is the judgement behind the path and diff rules, which cannot
  see a payment or messaging change in a blandly named file. Every reviewer prompt, unit or
  session, also carries: BLOCK if a dependency is added that the plan does not name, or an
  existing test is deleted or rewritten and the plan does not say so.
- **The merge** needs every reviewer PASS, uses the App's token (so `main`'s CI runs after
  it) and `--match-head-commit`, so a later push is refused, not merged. Any BLOCK or failed
  job: no merge, the label `flow: blocked`, the reports as comments.
- **The decision is made in one run on `main`'s copy of the workflow.** It reads its own
  reviewer jobs' results, never commit statuses, so a status posted with Pete's token counts
  for nothing, and a PR cannot change the rules it is judged by (the workflow, agents and
  Flow files are protected paths).
- **Undo:** ask a session to open a revert PR for the merge (`git revert <merge commit>` on
  a `revert-<n>-...` branch, then a PR). The workflow ignores it, since it is not a `loop/`
  branch; Pete merges it. GitHub's own **Revert** button is not relied on: it was absent
  on a PR the App merged in the sandbox (FM6b acceptance 5f, 2026-10-28), and why is not
  known. A revert that conflicts with later merges needs the conflict fixed by hand.

**Stated limits.** A reviewer reads the diff, so text in it can try to talk it into PASS;
three always-on reviewers, the strict verdict and protected paths reduce this, nothing
removes it. There is no branch protection (free plan), so anyone holding Pete's token can
still merge outside the workflow. `pr-adversary` runs the PR's code with the Claude token
present: never switch this on in a public repo with outside contributors (`llm-gateway`).
Free-plan Actions minutes (2,000 a month) are shared by every private repo.

## The loop allowlist and the tick's mode

- **Allowlist.** `.claude/loop-allowlist.json` (carried verbatim) lists the commands a tick
  runs up to "PR open" and never a merge. `settings.json` is merged per project, so
  `scripts/check_allowlist.py` fails when a project's `permissions.allow` lacks a rule from
  the file or holds a never-list rule (merge, API write, push to `main`, force push, reset,
  clean, `rm`, the deploy script, ssh); `--fix` appends the missing ones. A repo that wants
  one of those grants it itself. It stops permission prompts only: in auto mode the
  server-side classifier can still deny an allowed command (FM6a).
- **Mode.** A tick passes the `permissionMode` that `get_session("self")` reports, never one
  typed in the prompt; a failed call or any mode but `auto` is Gated. Unexercised until FM5.

## The kill switch

Fastest first. Pete saying "gated" in the session switches to Gated at once
(`--override gated`; for an open session PR, ship-it adds `flow: hold`). Pete adding the
`flow: hold` label on GitHub (phone) leaves that PR for him, even mid-run. A project removing
`"sessions": "flow"` on `main` turns session Flow off and leaves programmes alone;
`.claude/autonomy.json` saying `"gated"` is the standing kill switch for everything; no
`"merge": "github"` turns only the GitHub-side merge off. Nothing said in a session switches
to Flow.

**The deploy switch.** `"deploys": "flow"` in `.claude/autonomy.json`, read from `main`,
stops a deploying CI from gating a merge (rule 4, S2). Any other value, or no key, leaves
that rule on. Every verdict says `"deploys": true|false`.

**Before the first real user.** Remove `"deploys": "flow"` in that project. The switch rests
on a bad merge costing one revert; once users depend on `main`, it does not.

## Always Pete

**Always Pete** (both modes, programmes and sessions): payments; secrets and live keys;
deleting data or a destructive migration; logging on to the live server, or a deploy in a
project without the deploy switch; anything that messages real people (email, SMS, social
post, notification); another repo's `main`, except a verbatim sync PR (`sync_adopters.py`
copies canonical files unchanged and merges on green CI). Sign-in and security changes are
not on the list: the security reviewer reads them. Items on his list rather than stops. A unit that needs one carries
`"mode": "gated"`, and in a programme is also listed in its `gated_units`.

**One carve-out:** the release PR at the end of a Flow programme (`release/<tag>`, in a
project with `.claude/release.json`) is not on this list: it merges on green with its
reviewers if its diff is the strict release shape, and in Gated it waits for Pete. It
reaches users with no human look; the undo is a revert PR and a new release. Record: the
process repo's `docs/doctrine/release-at-programme-end.md`.

**Protected machinery** (never Flow): CI/CD configuration, the loop's and Flow's own files,
deleting or rewriting an existing test.

Detection is paths and diff rules first, the reviewers' judgement behind them. Neither is
complete; a miss is undone by a revert PR.

Pete's list is `PETE-TODO.md`, generated by `python scripts/check_queue.py --write-todo`;
never edit it by hand. He works through it by saying "what do I owe" (build-loop §3a).
