# Security model

## Trust boundaries

- The administrator bearer token controls the local API. The API binds to loopback by default.
- PostgreSQL is the domain authority and audit source. It has no published host port.
- Redis is untrusted as an authority; messages are accepted only with stable canonical IDs and
  duplicate-safe PostgreSQL receipts.
- Model providers and external UAMS are remote dependencies. Their responses are strict typed
  contracts and never executable source.
- The sandbox manager is the only component that can reach the restricted Docker socket proxy.
  Workers and the API do not mount the Docker socket.
- Imported source must be an existing directory below the configured import root. Mutable work
  occurs in managed Git worktrees.

## Controls

The API uses constant-time credential comparison, bounded request bodies, rate limits, explicit
CORS, no-store/security headers, scoped UUID lookups, redacted errors, and exact-call approvals.
Approval hashes bind normalized arguments, repository, baseline commit, approver, and expiry.
Changing any field invalidates authorization.

For the release commit, the bound arguments include the **worktree tree hash**: the operator
authorises a specific set of files, not a worktree name. The tree is staged into a scratch index
and its hash is re-verified inside the commit operation itself, so content that appears in the
worktree between the decision and the commit is refused rather than swept in by a blanket
`git add --all`. Content that lands in the worktree is often repository-controlled: `run_tests`
executes code from the imported repository in that same worktree while approval is pending.

The recorded approver is the authenticated principal, with any operator-supplied label appended
for readability. The label alone is not trusted for attribution.

Tool calls require a declared capability, eligible role, bounded schema, risk ceiling, replay
policy, side-effect class, timeout, and contained paths. Commit, push, pull request, infrastructure,
and deployment actions require approval. An unknown external outcome is quarantined instead of
retried.

Sandbox requests require digest-pinned images, non-root UID/GID, read-only root filesystems,
dropped capabilities, no-new-privileges, fixed argv, bounded CPU time, memory, PIDs, wall time and
output. Network is disabled by default. Source repositories are read-only; only the task worktree
is writable. Symlink and traversal escapes are rejected.

Artifacts are content-addressed by server-computed SHA-256. Retrieval never follows
user-controlled symlinks and always verifies metadata, object bytes, and hash. Corruption is
quarantined and cannot be release evidence.

Secrets are accepted only through runtime environment variables and represented as `SecretStr`.
Structured logs recursively redact credential keys and embedded bearer/token/password patterns.
Correlation IDs, not secret bodies, flow through API, model, sandbox, UAMS, message, tool, and
artifact boundaries.

## Network layout

`edge` contains loopback-published API/web endpoints. `control` and `docker-api` are internal
networks. `external-services` permits only components that need model/UAMS egress. The Docker
socket proxy exposes a restricted Engine API and is the sole socket mount.

**What "restricted" means here.** The proxy is a *denylist*: it disables whole Engine subsystems
(`/volumes`, `/build`, `/images/*/build`, `/commit`, `/swarm`, `/system`) while leaving
`POST /containers/*` reachable, because container creation is what the sandbox manager needs.
It therefore reduces blast radius by removing subsystems, but it does **not** constrain what a
container can be created as. The boundary that actually holds is `SandboxBoundary` inside the
sandbox manager, which re-derives the mount paths, image reference, and container user from the
manager's own configuration instead of trusting the request body. Treat the proxy as depth, not
as the control.

**Credential separation.** The sandbox manager authenticates with its own service credential
(`AUTOSWE_SANDBOX_MANAGER_TOKEN`), which is held only by `workers` and `sandbox-manager`. The
operator credential (`AUTOSWE_ADMIN_TOKEN`) is scoped to the API, the only published HTTP surface,
so compromising the API does not also yield the ability to create containers.

## Residual risks and operator duties

This is a single-machine deployment: host root or Docker daemon compromise defeats container
isolation. Protect the host, restrict membership in the Docker group, encrypt disks and backups,
and use a dedicated machine for untrusted repositories. Model prompt injection remains possible;
the tool gateway and sandbox are the enforcement boundary, not model obedience.

Rotate any secret that has ever entered Git history; deleting it from the current Compose file is
not sufficient. Rotate model, UAMS, tracing, and administrator tokens on personnel or provider
changes. Review pending approvals and dead letters before upgrades. Never publish PostgreSQL,
Redis, sandbox-manager, socket-proxy, Prometheus, or collector ports to untrusted networks.
