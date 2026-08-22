#!/usr/bin/env bash
# Point git at the version-controlled hooks in scripts/hooks/.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath scripts/hooks
echo "core.hooksPath -> scripts/hooks"
ls -1 scripts/hooks
