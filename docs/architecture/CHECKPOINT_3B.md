# Checkpoint 3B — trustworthy task execution foundation

Baseline commit: `d11e766` (`3.0.0.dev1`)

## Status

- [TESTED] Canonical deeply immutable task payloads and contextual authenticated
  encrypted object references.
- [TESTED] Task descriptors persisted by hash and rejected on semantic drift.
- [TESTED] Fenced, persistent, hash-linked checkpoints with full-chain
  verification after restart.
- [TESTED] New durable compatibility jobs execute through typed fenced attempts;
  pre-upgrade unmapped jobs are quarantined rather than executed unfenced.
- [TESTED] Context jobs capture immutable source bytes at submission and compile
  only the authenticated snapshot after restart.
- [PROTOTYPED] Fail-closed sandbox capability/request/result contracts. The
  production default is disabled and no process is represented as an OS sandbox.
- [PROTOTYPED] Locally authenticated HMAC producer statements and provenance-
  backed Skill Foundry evidence gates. This proves local key possession only,
  not an external human or organizational identity.

No V3 tag, publication, provider benchmark, active generated skill, or real OS
sandbox is claimed by this checkpoint.

## Trust and recovery properties

- Payload content identity is domain-separated canonical plaintext SHA-256.
- Object references authenticate backend, key, schema, purpose, metadata and
  contextual binding before decryption.
- Payloads are recursively immutable in memory and recoverable after restart.
- Checkpoints require the current unexpired fenced attempt. Their sequence and
  every ancestor link are verified before resume.
- New legacy job submission, typed task creation, payload binding and projection
  mapping share one SQLite transaction. Claim and completion update both state
  projections atomically.
- Expired non-idempotent compatibility runs fail closed and require an explicit
  operator retry; they are not automatically reclaimed while effects may be in
  flight.
- Context source capture uses no-follow descriptor opens, bounded double reads,
  metadata comparison, exact-content hashing and authenticated storage. A v2
  context job never reopens the original source path.
- Sandbox-required skill tasks are denied unless a backend reports the required
  independently enforced OS isolation capabilities. Bootstrap reports a disabled
  backend by default.
- Promotion evidence must resolve through a configured verifier and authenticated
  provenance object for the exact manifest, evidence kind and artifact hash.

## Compatibility behavior

- Migration 4 adds only `kernel_*` tables; existing `jobs`, `audit`, `runs` and
  context files are not altered or removed.
- The legacy job mapping cascades only when an old binary deletes its owning
  `jobs` row, preserving v2.1.1 purge behavior during rollback.
- Existing queued context v1 jobs lack submission-time content identity and are
  failed with `legacy_source_snapshot_missing` instead of trusting restart-time
  path contents.
- Existing unmapped durable run jobs are failed with
  `legacy_payload_not_migrated` instead of retaining unfenced execution.
- Rollback to v2.1.1 ignores retained additive kernel tables and continues to
  read the legacy schema and audit chain.

## Verification evidence

- Full local suite: 225 tests run: 224 passed and one expected
  privilege/filesystem test was skipped.
- `compileall` and `git diff --check`: clean.
- Wheel built from the final tree:
  `harness2-3.0.0.dev1-py3-none-any.whl`, SHA-256
  `2c08047d38f337c0d62b57f2fa06f2b5cfac87ef1c4a2bd3fecbc6e008a62d27`.
- Isolated target install reported version `3.0.0.dev1` and kernel schema 4; all
  required payload, storage, sandbox and provenance modules were present.
- Installed-wheel service reached a fresh, process-verified active heartbeat and
  stopped cleanly.
- Installed-wheel integrity verification and audit-chain verification passed.
- Doctor passed while the service was active; only a non-fatal dirty Prime source
  warning remained.
- Local old→V3→old test used the public v2.1.1 wheel. Audit counts remained valid
  at 1, 2 and 3 entries; `jobs`, `audit`, and `runs` remained readable; all 15
  additive kernel tables remained retained after rollback.
- A separate rollback mutation test used v2.1.1 to purge a 3B-created legacy
  projection; the owning mapping cascaded, the legacy row was removed, and typed
  task history remained retained.

This is local integration evidence. The repository CI workflow has not supplied
independent GitHub execution evidence for Checkpoint 3B.

## Open limitations

- A production OS sandbox runner is not implemented; skill execution remains
  unavailable rather than falling back to host Python or a subprocess-only claim.
- Local HMAC provenance is a local integrity mechanism, not external identity or
  a cryptographically authenticated human/governor approval system.
- Immutable object writes precede database binding. A crash can leave an
  authenticated unreferenced object; garbage collection and retention/crypto-
  erasure policy remain future work.
- Encrypted task inputs are retained as durable history after terminal completion
  and purge unless a later retention policy removes them.
- Symlink-safe opening on platforms without an effective `O_NOFOLLOW` requires a
  platform-native no-reparse implementation before equivalent guarantees can be
  claimed there.
- Authenticated object encryption currently uses the OpenSSL-backed crypto
  primitive; native Windows requires configured OpenSSL before typed payload or
  context-snapshot writes are available (legacy DPAPI job envelopes are separate).
- Automatic migration of pre-3B jobs is deliberately not claimed because their
  original payloads have no typed immutable binding or fence authority.
