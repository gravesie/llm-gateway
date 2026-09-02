// progress-review as a workflow graph (Phase 2 of the graph-engineering migration).
//
// Fan-out over independent personas -> barrier -> code-side merge -> one synthesis agent.
// The shape is what ~/.claude/skills/progress-review/SKILL.md already describes in prose:
// "each persona is run as an independent subagent so they cannot contaminate each other's
// judgement", then a synthesis pass. What changes is who does the deterministic work.
// Grouping, counting cross-persona agreement and ranking are now plain JS; the model is
// left with the semantic judgement that code cannot do.
//
// ---------------------------------------------------------------------------
// WHY THIS FILE IS SELF-CONTAINED
//
// The Workflow runtime gives scripts standard JS built-ins but "no filesystem or Node.js
// API access" — which rules out require(). So the schemas from ./schemas.js and the
// persona briefs from ./personas.js are INLINED below rather than imported.
//
// Those two files remain the authoring source of truth: they are ordinary CommonJS,
// requireable by test and tooling code outside the Workflow runtime, and shared by the
// Phase 3 and Phase 4 scripts still to come. This file holds a copy.
//
// The copies are kept honest by ./check-sync.js, which loads both modules and this
// file's declarations and asserts they still match. Run `node check-sync.js` after
// editing any of the three.
//
// CONFIRMED by running a probe workflow, 2026-08-29: `typeof require` is "undefined" in
// the Workflow runtime, as are `module` and `process`. There is no way to collapse this
// duplication, so check-sync.js is load-bearing rather than belt-and-braces.
//
// Same probe, worth knowing before editing this file: workflow scripts are parsed as
// CommonJS, NOT as ES modules, despite `export const meta` being the required syntax —
// an `import.meta` reference fails to launch with "only valid inside modules". Do not
// reach for other ESM-only constructs here.
// ---------------------------------------------------------------------------

export const meta = {
  name: 'progress-review',
  description:
    'Independent multi-persona ship-readiness review, semantically merged, counted and ranked in code, synthesised once.',
  whenToUse:
    'Quality and accuracy review of what has been built — "is this ready to ship". Not a bug hunt on a diff; use code-review for that. Needs real output as evidence, not just code.',
  phases: [
    { title: 'Personas', detail: 'one independent agent per persona, typed output' },
    { title: 'Merge', detail: 'one pass deciding which findings are the same finding' },
    { title: 'Synthesis', detail: 'rank merged groups into one ship-readiness list', model: 'opus' },
  ],
}

// ---------------------------------------------------------------------------
// Inlined from ./schemas.js — see "WHY THIS FILE IS SELF-CONTAINED" above.
// ---------------------------------------------------------------------------

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
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
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

// ---------------------------------------------------------------------------
// Inlined from ./personas.js. Briefs are verbatim from SKILL.md — do not paraphrase
// when editing; a reworded brief silently changes what the review looks for.
// ---------------------------------------------------------------------------

