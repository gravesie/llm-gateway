// Verification for the workflow scripts in this directory. Plain Node, no dependencies:
//
//     node check-sync.js
//
// Exit 0 when everything passes, 1 otherwise.
//
// It does two jobs.
//
// 1. DRIFT. progress-review.js is self-contained — the Workflow runtime has "no
//    filesystem or Node.js API access", so a workflow script cannot require() its
//    siblings. schemas.js and personas.js stay the authoring source of truth and the
//    workflow holds an inlined copy. This asserts the copy still matches. Without it the
//    duplication is a silent trap: edit personas.js, and the workflow keeps running the
//    old briefs with nothing to say so.
//
// 2. LOGIC. clusterFindings() is the deterministic half of the migration — the grouping,
//    counting and ranking moved out of a prompt into code. It is the only part of the
//    workflow testable outside the Workflow runtime, and it is where a silent bug would
//    do the most damage: a dropped finding looks exactly like a clean review.
//
// Loading the workflow: progress-review.js is ESM with top-level await and depends on
// runtime globals (agent, parallel, log, args) that only exist inside the Workflow tool.
// So we take the declarations above the "The graph" marker, turn `export const` into
// `const`, and evaluate that. It gives us the real runtime values rather than a textual
// approximation, and it deliberately stops short of the part that would need the runtime.

'use strict'

const fs = require('fs')
const path = require('path')
const assert = require('assert')

const DIR = __dirname
const GRAPH_MARKER = '// The graph'

let failures = 0
let checks = 0

function check(name, fn) {
  checks++
  try {
    fn()
    console.log(`  PASS  ${name}`)
  } catch (err) {
    failures++
    console.log(`  FAIL  ${name}`)
    console.log(`        ${err.message.split('\n').join('\n        ')}`)
  }
}

function section(title) {
  console.log(`\n${title}`)
}

/** Load the declaration half of a workflow script, without its runtime-dependent body. */
function loadWorkflowDeclarations(file, exportNames) {
  const src = fs.readFileSync(path.join(DIR, file), 'utf8')
  const idx = src.indexOf(GRAPH_MARKER)
  if (idx === -1) {
    throw new Error(
      `${file}: expected a "${GRAPH_MARKER}" section marker separating declarations from the ` +
        `graph body. Without it this check cannot load the file.`
    )
  }
  // Trim back to the start of the comment banner that precedes the marker.
  const head = src.slice(0, src.lastIndexOf('// ---', idx))
  const cjs = head.replace(/^export const /gm, 'const ')
  // eslint-disable-next-line no-new-func -- deliberate: evaluating our own source file.
  return new Function(`${cjs}\nreturn { ${exportNames.join(', ')} };`)()
}

// ---------------------------------------------------------------------------

const schemas = require('./schemas.js')
const personasModule = require('./personas.js')
const wf = loadWorkflowDeclarations('progress-review.js', [
  'meta',
  'FINDING_SCHEMA',
  'PERSONA_REPORT_SCHEMA',
  'PERSONAS',
  'PERSONA_SETS',
  'renderBrief',
  'resolvePersonas',
  'clusterFindings',
  'flattenReports',
  'resolveGroups',
  'personaPrompt',
  'mergePrompt',
  'synthesisPrompt',
  'anchorOf',
  'severityWeight',
  'FINDING_GROUPS_SCHEMA',
])

// ---------------------------------------------------------------------------
section('Drift: progress-review.js inlined copies vs their source modules')
// ---------------------------------------------------------------------------

check('FINDING_SCHEMA matches schemas.js', () => {
  assert.deepStrictEqual(wf.FINDING_SCHEMA, schemas.FINDING_SCHEMA)
})

check('PERSONA_REPORT_SCHEMA matches schemas.js', () => {
  assert.deepStrictEqual(wf.PERSONA_REPORT_SCHEMA, schemas.PERSONA_REPORT_SCHEMA)
})

check('FINDING_GROUPS_SCHEMA matches schemas.js', () => {
  assert.deepStrictEqual(wf.FINDING_GROUPS_SCHEMA, schemas.FINDING_GROUPS_SCHEMA)
})

