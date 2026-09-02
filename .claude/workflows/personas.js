// Persona registry for review workflows (Phase 2).
//
// Data, not orchestration. Every persona is one entry; every named set is an array of
// keys. Selection is code — `progress-review.js` takes a set name or an explicit list
// today, and Phase 3's `reviewScope()` will pick from this same registry based on the
// shape of the change. That is the point of separating them: adding a persona is a data
// edit here, and the routing that selects it is written once, elsewhere.
//
// This mirrors `app/llm_purposes.py` in web-auditor — a single source of truth for what
// every model call is for.
//
// ---------------------------------------------------------------------------
// DRIFT WARNING
//
// These briefs are copied verbatim from ~/.claude/skills/progress-review/SKILL.md.
// Phase 2 must not edit that file: ~/.claude has no history to restore from, so the
// migration's rollback story for the existing skill is "stop invoking the workflow",
// which only holds if the skill is untouched. So there are two copies for now.
//
// This registry is authoritative going forward. SKILL.md is frozen until the later
// phase that rewrites it into a thin entry point calling this workflow — at which
// point the duplication goes away. Until then, edit here, not there.
// ---------------------------------------------------------------------------
//
// MAINTENANCE RULES — this file is the thing that grows, so it needs a bar for entry:
//
// 1. A persona earns its place only by catching a failure mode no existing persona
//    catches. If two personas would flag the same things, they are one persona with a
//    longer brief. ("CRO" would earn entry: it catches "this page is clear and accurate
//    and still doesn't convert", which none of the seven below would raise.)
//
// 2. Every brief must state what the persona does NOT care about. That negative space
//    is what stops independent personas converging into copies of one generic reviewer
//    — and the independence is the whole reason for paying for a separate agent each.
//
// 3. Briefs stay product-agnostic. Product specifics arrive as evidence at call time,
//    never baked into a brief. Where a brief needs a variable, use the placeholder
//    syntax `<NAME — default: value>` (see `renderBrief`) rather than forking the
//    persona. A brief that names one product cannot be reused on the next job.

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

// Named sets. `product` is the default, matching SKILL.md Step 2. Mix freely — SKILL.md
// is explicit that personas which do not apply should be dropped, because a designer
// reviewing a queue worker produces noise.
const PERSONA_SETS = {
  product: ['domain-expert', 'implementer', 'business-owner', 'designer'],
  engineering: ['security-red-teamer', 'senior-developer', 'ops-reliability'],
}

// Matches `<NAME — default: value>`. The em dash is U+2014, as written in SKILL.md.
const PLACEHOLDER = /<([A-Z][A-Z0-9_]*)\s*—\s*default:\s*([^>]*)>/g

/**
 * Substitute `<NAME — default: value>` placeholders in a brief.
 *
 * Unknown placeholders fall back to their declared default rather than throwing, so a
 * caller that does not know a persona is parameterised still gets a usable brief. That
 * is deliberate: the default is written into the brief precisely so it can stand alone.
 *
 * @param {object} persona  Entry from PERSONAS.
 * @param {object} [params] Overrides, keyed by placeholder name, e.g. { DOMAIN: 'SEO' }.
 * @returns {string} The brief with every placeholder resolved.
 */
function renderBrief(persona, params = {}) {
  if (!persona || typeof persona.brief !== 'string') {
    throw new TypeError('renderBrief: expected a persona object with a string brief')
  }
  if (params === null || typeof params !== 'object' || Array.isArray(params)) {
    throw new TypeError('renderBrief: params must be a plain object')
  }
  return persona.brief.replace(PLACEHOLDER, (_match, name, fallback) => {
    const override = params[name]
    return override === undefined || override === null ? fallback.trim() : String(override)
  })
}

/**
 * Resolve a persona selector to an ordered, de-duplicated list of persona objects.
 *
 * Accepts a set name ('product'), an array of persona keys, or nothing (defaults to
 * 'product'). Throws on anything unrecognised rather than silently reviewing with the
 * wrong lenses — a review that quietly dropped its security persona would look like a
 * clean result.
 *
 * @param {string|string[]} [selector]
 * @returns {object[]} Persona entries, in the order given.
 */
function resolvePersonas(selector = 'product') {
  let keys

  if (typeof selector === 'string') {
    keys = PERSONA_SETS[selector]
    if (!keys) {
      throw new Error(
        `Unknown persona set "${selector}". Known sets: ${Object.keys(PERSONA_SETS).join(', ')}. ` +
          `To use an explicit list, pass an array of persona keys instead.`
      )
    }
  } else if (Array.isArray(selector)) {
    if (selector.length === 0) {
      throw new Error('Persona list is empty — a review with no personas produces nothing.')
    }
    keys = selector
  } else {
    throw new TypeError(
      `Persona selector must be a set name or an array of persona keys, got ${typeof selector}.`
    )
  }

  const unknown = keys.filter((k) => !PERSONAS[k])
  if (unknown.length) {
    throw new Error(
      `Unknown persona key(s): ${unknown.join(', ')}. ` +
        `Known personas: ${Object.keys(PERSONAS).join(', ')}.`
    )
  }

  // De-duplicate while preserving order: running one persona twice doubles the spend and
  // fabricates cross-persona agreement, which is the signal the merge stage ranks on.
  return [...new Set(keys)].map((k) => PERSONAS[k])
}

module.exports = {
  PERSONAS,
  PERSONA_SETS,
  renderBrief,
  resolvePersonas,
}