const PERSONAS = {
  'domain-expert': {
    key: 'domain-expert',
    title: 'Domain expert',
    cares: 'Accuracy',
    brief: `You are a senior <DOMAIN — default: SEO> expert. You are interested in one thing: is
the output truthful? Does it reflect what is actually going on with the subject? Has
everything been counted correctly, and has it been counted the right way?

The test that matters: if you did this analysis by hand, would anything you found
contradict what the product reports? Go and check the underlying subject yourself
where you can. Look for both directions of error — things reported that are not there,
and things there that are not reported.

You do not care about how it looks, how it is worded, or how hard it is to build.`,
  },
  implementer: {
    key: 'implementer',
    title: 'Implementer',
    cares: 'Actionability',
    brief: `You are the web developer who has to action every fix this output recommends.

You care about accuracy too, because you will be the one who looks stupid if you act
on something wrong. But your main concern is the **detail in the drill-downs** — the
pop-ups and expanded panels, not the summary screen. For every item that is not green:
is there enough there to actually fix it? Specific URLs, the full list rather than a
count, the exact elements, links to the standard or the doc?

Judge each one by whether you could start work from it without going back and asking
a question. Where you could not, say exactly what is missing.`,
  },
  'business-owner': {
    key: 'business-owner',
    title: 'Business owner',
    cares: 'Commercial clarity',
    brief: `You own this business. You are not technical and you have no interest in becoming
technical. You care about the commercial performance of your website, and a bit about
how it looks.

On the main screen you want no technical detail at all — that belongs in the
drill-downs, for your developer to deal with. What you want is clear messaging on
every point: what is it, is it good or bad, and does it matter?

Above all you want to know **what to do first**, and what genuinely does not matter
much. You have a very clear view of what you want commercially, and you will not spend
money on something you cannot connect to it.

Flag every place you have to guess what something means, and every place the priority
is not obvious.`,
  },
  designer: {
    key: 'designer',
    title: 'Designer',
    cares: 'Brand and experience',
    brief: `You are a brand-led graphic and web designer. You produce pixel-perfect work.

You are judging whether this looks genuinely good and whether the brand is well
represented and consistently applied — typography, colour, spacing, hierarchy,
alignment. You are also judging fit: does this look like it was made for its intended
customer, or does it look generic?

And you are judging the experience: is the journey through this clear, does the eye go
to the right thing first, is anything cramped, noisy, or hard to read?

Be specific about location. "The score panel" not "the design".`,
  },
  'security-red-teamer': {
    key: 'security-red-teamer',
    title: 'Security red-teamer',
    cares: 'How this gets abused',
    brief: `You are red-teaming this. Assume every input is hostile and every control is
unenforced until you have traced it. Look for outbound requests built from
user-controlled input, metered API calls without a spend cap checked *before* the call,
state changes on GET, one-time tokens that are not consumed, privileged effects applied
on unverified client input, and any control applied to some routes of a shape but not
all of them.`,
  },
  'senior-developer': {
    key: 'senior-developer',
    title: 'Senior developer',
    cares: 'Whether this survives six months',
    brief: `You are inheriting this codebase in six months with no handover.

Can you follow it? Is logic repeated where it should be shared? Are there abstractions
that no longer earn their place, dead code, or TODOs standing in for work not done? Do
the tests exercise the real logic or just the happy path around it? Would a new
contributor be able to add a feature without breaking something they cannot see?`,
  },
  'ops-reliability': {
    key: 'ops-reliability',
    title: 'Ops / reliability',
    cares: 'What happens when it fails at 3am',
    brief: `It is 3am and this has broken. Can you tell what happened, and can you get it back?

Consider deploy and rollback, migrations that cannot be reversed, failure modes that
produce plausible-looking output from empty input, silent retries, unbounded resource
use, and anything that loses data rather than erroring.`,
  },
}

const PERSONA_SETS = {
  product: ['domain-expert', 'implementer', 'business-owner', 'designer'],
  engineering: ['security-red-teamer', 'senior-developer', 'ops-reliability'],
}

const PLACEHOLDER = /<([A-Z][A-Z0-9_]*)\s*—\s*default:\s*([^>]*)>/g

function renderBrief(persona, params) {
  const overrides = params && typeof params === 'object' && !Array.isArray(params) ? params : {}
  return persona.brief.replace(PLACEHOLDER, (_m, name, fallback) => {
    const v = overrides[name]
    return v === undefined || v === null ? fallback.trim() : String(v)
  })
}

function resolvePersonas(selector) {
  const sel = selector === undefined || selector === null ? 'product' : selector
  let keys

  if (typeof sel === 'string') {
    keys = PERSONA_SETS[sel]
    if (!keys) {
      throw new Error(
        `Unknown persona set "${sel}". Known sets: ${Object.keys(PERSONA_SETS).join(', ')}. ` +
          `To use an explicit list, pass personas as an array of keys.`
      )
    }
  } else if (Array.isArray(sel)) {
    if (sel.length === 0) {
      throw new Error('Persona list is empty — a review with no personas produces nothing.')
    }
    keys = sel
  } else {
    throw new Error(
      `Persona selector must be a set name or an array of persona keys, got ${typeof sel}.`
    )
  }

  const unknown = keys.filter((k) => !PERSONAS[k])
  if (unknown.length) {
    throw new Error(
      `Unknown persona key(s): ${unknown.join(', ')}. Known: ${Object.keys(PERSONAS).join(', ')}.`
    )
  }
  // De-duplicate: running a persona twice doubles the spend and fabricates the
  // cross-persona agreement that the merge stage ranks on.
  return [...new Set(keys)].map((k) => PERSONAS[k])
}

