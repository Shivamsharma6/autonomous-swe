#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE=${1:-"$PROJECT_ROOT/.env"}
EXAMPLE_FILE="$PROJECT_ROOT/.env.example"

if [ -e "$ENV_FILE" ]; then
    echo "Refusing to overwrite existing $ENV_FILE" >&2
    exit 1
fi

umask 077
ADMIN_TOKEN=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 32)
GRAFANA_PASSWORD=$(openssl rand -hex 32)
RUNTIME_ROOT=${AUTOSWE_RUNTIME_ROOT_OVERRIDE:-"$PROJECT_ROOT/runtime"}
HOST_UID=$(id -u)
HOST_GID=$(id -g)
mkdir -p "$RUNTIME_ROOT/artifacts" "$RUNTIME_ROOT/worktrees" "$RUNTIME_ROOT/imports" "$RUNTIME_ROOT/backups"
chmod 700 "$RUNTIME_ROOT" "$RUNTIME_ROOT/artifacts" "$RUNTIME_ROOT/worktrees" "$RUNTIME_ROOT/imports" "$RUNTIME_ROOT/backups"

AUTOSWE_GENERATED_ADMIN_TOKEN=$ADMIN_TOKEN \
AUTOSWE_GENERATED_POSTGRES_PASSWORD=$POSTGRES_PASSWORD \
AUTOSWE_GENERATED_GRAFANA_PASSWORD=$GRAFANA_PASSWORD \
AUTOSWE_GENERATED_RUNTIME_ROOT=$RUNTIME_ROOT \
AUTOSWE_GENERATED_UID=$HOST_UID \
AUTOSWE_GENERATED_GID=$HOST_GID \
python3 - "$EXAMPLE_FILE" "$ENV_FILE" <<'PY'
from pathlib import Path
import os
import sys

source = Path(sys.argv[1]).read_text()
replacements = {
    "AUTOSWE_ADMIN_TOKEN=": f"AUTOSWE_ADMIN_TOKEN={os.environ['AUTOSWE_GENERATED_ADMIN_TOKEN']}",
    "AUTOSWE_POSTGRES_PASSWORD=": (
        f"AUTOSWE_POSTGRES_PASSWORD={os.environ['AUTOSWE_GENERATED_POSTGRES_PASSWORD']}"
    ),
    "AUTOSWE_GRAFANA_ADMIN_PASSWORD=": (
        f"AUTOSWE_GRAFANA_ADMIN_PASSWORD={os.environ['AUTOSWE_GENERATED_GRAFANA_PASSWORD']}"
    ),
    "AUTOSWE_HOST_RUNTIME_ROOT=/var/lib/autoswe": (
        f"AUTOSWE_HOST_RUNTIME_ROOT={os.environ['AUTOSWE_GENERATED_RUNTIME_ROOT']}"
    ),
    "AUTOSWE_UID=65532": f"AUTOSWE_UID={os.environ['AUTOSWE_GENERATED_UID']}",
    "AUTOSWE_GID=65532": f"AUTOSWE_GID={os.environ['AUTOSWE_GENERATED_GID']}",
}
lines = [replacements.get(line, line) for line in source.splitlines()]
Path(sys.argv[2]).write_text("\n".join(lines) + "\n")
PY
chmod 600 "$ENV_FILE"
echo "Created $ENV_FILE with mode 0600 and initialized $RUNTIME_ROOT"
