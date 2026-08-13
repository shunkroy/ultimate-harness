# Harness v3 staged migration plan

## Compatibility rule

All v3 state changes are additive during the compatibility window. The v2.1.1
tables remain untouched and readable. Rolling back binaries retains unknown v3
tables; it does not require a destructive down-migration.

## Ordered stages

1. **Kernel authority** — application bootstrap, provider driver bridges, typed
   execution plan/outcome, no concrete provider imports from `harness2.kernel`.
2. **State/evidence** — schema metadata, typed tasks, attempts and typed event
   bus with atomic state/event writes.
3. **Recovery** — fenced leases, checkpoints, interruption recovery and one retry
   owner.
4. **Skill Foundry** — immutable manifests and provenance, then external sandbox,
   tests, benchmarks and promotion gates.
5. **Provider intelligence** — append-only observations and locally measured
   capability scores; deterministic overrides remain authoritative.
6. **Resource Governor** — observations, policy decisions and checkpoint-before-
   pause behavior.
7. **Context v2** — dependencies, composition, semantic diff, typed operations,
   content-addressed cache and correct invalidation.
8. **Improvement loop** — candidates only; no direct core replacement.
9. **Multi-node contracts** — identities, ownership, leases, fencing and offline
   synchronization, without making network availability a core dependency.

## Migration protocol

Each schema migration has a monotonically increasing version, immutable name,
transactional additive SQL, and a recorded application timestamp. Startup
refuses a database newer than the running binary. Migration tests use a v2.1.1
fixture, repeat migration, interrupted transactions, and old-reader checks.

Before release cutover:

1. produce a consistent SQLite backup;
2. run `PRAGMA quick_check`;
3. install into a new version directory;
4. migrate and smoke-test offline;
5. atomically switch the launcher;
6. restart and verify heartbeat;
7. restore the old launcher/service on failure.

The current installer does not yet implement this cutover protocol; it remains a
required release-engineering stage rather than a claimed capability.