// ---------------------------------------------------------------------------
// The merge, and the division of labour between a model and this code.
//
// WHAT CHANGED, AND THE MEASUREMENT THAT FORCED IT (2026-08-30, the ASB gate run):
//
// Phase 2 originally grouped findings here in code, keyed on category + file:line, on the
// argument that exact matching on structured fields is reliable. It is reliable. It is
// also nearly useless for this task. Scored against a semantic pass over a real
// 94-finding review, that key reached **0.02 recall** — 2 of 127 same-finding pairs.
//
// Re-keying does not fix it, and this is the part worth not re-deriving:
//   - only 41.5% of findings carry a file anchor at all;
//   - of the same-finding pairs that DO carry two anchors, most point at DIFFERENT files,
//     legitimately — one persona blames app/conversion.py, another blames
//     app/audits/content_strategy.py, and they are describing one defect from two layers;
//   - so the recall ceiling for ANY (file, line, category) key on that data is 4.7%.
// Loosening the key to `file` alone lifts the multi-persona count from 1 to 7 and drops
// precision to 0.43 — it fuses three unrelated defects in site_detail.html into one
// "agreement". A count that looks like agreement and is not is worse than no count.
//
// A file anchor identifies WHERE A FIX GOES, not WHAT THE FINDING IS. So:
//
//   - Deciding which findings are the same finding is a judgement about prose, and is
//     done by the Merge agent (FINDING_GROUPS_SCHEMA).
//   - Counting distinct personas per group, taking the max severity, scoring and
//     ordering stay here, in deterministic code. That half was always sound. This is
//     still the migration's point: the model judges, the code counts.
//
// The anchor path is KEPT as the degraded mode for when the merge agent fails, because
// the alternative is losing the run. It is logged loudly when it happens — a 5%-recall
// merge presented as a merge is exactly the failure this replaced.
// ---------------------------------------------------------------------------

const SEVERITY_WEIGHT = { critical: 4, high: 3, medium: 2, low: 1 }
const BUCKETS = ['wrong', 'missing', 'notGoodEnough']

function severityWeight(severity) {
  return SEVERITY_WEIGHT[String(severity || '').toLowerCase()] || 0
}

/**
 * Deterministic anchor for a finding, or null when it has none.
 * Normalises path separators and case so the same file reported two ways still matches.
 */