check('PERSONAS matches personas.js', () => {
  assert.deepStrictEqual(wf.PERSONAS, personasModule.PERSONAS)
})

check('PERSONA_SETS matches personas.js', () => {
  assert.deepStrictEqual(wf.PERSONA_SETS, personasModule.PERSONA_SETS)
})

check('renderBrief behaves identically in both', () => {
  for (const key of Object.keys(personasModule.PERSONAS)) {
    const p = personasModule.PERSONAS[key]
    assert.strictEqual(wf.renderBrief(p, {}), personasModule.renderBrief(p, {}), key)
    assert.strictEqual(
      wf.renderBrief(p, { DOMAIN: 'x' }),
      personasModule.renderBrief(p, { DOMAIN: 'x' }),
      key
    )
  }
})

// ---------------------------------------------------------------------------
section('Registry integrity')
// ---------------------------------------------------------------------------

check('every persona key matches its entry key', () => {
  for (const [key, p] of Object.entries(personasModule.PERSONAS)) {
    assert.strictEqual(p.key, key, `entry "${key}" has key "${p.key}"`)
  }
})

check('every brief is non-empty and every set resolves', () => {
  for (const [key, p] of Object.entries(personasModule.PERSONAS)) {
    assert.ok(p.brief && p.brief.trim().length > 50, `brief for "${key}" is empty or too short`)
    assert.ok(p.title && p.cares, `"${key}" is missing title or cares`)
  }
  for (const [set, keys] of Object.entries(personasModule.PERSONA_SETS)) {
    assert.ok(keys.length > 0, `set "${set}" is empty`)
    for (const k of keys) {
      assert.ok(personasModule.PERSONAS[k], `set "${set}" names unknown persona "${k}"`)
    }
  }
})

check('no placeholder survives rendering', () => {
  for (const p of Object.values(personasModule.PERSONAS)) {
    const rendered = personasModule.renderBrief(p)
    assert.ok(
      !/<[A-Z][A-Z0-9_]*\s*—\s*default:/.test(rendered),
      `unrendered placeholder left in "${p.key}"`
    )
  }
})

check('resolvePersonas rejects bad input rather than defaulting', () => {
  assert.throws(() => personasModule.resolvePersonas('nope'), /Unknown persona set/)
  assert.throws(() => personasModule.resolvePersonas(['nope']), /Unknown persona key/)
  assert.throws(() => personasModule.resolvePersonas([]), /empty/)
  assert.throws(() => personasModule.resolvePersonas(42), /must be a set name/)
})

check('resolvePersonas defaults to product and de-duplicates', () => {
  assert.deepStrictEqual(
    personasModule.resolvePersonas().map((p) => p.key),
    personasModule.PERSONA_SETS.product
  )
  assert.deepStrictEqual(
    personasModule.resolvePersonas(['designer', 'designer']).map((p) => p.key),
    ['designer']
  )
})

// ---------------------------------------------------------------------------
section('clusterFindings: the code-side merge')
// ---------------------------------------------------------------------------

const finding = (over) => ({
  claim: 'something is off',
  category: 'accuracy',
  severity: 'medium',
  evidence: 'checked by hand',
  ...over,
})

const report = (persona, over) => ({
  persona,
  wrong: [],
  missing: [],
  notGoodEnough: [],
  ...over,
})

check('two personas at the same category+file:line form ONE cluster, count 2', () => {
  const out = wf.clusterFindings([
    report('domain-expert', {
      wrong: [finding({ file: 'app/runner.py', line: 761, severity: 'high' })],
    }),
    report('senior-developer', {
      wrong: [finding({ file: 'app/runner.py', line: 761, severity: 'critical' })],
    }),
  ])
  assert.strictEqual(out.clusters.length, 1, 'expected exactly one cluster')
  assert.strictEqual(out.clusters[0].personaCount, 2)
  assert.deepStrictEqual(out.clusters[0].personas.sort(), ['domain-expert', 'senior-developer'])
  assert.strictEqual(out.clusters[0].severity, 'critical', 'cluster takes the max severity')
  assert.strictEqual(out.clusters[0].score, 4 * 2)
  assert.strictEqual(out.stats.multiPersonaClusters, 1)
})

