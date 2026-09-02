// Edge contracts for graph-engineering workflows (Phase 1).
//
// These are JSON Schemas, not TypeScript types, because they are enforced at the
// Workflow tool's agent() call boundary: pass one as `schema` and the subagent is
// forced to return data matching it. The property being bought is replaceability —
// any two nodes that emit the same schema can swap without touching what consumes them.
//
// No orchestration code here. Phase 2 wires these into the first real workflow
// (progress-review) and is where they get their first real test.

// A single review finding. `file`/`line` are optional because not every review has a
// code location to point at — a progress-review persona critiquing a UI panel or a
// business owner reading commercial copy has no file:line, only a claim and evidence.
const FINDING_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['claim', 'category', 'severity'],
  properties: {
    file: {
      type: 'string',
      description: 'Repo-relative path, if this finding points at code. Omit otherwise.',
    },
    line: {
      type: 'integer',
      description: '1-indexed line the finding anchors to. Omit if not line-specific.',
    },
    severity: {
      type: 'string',
      enum: ['critical', 'high', 'medium', 'low'],
    },
    claim: {
      type: 'string',
      description: 'The finding itself, one sentence. What is wrong, missing, or not good enough.',
    },
    evidence: {
      type: 'string',
      description: 'What was checked or observed to support the claim — a quote, a value, a repro step.',
    },
    category: {
      type: 'string',
      description: 'Short kebab-case slug, e.g. "accuracy", "actionability", "security", "maintainability".',
    },
  },
}

// The output of a verifier whose job is to try to disprove a Finding, not confirm it
// (Phase 4). A verifier that agrees by default is just the same reasoning asking
// itself the same question — this schema makes "refuted" the field it must commit to.
const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['refuted', 'reasoning'],
  properties: {
    refuted: {
      type: 'boolean',
      description: 'true if this verifier could not stand the finding up under scrutiny.',
    },
    reasoning: {
      type: 'string',
      description: 'Why. Must reference what was actually examined, not just restate the claim.',
    },
    evidenceExamined: {
      type: 'array',
      items: { type: 'string' },
      description: 'Files read, commands run, or output inspected while checking this finding.',
    },
  },
}

// One persona's independent review (progress-review, Phase 2). Findings are split into
// the three buckets the skill already asks for in prose — wrong / missing / not yet
// good enough — so the synthesis stage can merge and rank across personas in code
// instead of re-deriving the split from free text.
const PERSONA_REPORT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['persona', 'wrong', 'missing', 'notGoodEnough'],
  properties: {
    persona: {
      type: 'string',
      description: 'Which persona produced this report, e.g. "domain-expert", "security-red-teamer".',
    },
    wrong: {
      type: 'array',
      items: FINDING_SCHEMA,
      description: 'Things reported that are inaccurate, or that contradict the evidence.',
    },
    missing: {
      type: 'array',
      items: FINDING_SCHEMA,
      description: 'Things this persona expected to see and did not.',
    },
    notGoodEnough: {
      type: 'array',
      items: FINDING_SCHEMA,
      description: 'Things present and correct but below the bar for shipping.',
    },
  },
}

// The output of the semantic merge pass: which findings are the same finding.
//
// This schema exists because of a measured failure, and the measurement belongs next to
// it. Phase 2 shipped with grouping done in code, keyed on category + file:line. Scored
// against a semantic pass over a real 94-finding review (2026-08-30, the ASB gate run),
// that key achieved 0.02 recall. Re-keying cannot rescue it: only 41.5% of findings carry
// a file at all, and of the same-finding pairs that DO carry two anchors, most point at
// different files — one persona blames the audit module, another blames the template, and
// both are right. The recall ceiling for ANY (file, line, category) key on that data is
// 4.7%.
//
// So grouping is a judgement about prose and belongs to a model. What stays in code is
// everything downstream of the groups: counting distinct personas, taking the max
// severity, scoring and ordering. That half was always sound and is untouched.
//
// Findings are referenced by the ids the caller assigns at flatten time, never by their
// text, so a group can never silently rewrite a claim.
const FINDING_GROUPS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['groups'],
  properties: {
    groups: {
      type: 'array',
      description:
        'One entry per set of findings that are the SAME finding. Findings that stand alone are kept automatically — do not list them. Never put a finding in two groups.',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['ids', 'summary'],
        properties: {
          ids: {
            type: 'array',
            items: { type: 'string' },
            description: 'Two or more finding ids (e.g. "F3"), exactly as given in the input list.',
          },
          summary: {
            type: 'string',
            description:
              'One sentence naming the single defect these findings all describe. Not a list of what each said.',
          },
          category: {
            type: 'string',
            description: 'Short kebab-case slug for the merged group, e.g. "accuracy".',
          },
        },
      },
    },
  },
}

// A routing decision: which lenses to run and how deep, decided by code from the shape
// of the change rather than judged fresh by a model every time (Phase 3). This is the
// escalation list in CLAUDE.md, made executable.
const REVIEW_SCOPE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['lenses', 'depth', 'reason'],
  properties: {
    lenses: {
      type: 'array',
      items: { type: 'string' },
      description: 'Persona/lens keys to run, e.g. ["security-red-teamer", "senior-developer"].',
    },
    depth: {
      type: 'string',
      enum: ['shallow', 'standard', 'deep'],
    },
    reason: {
      type: 'string',
      description: 'Why this lens set and depth were chosen — the diff shape or signal that triggered it.',
    },
  },
}

module.exports = {
  FINDING_SCHEMA,
  VERDICT_SCHEMA,
  PERSONA_REPORT_SCHEMA,
  FINDING_GROUPS_SCHEMA,
  REVIEW_SCOPE_SCHEMA,
}
