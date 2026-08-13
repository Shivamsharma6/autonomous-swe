# Repository adapters

Repository adapters translate a detected project manifest into policy-owned argument arrays. Model
output never supplies shell programs. Python and Node.js/TypeScript are supported.

## Python

Detection uses `pyproject.toml`, supported lockfiles, and conventional source/test directories. The
adapter selects dependency, lint, typecheck, targeted/full test, and build commands from declared
tool configuration. It prefers the locked environment and falls back to standard-library
`unittest` only when pytest is not a declared project dependency. Ambiguous lockfiles fail closed.

## Node.js and TypeScript

Detection uses `package.json` and exactly one supported package-manager lockfile. Commands use the
matching package manager and only allow known lifecycle scripts for install, lint, typecheck, test,
and build. Arbitrary `preinstall`, lifecycle, or model-provided shell fragments are rejected.

Both adapters discover artifacts only below the managed worktree, reject absolute/traversal paths
and symlink escapes, and return `CommandSpec.argv` tuples with explicit timeouts. Commands execute
inside the digest-pinned runner image with network disabled unless policy explicitly grants a
bounded egress profile.

## Adding an adapter

1. Implement `RepositoryAdapter` in `execution/repositories/` with deterministic detection,
   discovery, command selection, and artifact collection.
2. Represent every command as a fixed argument tuple. Never invoke a shell, interpolate model text
   into a program, or accept an unbounded lifecycle script.
3. Define supported manifests and exactly one lockfile strategy. Fail closed on ambiguity.
4. Register the adapter in `RepositoryAdapterRegistry.default()`.
5. Add a minimal locked fixture and tests for detection, source/tests, dependencies, lint,
   typecheck, targeted/full tests, build, artifacts, path escape, ambiguous lockfiles, and forbidden
   scripts.
6. Add the runner image as a required digest-pinned setting and Compose environment value.
7. Run:

   ```bash
   .venv/bin/pytest tests/unit/repositories tests/integration/sandbox tests/security/test_sandbox_escape.py -q
   .venv/bin/ruff check execution/repositories tests/unit/repositories
   .venv/bin/mypy execution/repositories
   ```

An adapter is not production-supported until the deterministic E2E or an equivalent end-to-end
fixture runs its install, verification, and artifact path through the real sandbox manager.