check('path separators and case are normalised before matching', () => {
  const out = wf.clusterFindings([
    report('a', { wrong: [finding({ file: 'App\\Runner.py', line: 761 })] }),
    report('b', { wrong: [finding({ file: './app/runner.py', line: 761 })] }),
  ])
  assert.strictEqual(out.clusters.length, 1, 'backslash and ./ prefix should still match')
})

check('a different line does NOT merge', () => {
  const out = wf.clusterFindings([
    report('a', { wrong: [finding({ file: 'app/runner.py', line: 761 })] }),
    report('b', { wrong: [finding({ file: 'app/runner.py', line: 762 })] }),
  ])
  assert.strictEqual(out.clusters.length, 2)
})

check('a different category does NOT merge', () => {
  const out = wf.clusterFindings([
    report('a', { wrong: [finding({ file: 'a.py', line: 1, category: 'accuracy' })] }),
    report('b', { wrong: [finding({ file: 'a.py', line: 1, category: 'security' })] }),
  ])
  assert.strictEqual(out.clusters.length, 2)
})

check('findings with no file land in unclustered, and are NOT dropped', () => {
  const out = wf.clusterFindings([
    report('business-owner', { notGoodEnough: [finding({ claim: 'priority is unclear' })] }),
    report('designer', { notGoodEnough: [finding({ claim: 'the score panel is cramped' })] }),
  ])
  assert.strictEqual(out.clusters.length, 0)
  assert.strictEqual(out.unclustered.length, 2, 'both must survive')
  assert.strictEqual(out.stats.totalFindings, 2)
  assert.strictEqual(
    out.stats.clusteredFindings + out.stats.unclusteredFindings,
    out.stats.totalFindings,
    'no finding may be lost between the two piles'
  )
})

check('a file with no line still anchors', () => {
  const out = wf.clusterFindings([
    report('a', { wrong: [finding({ file: 'DEPLOY.md' })] }),
    report('b', { wrong: [finding({ file: 'DEPLOY.md' })] }),
  ])
  assert.strictEqual(out.clusters.length, 1)
  assert.strictEqual(out.clusters[0].line, null)
})

check('ranking is severity x persona count, highest first', () => {
  const out = wf.clusterFindings([
    // low severity, 3 personas -> 1 * 3 = 3
    report('a', { wrong: [finding({ file: 'x.py', line: 1, severity: 'low' })] }),
    report('b', { wrong: [finding({ file: 'x.py', line: 1, severity: 'low' })] }),
    report('c', { wrong: [finding({ file: 'x.py', line: 1, severity: 'low' })] }),
    // critical, 1 persona -> 4 * 1 = 4
    report('d', { wrong: [finding({ file: 'y.py', line: 1, severity: 'critical' })] }),
  ])
  assert.deepStrictEqual(
    out.clusters.map((c) => c.score),
    [4, 3],
    'critical-alone should outrank low-but-agreed at these counts'
  )
  assert.strictEqual(out.clusters[0].file, 'y.py')
})

check('agreement outranks severity when counts are high enough', () => {
  const out = wf.clusterFindings([
    report('a', { wrong: [finding({ file: 'x.py', line: 1, severity: 'high' })] }),
    report('b', { wrong: [finding({ file: 'x.py', line: 1, severity: 'high' })] }),
    report('c', { wrong: [finding({ file: 'y.py', line: 1, severity: 'critical' })] }),
  ])
  // high x 2 = 6 beats critical x 1 = 4. Independent agreement is the signal.
  assert.strictEqual(out.clusters[0].file, 'x.py')
})

check('all three buckets are flattened and the bucket is retained', () => {
  const out = wf.clusterFindings([
    report('a', {
      wrong: [finding({ claim: 'w' })],
      missing: [finding({ claim: 'm' })],
      notGoodEnough: [finding({ claim: 'n' })],
    }),
  ])
  assert.strictEqual(out.stats.totalFindings, 3)
  assert.deepStrictEqual(out.unclustered.map((f) => f.bucket).sort(), [
    'missing',
    'notGoodEnough',
    'wrong',
  ])
})

