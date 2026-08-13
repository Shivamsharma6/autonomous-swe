# Operator guide

## Bootstrap and start

1. Confirm Docker and the external dependencies are reachable:

   ```bash
   docker version
   docker compose version
   curl --fail http://127.0.0.1:8000/ready
   ```

2. Generate local credentials and protected runtime directories:

   ```bash
   ./scripts/init-admin-token.sh
   chmod 600 .env
   ```

3. Edit `.env`. Set `AUTOSWE_UAMS_URL`, `AUTOSWE_UAMS_TOKEN`,
   `AUTOSWE_MODEL_BASE_URL`, `AUTOSWE_MODEL_API_KEY`, and `AUTOSWE_MODEL_PRIMARY`. UAMS is not a
   Compose service. When UAMS runs on the host, use `host.docker.internal`, not `localhost`.

4. Import a Git repository below the configured import root. The runtime root is created by the
   initialization script:

   ```bash
   git clone /path/to/source runtime/imports/my-saas
   git -C runtime/imports/my-saas rev-parse HEAD
   ```

5. Start the stack:

   ```bash
   docker compose build api web
   docker compose up -d postgres redis docker-socket-proxy sandbox-manager migrations
   docker compose up -d api dispatcher workers web
   docker compose ps
   curl --fail http://127.0.0.1:8080/health/ready
   ```

Readiness fails visibly when PostgreSQL, Redis, checkpoint tables, the sandbox manager, model
configuration, or UAMS is unavailable. Liveness only confirms that the API process is alive.

## Submit and observe a run

Load the generated token without printing it:

```bash
set -a
. ./.env
set +a
BASE=http://127.0.0.1:8080/api/v1
AUTH="Authorization: Bearer $AUTOSWE_ADMIN_TOKEN"
```

Register the imported source and retain the returned IDs:

```bash
curl --fail --header "$AUTH" --header 'Content-Type: application/json' \
  --data '{"name":"my-saas","source_path":"/absolute/path/to/runtime/imports/my-saas","default_branch":"main"}' \
  "$BASE/projects"
```

Submit a run using those IDs and the exact baseline from `git rev-parse HEAD`:

```bash
curl --fail --header "$AUTH" --header 'Content-Type: application/json' \
  --data '{"project_id":"PROJECT_UUID","repository_id":"REPOSITORY_UUID","goal":"Build the requested feature with tests","baseline_commit":"40_HEX_COMMIT"}' \
  "$BASE/runs"
```

Inspect progress using `GET /runs/{run_id}`, `/tasks`, `/approvals`, `/artifacts`, and `/events`.
The run response includes `state_duration_seconds`; the dashboard exposes time in every task,
workflow, approval, and UAMS state.

Approval decisions must echo the exact `call_hash` returned by the approval listing:

```bash
curl --fail --header "$AUTH" --header 'Content-Type: application/json' \
  --data '{"approved":true,"approver":"operator","expected_call_hash":"64_HEX_HASH"}' \
  "$BASE/approvals/APPROVAL_UUID/decision"
```

## Routine operations

```bash
./scripts/migrate.sh
./scripts/backup.sh
./scripts/reconcile.sh
docker compose logs --since 15m api dispatcher workers sandbox-manager
docker compose restart dispatcher workers
```

Restore is intentionally explicit and disruptive:

```bash
./scripts/restore.sh --confirm /absolute/path/to/autoswe.dump
```

The restore command verifies the SHA-256 sidecar and dump catalog, creates a pre-restore safety
backup, stops writers, restores with `--exit-on-error`, reruns migrations, and restarts services.

## Observability

Start the optional monitoring overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

Prometheus and the OpenTelemetry collector are internal. Grafana is bound to the configured local
port and uses the generated admin password. The reliability dashboard covers state age, queue
blocking, reservations versus actual use, delivery latency/dead letters, UAMS waits, sandbox
resources, artifact integrity, and SLO burn rate.

## Upgrade and shutdown

Always take a verified backup before changing image digests or schema versions:

```bash
./scripts/backup.sh
docker compose build api web
./scripts/migrate.sh
docker compose up -d
curl --fail http://127.0.0.1:8080/health/ready
```

Graceful shutdown preserves all PostgreSQL state and volumes:

```bash
docker compose stop
```

`docker compose down --volumes` deletes PostgreSQL and Redis volumes. Use it only for disposable
environments and only after verifying a restorable backup.
