# LLM features: visible prompts, logged calls, evals

Pete, 2026-10-30. Carried verbatim into adopting projects as `docs/llm-features.md`, where
`CLAUDE.md` points at it. The canonical copy is `docs/doctrine/llm-features.md` in
dev-process; edit it there, never in a project.

**The aim: anyone running the app can see what it asks an LLM to do, and how well it does
it.** This applies to every feature that sends a prompt to an LLM.

## The standard

1. **Every prompt is visible, and editable by admins, in the app's admin area.**
   - The default wording lives in the code, and is what a fresh install and the tests use.
   - An admin edit is saved as a new version. Every version is kept, with who saved it and
     when, and any earlier version can be restored in one action.
   - The parts the code depends on are locked: the inputs the code fills in and the answer
     format the code reads. A save that removes or breaks one is refused, with the reason.
   - A prompt whose job is a safety or security check is shown but not editable.
   - Only admins can edit.
2. **Every LLM call is logged.** Input, output, prompt version, model, tokens, cost and time
   taken. This is part of the feature, not optional infrastructure: the global instruction
   not to add logging unless it is in the brief does not apply to it. Each result in the app
   can be traced to the prompt version that produced it. LLM calls are paid for, so any
   spend cap is checked before the call, not only recorded after it.
3. **Evals, where the output matters.** Where an LLM's output reaches a customer or feeds a
   decision, the feature has an eval set: at least 10 real cases with known good answers or
   a scoring rule. It runs before a new prompt version or a model change goes live, and its
   scores are shown next to the prompt in the admin area. An eval run costs API money, so
   it runs on a change, not on a schedule.
4. **At planning time, say which of these apply.** A plan that adds or changes an LLM call
   names how it meets 1-3, and gives the reason for any it skips.

What is personal data in a logged input or output is handled as the project already handles
personal data: the log is not a way round that.

## Why

**Visibility.** A prompt buried in code is logic the business owner cannot read or adjust.
Putting it in the admin area makes the app's reasoning inspectable by the people who run it.

**Why the guards.** An editable prompt with no guards fails in three ways: an edit quietly
breaks the feature (most often by changing the answer format the code reads), nobody can
tell which wording produced a given result, and a bad edit has no undo. Versioning, the
locked contract parts and the trace from result to version close each one. Safety prompts
are view-only because an edit there removes a protection rather than changing behaviour.

**Why the log comes first.** Evals run on real cases, and real cases come from the log. The
log is also the cheapest visibility there is: what was asked, what came back, what it cost.

**Why "where the output matters" for evals.** Each eval run is paid for in API calls, and an
eval set needs known good answers, which take effort to collect. A feature whose output
nobody acts on does not repay that.

Plan: `docs/lf1-llm-features-standard-plan-V0-1.md` (dev-process).