function anchorOf(finding) {
  const raw = typeof finding.file === 'string' ? finding.file.trim() : ''
  if (!raw) return null
  const file = raw.replace(/\\/g, '/').replace(/^\.\//, '').toLowerCase()
  return Number.isInteger(finding.line) ? `${file}:${finding.line}` : file
}

function clusterKeyOf(finding) {
  const anchor = anchorOf(finding)
  if (!anchor) return null
  return `${String(finding.category || '').trim().toLowerCase()}::${anchor}`
}

/**
 * Flatten every persona report into one tagged list, giving each finding a stable id.
 *
 * Defensive about shape: a persona agent can fail (agent() resolves to null) or return a
 * report with a bucket missing, and neither should take the whole review down.
 *
 * `personaKeys` is the caller's index-aligned list of registry keys. It exists because
 * `report.persona` is MODEL-SUPPLIED and drifts: on the 2026-08-30 run, 3 of 4 agents
 * renamed themselves ("web-developer-actioning-the-fixes" for `implementer`,
 * "non-technical-business-owner" for `business-owner`, "brand-and-visual-designer" for
 * `designer`). The returned object then said `personas: ['implementer', ...]` while every
 * finding under it said something else — two vocabularies for one persona, in data a
 * caller is meant to persist. The registry key is the truth and is stamped on here.
 *
 * Ids are `F1..Fn` in flatten order: stable, deterministic, and short enough that the
 * merge agent can reference many of them without burning its output budget.
 */
function flattenReports(reports, personaKeys) {
  const keys = Array.isArray(personaKeys) ? personaKeys : []
  const flat = []
  for (let i = 0; i < reports.length; i++) {
    const report = reports[i]
    if (!report || typeof report !== 'object') continue
    const persona =
      typeof keys[i] === 'string' && keys[i]
        ? keys[i]
        : typeof report.persona === 'string'
          ? report.persona
          : 'unknown'
    for (const bucket of BUCKETS) {
      const items = report[bucket]
      if (!Array.isArray(items)) continue
      for (const finding of items) {
        if (!finding || typeof finding !== 'object') continue
        flat.push({ ...finding, persona, bucket, id: `F${flat.length + 1}` })
      }
    }
  }
  return flat
}

/**
 * Resolve the merge agent's groups against the flattened findings.
 *
 * Every rule here is about not letting a model's output delete or duplicate a finding:
 *   - an id nobody recognises is ignored (counted, so the caller can see it happened);
 *   - a finding claimed by two groups joins the first — a finding must not be double
 *     counted into two different agreement scores;
 *   - a finding in no group becomes its own singleton, never dropped;
 *   - a group left with fewer than two findings after resolution collapses to singletons,
 *     so a "group" can never assert agreement it does not have.
 */
function resolveGroups(flat, groups) {
  const byId = new Map(flat.map((f) => [f.id, f]))
  const claimed = new Set()
  const resolved = []
  let unknownIds = 0

  for (const group of Array.isArray(groups) ? groups : []) {
    if (!group || typeof group !== 'object' || !Array.isArray(group.ids)) continue
    const members = []
    for (const rawId of group.ids) {
      const id = typeof rawId === 'string' ? rawId.trim() : ''
      if (!byId.has(id)) {
        unknownIds++
        continue
      }
      if (claimed.has(id)) continue
      claimed.add(id)
      members.push(byId.get(id))
    }
    if (members.length > 1) {
      resolved.push({
        findings: members,
        summary: typeof group.summary === 'string' ? group.summary : '',
        category: typeof group.category === 'string' ? group.category : members[0].category,
      })
    } else {
      // Not a group. Release the one member back to the singleton pile below.
      for (const m of members) claimed.delete(m.id)
    }
  }

  for (const f of flat) {
    if (!claimed.has(f.id)) {
      resolved.push({ findings: [f], summary: '', category: f.category })
    }
  }
  return { resolved, unknownIds }
}

/** Count personas, take max severity, score and describe one group of findings. */
function summariseGroup(findings, extra) {
  const personas = [...new Set(findings.map((f) => f.persona))]
  const maxWeight = Math.max(...findings.map((f) => severityWeight(f.severity)))
  const severity =
    Object.keys(SEVERITY_WEIGHT).find((s) => SEVERITY_WEIGHT[s] === maxWeight) || 'low'
  return {
    findings,
    personas,
    personaCount: personas.length,
    severity,
    buckets: [...new Set(findings.map((f) => f.bucket))],
    score: maxWeight * personas.length,
    ...extra,
  }
}

/**
 * Merge findings into groups, count distinct personas per group, and rank.
 *
 * Ranking is severity weight x distinct-persona count. Findings several personas raise
 * independently are the signal — SKILL.md already says the overlap and the disagreement
 * are the interesting parts — and counting them is deterministic, so code should do it.
 * That is unchanged. What changed is where the groups come from; see the banner above.
 *
 * @param {object[]} reports  PERSONA_REPORT_SCHEMA objects (nulls tolerated).
 * @param {object}   [options]
 * @param {string[]} [options.personaKeys]  registry keys, index-aligned with `reports`.
 * @param {object[]} [options.groups]       FINDING_GROUPS_SCHEMA groups. Omit to fall
 *                                          back to anchor clustering (degraded mode).
 * @returns {{clusters: object[], unclustered: object[], stats: object}}
 *   `clusters` are groups of two or more findings; `unclustered` are singletons. Every
 *   finding appears in exactly one of the two, always.
 */
function clusterFindings(reports, options) {
  const opts = options && typeof options === 'object' ? options : {}
  const flat = flattenReports(reports, opts.personaKeys)
  const semantic = Array.isArray(opts.groups)
  const baseStats = {
    personaReports: reports.filter((r) => r && typeof r === 'object').length,
    totalFindings: flat.length,
    mergeMode: semantic ? 'semantic' : 'anchor',
  }

  let clusters
  let unclustered

  if (semantic) {
    const { resolved, unknownIds } = resolveGroups(flat, opts.groups)
    const merged = resolved.filter((g) => g.findings.length > 1)
    const singles = resolved.filter((g) => g.findings.length === 1)
    clusters = merged.map((g, i) =>
      summariseGroup(g.findings, {
        key: `G${i + 1}`,
        category: g.category,
        summary: g.summary,
        // A merged group spans layers by design, so it has no single file. Keep every
        // anchor its members offered rather than picking one and implying the others
        // were wrong — the whole reason the anchor key failed is that they disagree.
        files: [...new Set(g.findings.map((f) => f.file).filter(Boolean))],
      })
    )
    unclustered = singles.map((g) => g.findings[0])
    baseStats.unknownIdsFromMerge = unknownIds
  } else {
    const byKey = new Map()
    unclustered = []
    for (const finding of flat) {
      const key = clusterKeyOf(finding)
      if (!key) {
        unclustered.push(finding)
        continue
      }
      if (!byKey.has(key)) {
        byKey.set(key, {
          key,
          category: finding.category,
          file: finding.file,
          line: Number.isInteger(finding.line) ? finding.line : null,
          findings: [],
        })
      }
      byKey.get(key).findings.push(finding)
    }
    clusters = [...byKey.values()].map((c) => summariseGroup(c.findings, c))
  }

  // Stable, fully-determined ordering. Nothing time- or randomness-derived here: both
  // are unavailable in the Workflow runtime, and a non-deterministic order would break
  // run resume.
  clusters.sort(
    (a, b) =>
      b.score - a.score ||
      b.personaCount - a.personaCount ||
      severityWeight(b.severity) - severityWeight(a.severity) ||
      String(a.key).localeCompare(String(b.key))
  )

  unclustered.sort(
    (a, b) =>
      severityWeight(b.severity) - severityWeight(a.severity) ||
      String(a.persona).localeCompare(String(b.persona)) ||
      String(a.claim).localeCompare(String(b.claim))
  )

  const clusteredFindings = clusters.reduce((n, c) => n + c.findings.length, 0)
  return {
    clusters,
    unclustered,
    stats: {
      ...baseStats,
      clusteredFindings,
      unclusteredFindings: unclustered.length,
      clusterCount: clusters.length,
      multiPersonaClusters: clusters.filter((c) => c.personaCount > 1).length,
    },
  }
}

// ---------------------------------------------------------------------------
// Prompt construction
// ---------------------------------------------------------------------------

function formatEvidence(evidence) {
  return evidence.map((e, i) => `${i + 1}. ${e}`).join('\n')
}

function mergePrompt(subject, flat) {
  const list = flat
    .map(
      (f) =>
        `${f.id}. [${f.persona} / ${f.bucket} / ${f.severity} / ${f.category}]` +
        `${f.file ? ` (${f.file}${Number.isInteger(f.line) ? `:${f.line}` : ''})` : ''}\n` +
        `    ${f.claim}\n` +
        `    evidence: ${f.evidence || '(none given)'}`
    )
    .join('\n')

  return `${flat.length} findings were produced by several reviewers working independently
on the same subject, with no visibility of each other. Your only job is to say which of
them are **the same finding**.

## Subject

${subject}

## The findings

${list}

## What counts as the same finding

One defect, described by two or more reviewers in their own words. The test is whether
**one fix clears every member of the group**. Two findings that are merely about the same
area, the same page or the same file are NOT the same finding.

Group across reviewers freely, and group across the wrong / missing / not-good-enough
split freely — one reviewer calling something wrong and another calling it missing is very
often one defect seen from two sides.

**Do not group on the file path.** Reviewers legitimately anchor one defect at different
layers: a value computed wrongly in an audit module and rendered wrongly in a template is
one defect with two plausible files. Equally, one file can hold several unrelated defects.
Judge the claim, not the location.

Be conservative in one specific direction: a group asserts that independent reviewers
agreed, and that agreement is weighted heavily downstream. If you are unsure whether two
findings are one defect, leave them apart.

## Output

Return only the groups of two or more. Anything you do not list is kept automatically as a
finding in its own right — omitting a finding does not discard it, so there is no need to
list singletons. Never put one id in two groups. Use the ids exactly as given.`
}

function personaPrompt(persona, params, subject, evidence) {
  return `${renderBrief(persona, params)}

---

## What you are reviewing

${subject}

## Your evidence

Work from this evidence directly. Open the URLs, read the files, run the commands. Real
output beats reading code — a review of actual output on a known input is worth more than
a review of the code that produces it.

${formatEvidence(evidence)}

## How to report

Be specific. "The trust signals panel says 3 when a manual check finds 7" beats "accuracy
could be improved". Every finding needs evidence: what you actually checked or observed.

Separate what is **wrong** (inaccurate, contradicts the evidence) from what is **missing**
(you expected it and it is not there) from what is **not good enough yet** (present and
correct, but below the bar for shipping).

Where a finding points at a specific file, give the repo-relative path and line. Where it
does not — a UI panel, a piece of copy, a commercial judgement — leave file and line out
rather than inventing one. An invented path is worse than no path.

Stay in character. Judge only what your brief tells you to care about, and do not soften a
finding for the sake of balance. Other personas are reviewing this independently and will
cover what you are ignoring; you do not need to be well-rounded.`
}

function synthesisPrompt(subject, merged, personaKeys) {
  const { clusters, unclustered, stats } = merged
  const semantic = stats.mergeMode === 'semantic'

  const clusterBlock = clusters.length
    ? clusters
        .map(
          (c, i) =>
            // Labelled positionally, after the sort, in BOTH modes. These labels are what
            // the synthesis output cites back ("Merged from C12, U3"), so they have to
            // match what the reader of that output sees — not the internal cluster key.
            `### C${i + 1} — ${c.category}` +
            (semantic
              ? `${c.files && c.files.length ? ` (anchored at ${c.files.join(', ')})` : ''}\n` +
                `Merge summary: ${c.summary || '(none given)'}\n`
              : ` @ ${c.file}${c.line ? `:${c.line}` : ''}\n`) +
            `Raised by ${c.personaCount} persona(s): ${c.personas.join(', ')}\n` +
            `Max severity: ${c.severity} | rank score: ${c.score} | buckets: ${c.buckets.join(', ')}\n` +
            c.findings
              .map((f) => `  - [${f.persona} / ${f.bucket} / ${f.severity}] ${f.claim}\n    evidence: ${f.evidence || '(none given)'}`)
              .join('\n')
        )
        .join('\n\n')
    : semantic
      ? '(none — the merge pass found no finding raised more than once)'
      : '(none — no findings carried a file anchor)'

  const singleBlock = unclustered.length
    ? unclustered
        .map(
          (f, i) =>
            `U${i + 1}. [${f.persona} / ${f.bucket} / ${f.severity} / ${f.category}] ${f.claim}\n` +
            `    evidence: ${f.evidence || '(none given)'}`
        )
        .join('\n')
    : '(none)'

  const mergeSection = semantic
    ? `## What has already been done for you

A separate pass read all ${stats.totalFindings} findings and judged which of them describe the
same defect. Code then counted distinct personas per group, took the max severity, and
ranked by severity x persona count. **The counting and the ranking are arithmetic and are
correct — do not redo them.**

The grouping is a judgement, not arithmetic, and it was made by a model like you. Treat it
as a strong starting point rather than a settled fact: if two groups below are plainly one
defect, merge them and say so. If a group has swept together things one fix would not
clear, split it and say so.

${stats.multiPersonaClusters} of ${stats.clusterCount} merged groups were raised by more than one persona.

### Merged groups

${clusterBlock}

## Findings the merge pass judged to stand alone

These are not leftovers and not lower priority — most were raised once because only one
persona was looking for them. A single-persona finding can still be the most important
thing in the review.

${stats.unclusteredFindings} findings:

${singleBlock}`
    : `## What has already been done for you, in code

⚠️ **The semantic merge pass did not run, so this is the degraded fallback.** Findings that
share a category AND a file anchor have been grouped. On measured data that rule recovers
about 5% of real cross-persona agreement, so **the grouping below is close to meaningless
and the persona counts are a floor, not a fact.** The counting arithmetic on top of the
groups is correct; the groups themselves are not to be trusted. Merge across them freely.

${stats.multiPersonaClusters} of ${stats.clusterCount} anchor clusters were raised by more than one persona.

### Anchor clusters (unreliable — see above)

${clusterBlock}

## Findings with no file anchor — nothing has been deduplicated here

${stats.unclusteredFindings} findings:

${singleBlock}`

  return `You are synthesising an independent multi-persona review into one ranked
ship-readiness list.

## Subject

${subject}

## Personas run

${personaKeys.join(', ')} — each as a separate agent, with no visibility of the others.

${mergeSection}

## Produce

One ranked list, ordered by what blocks shipping first — not by severity label, and not by
which section above an item came from. Where you merge or split anything further, say so.

For each item:
- the finding, stated plainly
- which persona(s) raised it — for merged items, all of them, because independent
  agreement is the strongest signal in this whole review
- the evidence
- what fixing it involves

Where personas disagree, say so rather than averaging them. The disagreement is usually
the interesting part.

End with a plain statement of ship-readiness. Not a score.`
}

// ---------------------------------------------------------------------------
// Argument validation
//
// Fail loudly on bad input rather than defaulting. A review launched with no evidence is
// exactly the failure SKILL.md Step 1 exists to prevent — the personas would read code
// instead of output and produce confident, useless findings.
// ---------------------------------------------------------------------------

function parseArgs(raw) {
  const a = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}

  const subject = typeof a.subject === 'string' ? a.subject.trim() : ''
  if (!subject) {
    throw new Error(
      'progress-review: `subject` is required — say what is being reviewed (the whole app, ' +
        'one report, one flow). Pass it via the Workflow tool\'s `args`.'
    )
  }

  const evidenceRaw = Array.isArray(a.evidence) ? a.evidence : a.evidence ? [a.evidence] : []
  const evidence = evidenceRaw
    .filter((e) => typeof e === 'string')
    .map((e) => e.trim())
    .filter(Boolean)
  if (evidence.length === 0) {
    throw new Error(
      'progress-review: `evidence` is required — at least one URL, file path, report ID or ' +
        'harness output for the personas to work from. A review with no evidence reads code ' +
        'instead of output and produces confident, useless findings.'
    )
  }

  const personas = resolvePersonas(a.personas !== undefined ? a.personas : a.personaSet)
  const params = a.params && typeof a.params === 'object' && !Array.isArray(a.params) ? a.params : {}
  if (typeof a.domain === 'string' && a.domain.trim() && params.DOMAIN === undefined) {
    params.DOMAIN = a.domain.trim()
  }

  return { subject, evidence, personas, params }
}

