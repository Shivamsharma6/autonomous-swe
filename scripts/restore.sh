#!/bin/sh
set -eu

if [ "${1:-}" != "--confirm" ] || [ -z "${2:-}" ]; then
    echo "Usage: $0 --confirm /absolute/path/to/autoswe.dump" >&2
    exit 2
fi

BACKUP=$2
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
ENV_FILE=${AUTOSWE_ENV_FILE:-"$PROJECT_ROOT/.env"}
test -f "$ENV_FILE"
. "$ENV_FILE"
export COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-autoswe}

compose() {
    docker compose --env-file "$ENV_FILE" "$@"
}

case "$BACKUP" in
    /*) ;;
    *) echo "Backup path must be absolute" >&2; exit 2 ;;
esac
test -f "$BACKUP"
test -f "${BACKUP}.sha256"
(cd "$(dirname -- "$BACKUP")" && shasum -a 256 -c "$(basename -- "$BACKUP").sha256")
compose exec -T postgres pg_restore --list < "$BACKUP" > /dev/null
if [ "${AUTOSWE_SKIP_PRE_RESTORE_BACKUP:-0}" != "1" ]; then
    SAFETY_BACKUP="${AUTOSWE_HOST_RUNTIME_ROOT}/backups/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).dump"
    AUTOSWE_ENV_FILE="$ENV_FILE" "$PROJECT_ROOT/scripts/backup.sh" "$SAFETY_BACKUP"
fi
compose stop api dispatcher workers sandbox-manager
compose exec -T postgres pg_restore \
    --username autoswe \
    --dbname autoswe \
    --clean \
    --if-exists \
    --exit-on-error \
    --no-owner \
    --no-acl < "$BACKUP"
compose exec -T postgres psql --username autoswe --dbname autoswe --command "SELECT 1" > /dev/null
AUTOSWE_ENV_FILE="$ENV_FILE" "$PROJECT_ROOT/scripts/migrate.sh"
compose up -d sandbox-manager api dispatcher workers web
echo "Restore verified and services restarted."
