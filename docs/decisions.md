# Decisions

Why this project is shaped the way it is. Each entry records what was decided, what it
rules out, and the evidence. Add to the bottom; do not rewrite history.

---

## 2026-09-02 — No subscription-credential routing, ever

**Decided:** the original idea, a router that accepts API-shaped calls and serves them
from a Claude Max or ChatGPT subscription, is abandoned. This library only ever uses
paid API credentials.

**Why:** all three major vendors prohibit it explicitly, and all three have enforced it
with account-level bans during 2026.

- Anthropic: OAuth "is intended exclusively for purchasers of Claude Free, Pro, Max, Team,
  and Enterprise subscription plans and is designed to support ordinary use of Claude Code
  and other native Anthropic applications", and developers "may not collect, store, or
  intermediate Claude.ai credentials or session tokens".
  <https://code.claude.com/docs/en/legal-and-compliance>
- Anthropic Agent SDK: "Anthropic does not allow third party developers to offer claude.ai
  login or rate limits for their products, including agents built on the Claude Agent SDK."
  <https://code.claude.com/docs/en/agent-sdk/overview>
- Anthropic Consumer ToS section 3(7): no access "through automated or non-human means,
  whether through a bot, script, or otherwise" except via an API key.
  <https://www.anthropic.com/legal/consumer-terms>
- Google: "using Gemini CLI oAuth with third-party software" named as policy-violating,
  with 403s and account bans.
  <https://github.com/google-gemini/gemini-cli/discussions/22970>
- OpenAI Terms of Use: prohibits "automatically or programmatically extract data or
  Output" and circumventing rate limits.
  <https://openai.com/policies/row-terms-of-use/>

**Rules out:** any future suggestion to reduce cost by using a subscription seat
programmatically. The saving is real and the risk is losing the account the rest of the
business runs on.

**What is permitted instead:** routing work by *type*. Interactive and agentic coding
happens inside Claude Code on the Max plan, which is ordinary individual use. Programmatic
and production traffic uses the API through this library.

---

## 2026-09-02 — A library, not a LiteLLM proxy

**Decided:** LiteLLM is used as a Python SDK inside consuming applications. No proxy
server, no Postgres, no dedicated host.

**Why:** the plan was a LiteLLM proxy on the existing Hetzner box alongside web-auditor.
A survey of that box on 2026-09-02 ruled it out:

- 3.7Gi total RAM, 2.6Gi available, **no swap**
- 38G disk, 26G used, 9.8G free (73%)
- 2 vCPU
- Postgres 17 runs as `web-auditor-db-1`, in the `web-auditor_default` bridge network

LiteLLM's own production guidance is 1 vCPU and 4Gi of memory per pod as both request and
limit, because "Prisma's query engine retains memory as a high-water mark" and going below
risks OOM kills under load. <https://docs.litellm.ai/docs/proxy/prod>

Their recommended floor for one container exceeds the whole server. With no swap, a spike
would OOM-kill by score, and the victim could as easily be a web-auditor worker as the
gateway. LiteLLM also writes a spend-log row per request, against a catalogue in the
hundreds of thousands, on a volume with 9.8G free that a live product depends on.

**Rules out:** co-tenancy on the web-auditor box. Revisit only with a separate host of at
least 8GB.

**Accepted losses:** no central dashboard, no per-project virtual keys, provider keys stay
in each consuming application's environment. Acceptable at two applications; revisit if
the number of consumers grows.

**Open:** the Python floor is set to 3.10 without confirming what web-auditor and the SEO
pipeline actually run. Check both and tighten the CI matrix to match.
