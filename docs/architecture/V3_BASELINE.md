# Harness v3 evidence baseline

Audit date: 2026-08-13

Audited release: `v2.1.1` (`2c517d3`)

Evidence: tracked source, tests, clean runtime checks, installer/release checks

This document separates observed behavior from design intent. A capability is
not promoted because prose or a provider claims it exists.

## CURRENT STATE

Harness v2.1.1 is a Python 3.11+ deterministic orchestration foundation. The
typed kernel contracts and registries exist, but foreground execution still
travels through legacy `RunRequest`/`RoutingDecision`/`EngineResult`, a static
adapter registry, and an 800+ line CLI composition root. SQLite persists runs,
circuits, a hash-linked metadata audit, and an encrypted legacy job queue.
Context compilation jobs use a separate JSON-file queue.

## IMPLEMENTED

- [IMPLEMENTED] Typed capability/runtime/evidence descriptors and in-memory
  registries.
- [IMPLEMENTED] Provider adapters, deterministic routing, bounded retries and
  persistent circuit breakers.
- [IMPLEMENTED] Encrypted legacy jobs with atomic SQLite claim.
- [IMPLEMENTED] Hash-linked metadata audit and installed-source integrity pins.
- [IMPLEMENTED] Deterministic Context-as-Program v1 with provenance and package
  tamper detection.
- [IMPLEMENTED] Desired-versus-observed always-active heartbeat semantics.
- [IMPLEMENTED] Exact-process Prime supervision on supported POSIX systems.

## TESTED

- [TESTED] 158 tests passed and one privileged ownership-emulation test skipped
  during the baseline audit.
- [TESTED] Kernel source has an architecture fitness test prohibiting concrete
  provider adapter imports.
- [TESTED] Context authority restrictions, package tampering, encrypted payload
  tampering, audit tampering, parser failures, retries, service heartbeat and
  fallback installation.
- [TESTED] The public v2.1.1 wheel and checksum installer on this environment.

These are local test observations, not benchmarks or stability evidence.

## PARTIAL

- [PROTOTYPED] The provider-independent kernel is descriptive rather than the
  authoritative execution path.
- [IMPLEMENTED] Capability evidence records exist, but catalog construction can
  label generic local tests without binding them to a commit, platform, or
  signed test artifact.
- [IMPLEMENTED] Provider event-stream parsers are typed in memory; there is no
  persistent typed event bus.
- [IMPLEMENTED] Legacy queued jobs are durable, but have no fencing token,
  idempotency key, lease renewal, or stale-completion rejection.
- [IMPLEMENTED] Always-active service performs bounded explicit work, but its
  queues do not share a unified task model.

## MISSING

- [DESIGNED] Authoritative `ApplicationKernel` execution boundary.
- [DESIGNED] Persistent typed task state machine, attempts, plans, checkpoints,
  dependencies and fenced leases.
- [DESIGNED] Persistent schema-versioned event bus with replay and deduplication.
- [DESIGNED] Native Skill Foundry and lower-trust skill sandbox.
- [DESIGNED] Empirical provider observations and routing scores.
- [DESIGNED] Resource/cost governor.
- [DESIGNED] Context v2 composition, semantic diff, dependency graph and cache.
- [IDEA] Bounded improvement loop and multi-node leases/fencing.
- [DESIGNED] Independent CI evidence; no workflow exists in v2.1.1.

## DUPLICATED

- Legacy and kernel capability models.
- `EngineStatus` and `RuntimeDescriptor`.
- Static adapter registry and typed runtime registry.
- SQLite run jobs and JSON context jobs.
- Retry ownership in foreground orchestration and durable jobs.
- Provider/credential maps in configuration, discovery, and CLI code.
- Service process discovery in CLI and supervisor modules.

## SECURITY RISKS

1. **Critical:** `policy.expert_for_task` dynamically imports externally mutable
   Python into the trusted process, before sensitive-task routing.
2. Durable jobs can be reclaimed after lease expiry while an old worker remains
   active; completion is not fenced.
3. The `harness-sandbox` name and provider `--pure` flag are claims, not an
   independently enforced OS sandbox.
4. Provider output is fully buffered and lacks byte/event limits.
5. Context compilation retains mutable source paths, creating submit/use TOCTOU.
6. Audit append is not serialized and has no external trust anchor.
7. Stored payload paths can become filesystem authority if the DB is corrupted.
8. PID verification and signalling are separate operations and remain exposed to
   PID-reuse races where pidfds are unavailable.

## PORTABILITY RISKS

- Native Windows can fail because POSIX-only supervisor dependencies are imported
  by the CLI composition root.
- `/proc` process identity is Linux-specific.
- The installer assumes `sha256sum` and invokes the POSIX service helper.
- macOS documentation and hardened Prime support do not fully agree.
- Most provider and platform tests are simulations on Linux, not a CI matrix.

## MIGRATION RISKS

- `CREATE TABLE IF NOT EXISTS` is initialization, not a schema migration system.
- Existing `migrations` rows describe legacy imports, not ordered DB versions.
- Positional `INSERT INTO ... VALUES` calls are fragile under column changes.
- In-place installation and service cutover lack automatic rollback.
- Context IDs do not include package version and packages can overwrite paths.

## FIRST V3 CHECKPOINT

The first checkpoint is intentionally foundational:

1. [IMPLEMENTED target] move dependency construction out of `cli.py`;
2. [IMPLEMENTED target] make one provider-independent kernel execution path
   authoritative while preserving legacy result compatibility;
3. [IMPLEMENTED target] add additive, versioned migrations;
4. [IMPLEMENTED target] add persistent typed tasks and typed events with explicit
   legal transitions, idempotency, replay ordering, and fenced attempts;
5. [TESTED target] prove old v2.1.1 tables remain readable and rollback can retain
   additive tables.

Skill generation, autonomous promotion, empirical routing, resource governance,
Context v2, distributed execution, benchmarking, and stability remain outside
this checkpoint. They must build on the task/event/evidence foundation.
