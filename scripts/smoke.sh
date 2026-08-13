#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
SMOKE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/autoswe-smoke.XXXXXX")
SMOKE_ENV="$SMOKE_ROOT/.env"
SMOKE_PROJECT="autoswe-smoke-$$"

cleanup() {
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$SMOKE_ROOT"
}

compose() {
    docker compose --project-name "$SMOKE_PROJECT" --env-file "$SMOKE_ENV" "$@"
}

trap cleanup EXIT HUP INT TERM

AUTOSWE_RUNTIME_ROOT_OVERRIDE="$SMOKE_ROOT/runtime" ./scripts/init-admin-token.sh "$SMOKE_ENV"
cat >> "$SMOKE_ENV" <<EOF
AUTOSWE_API_PORT=18080
AUTOSWE_WEB_PORT=13000
EOF
. "$SMOKE_ENV"

compose build api web
compose up -d postgres redis docker-socket-proxy sandbox-manager migrations
compose up -d api dispatcher workers web

attempt=0
until curl --fail --silent "http://127.0.0.1:$AUTOSWE_API_PORT/health/ready" > /dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        compose ps
        exit 1
    fi
    sleep 2
done

curl --fail --silent \
    --header "Authorization: Bearer $AUTOSWE_ADMIN_TOKEN" \
    "http://127.0.0.1:$AUTOSWE_API_PORT/api/v1/status" > /dev/null

MARKER_ID=00000000-0000-0000-0000-000000000777
compose exec -T postgres psql --username autoswe --dbname autoswe --set ON_ERROR_STOP=1 \
    --command "INSERT INTO projects (id, name, created_at, updated_at) VALUES ('$MARKER_ID', 'restore-marker', now(), now())"
BACKUP="$SMOKE_ROOT/recovery.dump"
AUTOSWE_ENV_FILE="$SMOKE_ENV" COMPOSE_PROJECT_NAME="$SMOKE_PROJECT" \
    ./scripts/backup.sh "$BACKUP"
compose exec -T postgres psql --username autoswe --dbname autoswe --set ON_ERROR_STOP=1 \
    --command "DELETE FROM projects WHERE id = '$MARKER_ID'"
AUTOSWE_ENV_FILE="$SMOKE_ENV" COMPOSE_PROJECT_NAME="$SMOKE_PROJECT" \
    AUTOSWE_SKIP_PRE_RESTORE_BACKUP=1 ./scripts/restore.sh --confirm "$BACKUP"
RESTORED=$(compose exec -T postgres psql --username autoswe --dbname autoswe --tuples-only \
    --no-align --command "SELECT count(*) FROM projects WHERE id = '$MARKER_ID'")
test "$RESTORED" = "1"

.venv/bin/pytest tests/e2e/test_scripted_production_workflow.py -q
compose ps
echo "AutoSWE readiness, deterministic workflow, artifacts, and clean-shutdown smoke passed."
