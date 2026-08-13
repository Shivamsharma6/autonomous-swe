# External UAMS integration

UAMS is the only durable cross-run memory service. AutoSWE Compose does not deploy UAMS, mount its
storage, or fall back to SQLite. Configure only:

```dotenv
AUTOSWE_UAMS_URL=http://host.docker.internal:8000
AUTOSWE_UAMS_TOKEN=
AUTOSWE_UAMS_TIMEOUT_SECONDS=15
```

The adapter expects `GET /ready`, `POST /search`, `POST /remember`, and
`GET /memory/status/{memory_id}`. Authentication uses an optional bearer token. Correlation headers
for request, run, task, graph, artifact, and memory IDs are forwarded when present.

## Recall and freshness

Recall is scoped by project and, when available, repository and baseline commit. Results must carry
memory ID, revision ID, source ID, observation/verification time, provenance, evidence hashes, and
originating run/task/attempt/message IDs. Expired knowledge is excluded. Commit-scoped knowledge is
stale when the repository baseline moves. UAMS unavailability is a visible workflow wait, never an
empty context pretending to be success.

## Promotion gate

Only distilled knowledge from a verified outcome can be promoted. The gate requires valid artifact
evidence, a successful verification command, supported semantic/episodic/procedural classification,
minimum structural/evidence/confidence quality, and no duplicate, contradiction, credential, email,
or sensitive content. Cross-project and identity/preference/security knowledge requires human
approval.

The memory ID is UUIDv5 over project, source, type, normalized content, and schema. `remember()` is
therefore idempotent. If a process dies after UAMS writes but before PostgreSQL acknowledges, retry
uses the same ID. Promotion completes only when status says the current revision equals the latest
revision and is active, indexed, and searchable.

## Failure behavior

- Request/5xx failure: set candidate and graph to `WAITING_FOR_MEMORY`; retain the error and start
  the UAMS wait timer.
- Pending index: retain UAMS memory/revision IDs and retry without creating a second memory.
- Contract/4xx failure: surface the contract error; do not silently retry as availability failure.
- Search result lacking IDs/provenance: reject it from agent context.
- Recovery: restore UAMS, verify `/ready`, and keep dispatcher running. The promotion service resumes
  the same deterministic memory operation.

Before a release, run `tests/contract/test_memory_port.py`,
`tests/integration/memory/test_uams_failure.py`, and the deterministic E2E.