// ---------------------------------------------------------------------------
// The graph
// ---------------------------------------------------------------------------

const { subject, evidence, personas, params } = parseArgs(typeof args !== 'undefined' ? args : undefined)
const personaKeys = personas.map((p) => p.key)

log(`Reviewing: ${subject}`)
log(`${personas.length} personas (${personaKeys.join(', ')}) over ${evidence.length} evidence item(s)`)

// Fan-out. A barrier is genuinely required after this: cross-persona agreement cannot be
// counted until every persona has reported, and the ranking depends on that count. This is
// the case the Workflow docs call out as a legitimate barrier, not a lazy one.
const settled = await parallel(
  personas.map((persona) => () =>
    agent(personaPrompt(persona, params, subject, evidence), {
      label: `persona:${persona.key}`,
      phase: 'Personas',
      schema: PERSONA_REPORT_SCHEMA,
    })
  )
)

// parallel() preserves input order and resolves a failed thunk to null, so index
// correlation is what tells us WHICH persona was lost. The report's own `persona` field
// is model-supplied and cannot be trusted to identify the agent that produced it.
const reports = []
const failedPersonas = []
settled.forEach((result, i) => {
  if (result) reports.push(result)
  else failedPersonas.push(personas[i].key)
})

if (failedPersonas.length > 0) {
  // Named, not just counted. A review missing its security persona looks exactly like a
  // review that found no security problems, and the reader has to be able to tell those
  // apart before trusting the ship-readiness call.
  log(
    `WARNING: ${failedPersonas.length} of ${personas.length} persona agents failed — ` +
      `${failedPersonas.join(', ')}. Those lenses are MISSING from this review, not clean.`
  )
}

