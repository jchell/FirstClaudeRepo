#!/usr/bin/env bash
# From-scratch validation: first-time setup of the whole stack, then the integration
# and browser test suites. Used by CI; safe to run locally on a machine with no
# existing stack (it creates DATAPLAT_HOME and fresh volumes).
#
#   scripts/e2e.sh            # build, init, test
#   SKIP_BUILD=1 scripts/e2e.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export DATAPLAT_HOME="${DATAPLAT_HOME:-$HOME/.dataplat}"
export COMPOSE_PROFILES="${COMPOSE_PROFILES-full}"

if [ -e "$DATAPLAT_HOME/vault-init.json" ] || [ -e "$DATAPLAT_HOME/vault-init.dpapi" ]; then
  echo "DATAPLAT_HOME=$DATAPLAT_HOME already holds a platform; point it elsewhere or run 'make reset'." >&2
  exit 1
fi

[ -n "${SKIP_BUILD:-}" ] || docker compose build
mkdir -p "$DATAPLAT_HOME"
docker compose run --rm -T bootstrap bootstrap prepare
docker compose up -d postgres minio vault redpanda oxigraph
docker compose run --rm -T bootstrap bootstrap vault
docker compose run --rm -T api migrate
DATAPLAT_ADMIN_PASSWORD="$(docker compose run --rm -T api create-admin --username admin | tail -n 1)"
export DATAPLAT_ADMIN_PASSWORD
docker compose up -d --wait --no-build

(cd backend && uv run --extra dev pytest -q -m integration)
(cd console && npx playwright test --reporter=line)
echo "End-to-end validation passed."
