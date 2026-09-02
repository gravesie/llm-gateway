#!/usr/bin/env bash
#
# Point git at the versioned hooks in scripts/git-hooks. Run once per clone:
#
#     ./scripts/install-hooks.sh
#
# This uses core.hooksPath, so the hooks live in the repo (versioned, shared)
# rather than in the un-tracked .git/hooks directory.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

git config core.hooksPath scripts/git-hooks
chmod +x scripts/git-hooks/* 2>/dev/null || true

echo "Hooks installed (core.hooksPath = scripts/git-hooks)."
echo "The pre-push gate now runs lint + the full test suite before every push."