if (reports.length === 0) {
  // Explicit no-data path. Handing an empty set to the synthesis agent would produce a
  // plausible-looking ranked list built from nothing, which is worse than an error.
  throw new Error(
    'progress-review: every persona agent failed — no reports to synthesise. Nothing was ' +
      'reviewed. Check the evidence paths are reachable and re-run.'
  )
}

// The persona keys for the reports that actually came back, index-aligned with `reports`.
// Built from the registry rather than from each report's own `persona` field, which is
// model-supplied and drifts — see flattenReports.
const reportPersonaKeys = personas.map((p) => p.key).filter((k) => !failedPersonas.includes(k))

// The merge node. One agent, reading every finding, deciding only which are the same
// finding. It gets the flattened list built exactly as clusterFindings will rebuild it,
// so the ids it is shown are the ids it is answering about.
const flatForMerge = flattenReports(reports, reportPersonaKeys)
const grouping = await agent(mergePrompt(subject, flatForMerge), {
  label: 'merge',
  phase: 'Merge',
  schema: FINDING_GROUPS_SCHEMA,
})

if (!grouping) {
  // Degraded, not fatal. The anchor path still runs and the synthesis prompt says plainly
  // that its groups are ~5% recall and not to be trusted — which is the honest version of
  // what this workflow shipped with before the merge node existed.
  log(
    'WARNING: the merge agent failed. Falling back to anchor clustering, which recovers ' +
      'only ~5% of real cross-persona agreement. Persona counts below are a FLOOR, not a ' +
      'fact, and the synthesis prompt says so.'
  )
}