check('the same persona raising it twice counts as ONE persona', () => {
  const out = wf.clusterFindings([
    report('a', {
      wrong: [finding({ file: 'x.py', line: 1 })],
      missing: [finding({ file: 'x.py', line: 1 })],
    }),
  ])
  assert.strictEqual(out.clusters.length, 1)
  assert.strictEqual(out.clusters[0].personaCount, 1, 'agreement must be across personas, not findings')
  assert.strictEqual(out.clusters[0].findings.length, 2)
})

check('nulls and malformed reports are tolerated, not fatal', () => {
  const out = wf.clusterFindings([
    null, // a failed agent() resolves to null
    undefined,
    'not an object',
    report('a', { wrong: null }), // bucket wrong type
    report('b', { wrong: [null, finding({ file: 'x.py', line: 1 })] }),
    { persona: 'c' }, // no buckets at all
  ])
  assert.strictEqual(out.stats.totalFindings, 1)
  assert.strictEqual(out.clusters.length, 1)
  // Three of the six inputs are object-shaped: the two report() calls and the bare
  // { persona: 'c' }. null is excluded by the truthiness guard (typeof null is 'object',
  // so the guard is doing real work) and the string by the typeof check.
  assert.strictEqual(out.stats.personaReports, 3, 'counts only the object-shaped entries')
})

check('an empty input produces empty output, not a crash', () => {
  const out = wf.clusterFindings([])
  assert.deepStrictEqual(out.clusters, [])
  assert.deepStrictEqual(out.unclustered, [])
  assert.strictEqual(out.stats.totalFindings, 0)
})

check('ordering is deterministic across repeated runs', () => {
  const build = () => [
    report('a', { wrong: [finding({ file: 'b.py', line: 2, severity: 'high' })] }),
    report('b', { wrong: [finding({ file: 'a.py', line: 1, severity: 'high' })] }),
    report('c', { wrong: [finding({ file: 'c.py', line: 3, severity: 'high' })] }),
  ]
  const first = wf.clusterFindings(build()).clusters.map((c) => c.key)
  for (let i = 0; i < 5; i++) {
    assert.deepStrictEqual(wf.clusterFindings(build()).clusters.map((c) => c.key), first)
  }
})

check('an unknown severity scores 0 rather than throwing', () => {
  const out = wf.clusterFindings([
    report('a', { wrong: [finding({ file: 'x.py', line: 1, severity: 'catastrophic' })] }),
  ])
  assert.strictEqual(out.clusters.length, 1)
  assert.strictEqual(out.clusters[0].score, 0)
})

// ---------------------------------------------------------------------------
section('Semantic merge: groups from the merge agent')
//
// The anchor path above is the DEGRADED mode and its tests stay as they were. These cover
// the path that actually runs. Every check here is about the same property: a model's
// grouping may not delete, duplicate or invent a finding.
// ---------------------------------------------------------------------------

check('a semantic group merges findings that share no file at all', () => {
  const out = wf.clusterFindings(
    [
      report('domain-expert', { wrong: [finding({ claim: 'the panel undercounts signals' })] }),
      report('designer', { notGoodEnough: [finding({ claim: 'signal count in the header is wrong' })] }),
    ],
    { groups: [{ ids: ['F1', 'F2'], summary: 'the signal count is wrong', category: 'accuracy' }] }
  )
  assert.strictEqual(out.clusters.length, 1, 'the whole point: no anchor, still merged')
  assert.strictEqual(out.clusters[0].personaCount, 2)
  assert.strictEqual(out.stats.mergeMode, 'semantic')
  assert.strictEqual(out.stats.multiPersonaClusters, 1)
  assert.strictEqual(out.unclustered.length, 0)
})

