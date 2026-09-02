# llm-gateway

In-process LLM routing, fallback and cost instrumentation for PGGI projects.

A Python library, imported by the applications that spend the money. No server, no
database, nothing deployed. See `DEPLOY.md` for why, and `docs/decisions.md` for the
research behind it.

## What it is for

To answer, with evidence, where a monthly LLM bill actually goes, and then to cut it:
cheap models first with escalation, prompt caching, batch submission where latency
allows, and a hard budget ceiling.

## Consumers

- **web-auditor** (Hetzner) — page content sent to Claude during an audit.
- **moto SEO pipeline** — bulk catalogue work.
- Local scripts on pete24.

## Install

```
pip install "llm-gateway @ git+https://github.com/gravesie/llm-gateway.git@v0.1.0"
```

Always a tag, never `main`. `DEPLOY.md` explains why.

## Development

```
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -e ".[dev]"
./scripts/install-hooks.sh      # once per clone
python -m pytest
```

`CLAUDE.md` holds the rules a session must follow. Read it before changing anything.
