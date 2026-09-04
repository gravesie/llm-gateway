# Release

`llm-gateway` is a Python library, not a deployed service. It runs inside the processes
of the applications that import it. There is no server, no container, no database and
nothing to SSH to.

## Deviation from PROCESS.md, and why

The standing process says **merging to `main` is the deploy**, with CI pushing to a
production host. That contract assumes a deployed service. This project has no host, so
applying it literally would mean writing a deploy job against a machine that does not
exist.

The equivalent here:

- **Merging to `main` makes the code available.** CI runs lint and the full suite on every
  pull request and again on `main`.
- **Tagging a version is the release.** Consumers pin a tag. A change is not in production
  anywhere until a consuming application bumps its pin.
- **Nothing is deployed by hand, because nothing is deployed.**

The spirit holds: code flows one way, `origin/main` is the source of truth, and no change
reaches a consumer without passing the gate. Only the last mile differs.

## Everyday release process

1. Branch off `main` before editing anything: `git checkout -b feat/thing main`.
2. Build and test locally. The `pre-push` hook runs lint and the full suite on every push
   (install once with `./scripts/install-hooks.sh`).
3. Push the branch and open a pull request. CI runs lint and the full suite.
4. Merge to `main` after an explicit go. A green suite is not authorisation to merge.
5. To cut a release: `git tag -a v0.3.0 -m "..." && git push origin v0.3.0`. CI builds the
   distribution and attaches it to a GitHub Release.

## Consuming it

Applications install from the tag, never from `main`:

```
pip install "llm-gateway @ git+https://github.com/gravesie/llm-gateway.git@v0.3.0"
```

Pinning to `main` would make every merge an uncontrolled production change in
web-auditor and the SEO pipeline. Don't.

## Rolling back

Rollback is the consumer pinning the previous tag and redeploying itself. There is
nothing to roll back in this repository, which is the main advantage of the library shape
over the proxy shape it replaced.

## Secrets

Provider API keys live in each consuming application's own environment. This repository
holds `.env.example` and never a real key. There are no repository secrets beyond what CI
needs to run tests, which at present is nothing.

## Backups

No state, so nothing to back up here. The cost logs this library writes belong to the
consuming application and are covered by that application's backups.