check('a semantic group merges across DIFFERENT files', () => {
  const out = wf.clusterFindings(
    [
      report('a', { wrong: [finding({ file: 'app/conversion.py', line: 17 })] }),
      report('b', { wrong: [finding({ file: 'app/audits/content_strategy.py', line: 788 })] }),
    ],
    { groups: [{ ids: ['F1', 'F2'], summary: 'one predicate, two inputs' }] }
  )
  assert.strictEqual(out.clusters.length, 1, 'the anchor key could never do this')
  assert.deepStrictEqual(out.clusters[0].files, [
    'app/conversion.py',
    'app/audits/content_strategy.py',
  ], 'both anchors kept — neither persona was wrong about its layer')
})

check('findings in no group survive as singletons', () => {
  const out = wf.clusterFindings(
    [
      report('a', { wrong: [finding({ claim: 'one' }), finding({ claim: 'two' })] }),
      report('b', { wrong: [finding({ claim: 'three' })] }),
    ],
    { groups: [{ ids: ['F1', 'F3'], summary: 'merged' }] }
  )
  assert.strictEqual(out.clusters.length, 1)
  assert.strictEqual(out.unclustered.length, 1)
  assert.strictEqual(out.unclustered[0].claim, 'two')
  assert.strictEqual(
    out.stats.clusteredFindings + out.stats.unclusteredFindings,
    out.stats.totalFindings,
    'no finding may be lost between the two piles'
  )
})

check('an empty groups array is semantic mode, not a fallback to anchors', () => {
  const out = wf.clusterFindings(
    [report('a', { wrong: [finding({ file: 'x.py', line: 1 })] }),
     report('b', { wrong: [finding({ file: 'x.py', line: 1 })] })],
    { groups: [] }
  )
  assert.strictEqual(out.stats.mergeMode, 'semantic')
  assert.strictEqual(out.clusters.length, 0, 'the merge pass said these are not the same finding')
  assert.strictEqual(out.unclustered.length, 2, 'and the anchor rule must not overrule it')
})

check('omitting groups falls back to anchor clustering', () => {
  const out = wf.clusterFindings([
    report('a', { wrong: [finding({ file: 'x.py', line: 1 })] }),
    report('b', { wrong: [finding({ file: 'x.py', line: 1 })] }),
  ])
  assert.strictEqual(out.stats.mergeMode, 'anchor')
  assert.strictEqual(out.clusters.length, 1)
})

check('an unknown id is ignored and counted, never fatal', () => {
  const out = wf.clusterFindings(
    [report('a', { wrong: [finding({ claim: 'one' })] }),
     report('b', { wrong: [finding({ claim: 'two' })] })],
    { groups: [{ ids: ['F1', 'F2', 'F99', 'nonsense'], summary: 's' }] }
  )
  assert.strictEqual(out.clusters.length, 1)
  assert.strictEqual(out.clusters[0].findings.length, 2)
  assert.strictEqual(out.stats.unknownIdsFromMerge, 2)
})

check('an id claimed by two groups joins the first only', () => {
  const out = wf.clusterFindings(
    [report('a', { wrong: [finding({ claim: 'one' }), finding({ claim: 'two' })] }),
     report('b', { wrong: [finding({ claim: 'three' })] })],
    {
      groups: [
        { ids: ['F1', 'F2'], summary: 'first' },
        { ids: ['F2', 'F3'], summary: 'second' },
      ],
    }
  )
  const total = out.clusters.reduce((n, c) => n + c.findings.length, 0) + out.unclustered.length
  assert.strictEqual(total, 3, 'a finding must not be counted into two agreement scores')
  assert.strictEqual(out.clusters[0].findings.length, 2)
})

check('a group that resolves to one finding collapses to a singleton', () => {
  const out = wf.clusterFindings(
    [report('a', { wrong: [finding({ claim: 'one' })] })],
    { groups: [{ ids: ['F1', 'F404'], summary: 'claims agreement it does not have' }] }
  )
  assert.strictEqual(out.clusters.length, 0, 'one finding is not agreement')
  assert.strictEqual(out.unclustered.length, 1)
})

