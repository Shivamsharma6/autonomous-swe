#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
ENV_FILE=${AUTOSWE_ENV_FILE:-"$PROJECT_ROOT/.env"}
test -f "$ENV_FILE"
. "$ENV_FILE"
export COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-autoswe}

compose() {
    docker compose --env-file "$ENV_FILE" "$@"
}

DESTINATION=${1:-"${AUTOSWE_HOST_RUNTIME_ROOT}/backups/autoswe-$(date -u +%Y%m%dT%H%M%SZ).dump"}
case "$DESTINATION" in
    /*) ;;
    *) echo "Backup destination must be an absolute path" >&2; exit 2 ;;
esac
mkdir -p "$(dirname -- "$DESTINATION")"
umask 077
TEMPORARY=$(mktemp "${DESTINATION}.tmp.XXXXXX")
trap 'rm -f "$TEMPORARY"' EXIT HUP INT TERM

compose exec -T postgres pg_dump \
    --username autoswe \
    --dbname autoswe \
    --format custom \
    --no-owner \
    --no-acl > "$TEMPORARY"
compose exec -T postgres pg_restore --list < "$TEMPORARY" > /dev/null
mv "$TEMPORARY" "$DESTINATION"
(cd "$(dirname -- "$DESTINATION")" && shasum -a 256 "$(basename -- "$DESTINATION")" > "$(basename -- "$DESTINATION").sha256")
chmod 600 "$DESTINATION" "${DESTINATION}.sha256"
trap - EXIT HUP INT TERM
echo "Verified backup: $DESTINATION"