const merged = clusterFindings(reports, {
  personaKeys: reportPersonaKeys,
  groups: grouping ? grouping.groups : undefined,
})

log(
  `${merged.stats.totalFindings} findings from ${reports.length} personas ` +
    `(${merged.stats.mergeMode} merge): ${merged.stats.clusterCount} merged group(s) ` +
    `covering ${merged.stats.clusteredFindings} findings, ` +
    `${merged.stats.multiPersonaClusters} with more than one persona, ` +
    `${merged.stats.unclusteredFindings} standing alone`
)
if (merged.stats.unknownIdsFromMerge) {
  log(
    `NOTE: the merge agent referenced ${merged.stats.unknownIdsFromMerge} finding id(s) that ` +
      `do not exist; they were ignored. No finding was dropped.`
  )
}

const synthesis = await agent(synthesisPrompt(subject, merged, personaKeys), {
  label: 'synthesis',
  phase: 'Synthesis',
  model: 'opus',
})

if (!synthesis) {
  // The persona work is still worth returning — the clusters and counts are the expensive
  // part and they are intact. But a caller reading `synthesis` must not mistake a failed
  // agent for "nothing to report", so it is flagged rather than left as a bare null.
  log(
    'WARNING: the synthesis agent failed. The persona findings below are complete and ' +
      'usable, but they have NOT been ranked or cross-merged. Re-run to get the ranked list.'
  )
}

// Return the structured result, not just the prose. SKILL.md's note that findings must go
// somewhere durable — resume-state memory, or issues — is the reason: a caller can persist
// the clusters and counts, and a review whose output exists only in a transcript has been
// paid for twice.
return {
  subject,
  personas: personaKeys,
  personasFailed: failedPersonas,
  mergeFailed: !grouping,
  synthesisFailed: !synthesis,
  stats: merged.stats,
  clusters: merged.clusters,
  unclustered: merged.unclustered,
  synthesis: synthesis || null,
}