check('malformed group entries are skipped, not fatal', () => {
  const out = wf.clusterFindings(
    [report('a', { wrong: [finding({ claim: 'one' })] }),
     report('b', { wrong: [finding({ claim: 'two' })] })],
    { groups: [null, 'nope', {}, { ids: 'F1' }, { ids: ['F1', 'F2'], summary: 'ok' }] }
  )
  assert.strictEqual(out.clusters.length, 1)
  assert.strictEqual(out.stats.totalFindings, 2)
})

check('personaKeys override the model-supplied persona field', () => {
  // The 2026-08-30 run: 3 of 4 agents renamed themselves in their own report.
  const out = wf.clusterFindings(
    [report('web-developer-actioning-the-fixes', { wrong: [finding({ claim: 'x' })] })],
    { personaKeys: ['implementer'], groups: [] }
  )
  assert.strictEqual(out.unclustered[0].persona, 'implementer')
})

check('flattenReports assigns stable sequential ids across reports and buckets', () => {
  const flat = wf.flattenReports([
    report('a', { wrong: [finding({ claim: 'w' })], missing: [finding({ claim: 'm' })] }),
    report('b', { notGoodEnough: [finding({ claim: 'n' })] }),
  ])
  assert.deepStrictEqual(flat.map((f) => f.id), ['F1', 'F2', 'F3'])
  assert.deepStrictEqual(flat.map((f) => f.claim), ['w', 'm', 'n'])
})

check('ranking still uses severity x persona count on semantic groups', () => {
  const out = wf.clusterFindings(
    [
      report('a', { wrong: [finding({ claim: 'p', severity: 'high' })] }),
      report('b', { wrong: [finding({ claim: 'q', severity: 'high' })] }),
      report('c', { wrong: [finding({ claim: 'r', severity: 'critical' })] }),
    ],
    { groups: [{ ids: ['F1', 'F2'], summary: 'agreed' }] }
  )
  // high x 2 = 6 beats critical x 1 = 4; the arithmetic is unchanged by the new grouping.
  assert.strictEqual(out.clusters[0].score, 6)
  assert.strictEqual(out.clusters[0].personaCount, 2)
})

check('mergePrompt lists every finding by id and forbids grouping on file path', () => {
  const flat = wf.flattenReports([
    report('a', { wrong: [finding({ claim: 'first', file: 'x.py', line: 3 })] }),
    report('b', { missing: [finding({ claim: 'second' })] }),
  ])
  const out = wf.mergePrompt('Subject', flat)
  assert.ok(out.includes('F1.'), 'F1 not listed')
  assert.ok(out.includes('F2.'), 'F2 not listed')
  assert.ok(out.includes('(x.py:3)'), 'anchor not shown for the finding that has one')
  assert.ok(out.includes('Do not group on the file path'), 'the load-bearing instruction is missing')
  assert.ok(out.includes('one fix clears every member'), 'the same-finding test is missing')
})

check('synthesisPrompt in semantic mode does not claim the grouping is arithmetic', () => {
  const merged = wf.clusterFindings(
    [report('a', { wrong: [finding({ claim: 'one' })] }),
     report('b', { wrong: [finding({ claim: 'two' })] })],
    { groups: [{ ids: ['F1', 'F2'], summary: 'the same defect' }] }
  )
  const out = wf.synthesisPrompt('Subject', merged, ['a', 'b'])
  assert.ok(out.includes('The counting and the ranking are arithmetic'), 'counting claim missing')
  assert.ok(
    out.includes('The grouping is a judgement, not arithmetic'),
    'the synthesiser must be told it may re-merge — it had to override the old prompt to do its job'
  )
  assert.ok(out.includes('the same defect'), 'merge summary not passed through')
  assert.ok(!out.includes('do not second-guess the counts'), 'the old absolute claim must be gone')
})

check('synthesisPrompt in anchor mode warns that the grouping is unreliable', () => {
  const merged = wf.clusterFindings([
    report('a', { wrong: [finding({ file: 'x.py', line: 1 })] }),
  ])
  const out = wf.synthesisPrompt('Subject', merged, ['a'])
  assert.ok(out.includes('degraded fallback'), 'fallback not flagged to the synthesiser')
  assert.ok(out.includes('about 5%'), 'the measured recall must be stated, not implied')
})

