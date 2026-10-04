#!/usr/bin/env bash
# Run the existing CI gates with installed, lockfile-managed dependencies.
set -euo pipefail

if [[ $# -gt 1 ]]; then
  echo "Usage: bash scripts/verify.sh [all|python|dispatcher]" >&2
  exit 2
fi
verify_scope=${1:-all}
case "$verify_scope" in
  all|python|dispatcher) ;;
  *) echo "Usage: bash scripts/verify.sh [all|python|dispatcher]" >&2; exit 2 ;;
esac

cd "$(dirname "${BASH_SOURCE[0]}")/.."
printf 'Verifying commit %s (%s)\n' "$(git rev-parse HEAD)" "$verify_scope"

run() {
  printf '+ '
  printf '%q ' "$@"
  printf '\n'
  "$@"
}

if [[ "$verify_scope" == all || "$verify_scope" == python ]]; then
  run .venv/bin/python --version
  run .venv/bin/ruff --version
  run .venv/bin/python scripts/check_shared_contracts.py
  run .venv/bin/python -m unittest discover -s scripts -p test_shared_contracts.py
  echo "Offline suite: excluding tests/test_r2_conditional.py (live R2 writes)."
  run env -u R2_TEST_PREFIX .venv/bin/python -m pytest -q --ignore=tests/test_r2_conditional.py
  run .venv/bin/ruff check .
  run .venv/bin/ruff format --check .
fi

if [[ "$verify_scope" == all || "$verify_scope" == dispatcher ]]; then
  cd dispatcher
  run node --version
  run npm --version
  run npm run typecheck
  run npm test
fi
