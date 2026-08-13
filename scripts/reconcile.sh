#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
ENV_FILE=${AUTOSWE_ENV_FILE:-"$PROJECT_ROOT/.env"}
test -f "$ENV_FILE"
. "$ENV_FILE"
export COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-autoswe}
docker compose --env-file "$ENV_FILE" run --rm api python -m infrastructure.reconcile