check('a single-persona finding is not described as a leftover', () => {
  const merged = wf.clusterFindings(
    [report('a', { wrong: [finding({ claim: 'only I saw this' })] })],
    { groups: [] }
  )
  const out = wf.synthesisPrompt('Subject', merged, ['a'])
  // The ASB gate lost a critical single-persona finding (four sold-out homepage products).
  // The prompt must not invite the synthesiser to treat singletons as low priority.
  assert.ok(out.includes('not leftovers and not lower priority'), 'singleton framing missing')
})

// ---------------------------------------------------------------------------
section('Prompt construction')
//
// synthesisPrompt runs AFTER the persona fan-out. A throw there wastes every persona
// run that just completed, so the edge cases are worth covering even though they look
// like formatting.
// ---------------------------------------------------------------------------

check('personaPrompt renders the brief, subject and evidence', () => {
  const out = wf.personaPrompt(
    personasModule.PERSONAS['domain-expert'],
    { DOMAIN: 'antique retail' },
    'The ASB report',
    ['http://localhost:8000/report/1', 'scripts/acceptance_asb.py']
  )
  assert.ok(out.includes('senior antique retail expert'), 'placeholder not substituted')
  assert.ok(out.includes('The ASB report'), 'subject missing')
  assert.ok(out.includes('1. http://localhost:8000/report/1'), 'evidence not numbered')
  assert.ok(out.includes('2. scripts/acceptance_asb.py'), 'second evidence item missing')
})

check('synthesisPrompt survives an all-unclustered result', () => {
  const merged = wf.clusterFindings([
    report('business-owner', { notGoodEnough: [finding({ claim: 'priority unclear' })] }),
  ])
  const out = wf.synthesisPrompt('Subject', merged, ['business-owner'])
  assert.ok(out.includes('no findings carried a file anchor'), 'empty-cluster wording missing')
  assert.ok(out.includes('priority unclear'), 'unclustered finding not passed through')
})

check('synthesisPrompt survives an all-clustered result', () => {
  const merged = wf.clusterFindings([
    report('a', { wrong: [finding({ file: 'x.py', line: 1 })] }),
  ])
  const out = wf.synthesisPrompt('Subject', merged, ['a'])
  assert.ok(out.includes('C1 —'), 'cluster not rendered')
  assert.ok(out.includes('(none)'), 'empty unclustered block should read "(none)"')
})

check('synthesisPrompt handles a finding with no evidence', () => {
  const merged = wf.clusterFindings([
    report('a', { wrong: [{ claim: 'c', category: 'accuracy', severity: 'low' }] }),
  ])
  const out = wf.synthesisPrompt('Subject', merged, ['a'])
  assert.ok(out.includes('(none given)'), 'missing evidence should be marked, not blank')
})

// ---------------------------------------------------------------------------
section('meta contract')
// ---------------------------------------------------------------------------

check('meta has the required literal fields', () => {
  assert.ok(wf.meta.name, 'meta.name required')
  assert.ok(wf.meta.description, 'meta.description required')
  assert.ok(Array.isArray(wf.meta.phases) && wf.meta.phases.length > 0)
  for (const p of wf.meta.phases) assert.ok(p.title, 'every phase needs a title')
})

check('every phase() title used in the body has a meta entry', () => {
  const src = fs.readFileSync(path.join(DIR, 'progress-review.js'), 'utf8')
  const used = new Set()
  for (const m of src.matchAll(/phase:\s*'([^']+)'/g)) used.add(m[1])
  const declared = new Set(wf.meta.phases.map((p) => p.title))
  for (const title of used) {
    assert.ok(declared.has(title), `phase "${title}" used in body but not declared in meta.phases`)
  }
})

// ---------------------------------------------------------------------------

console.log(`\n${checks - failures}/${checks} checks passed`)
if (failures > 0) {
  console.error(`${failures} FAILED`)
  process.exit(1)
}
