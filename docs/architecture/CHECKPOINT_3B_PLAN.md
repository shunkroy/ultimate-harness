# Checkpoint 3B plan — trustworthy task execution foundation

Baseline commit: `df66356` (`3.0.0.dev1`)

This plan is evidence-based. It preserves Checkpoint 3A and adds one shared
immutable-reference boundary before migrating legacy and context work.

## Current blockers

1. Typed tasks persist hashes but retain no immutable recoverable input payload.
2. Checkpoints do not exist; resource policy cannot checkpoint before pausing.
3. Legacy jobs can be reclaimed and later completed by an old stale worker.
4. Context compilation queues mutable source paths and has a submit/use race.
5. Task types are free-form strings rather than registered execution contracts.
6. Skill Foundry is intentionally non-executable and has no sandbox backend.
7. Skill producer identity is descriptive, not authenticated.

## Additive implementation order

1. Canonical `TaskPayload`, `PayloadReference`, and authenticated object storage.
2. Migration 4: payload bindings, checkpoints, snapshots, task descriptors,
   legacy/context mappings, provenance observations, and local audit anchors.
3. Fenced checkpoint publication and attempt-bound event acknowledgement.
4. Typed task registry with resource, retry, side-effect, cancellation, and
   resumability declarations.
5. Legacy job compatibility projection backed by typed tasks and attempts.
6. Immutable `SourceSnapshot` and context compilation from captured bytes.
7. Fail-closed sandbox backend protocol; no unrestricted fallback.
8. Explicit skill signature/trust verification status.

## Trust model

- Payload identity is the SHA-256 of a domain-separated canonical plaintext
  envelope. Timestamps do not participate in content identity.
- Encrypted object storage authenticates task/purpose/schema binding before
  decryption. Database rows never grant arbitrary filesystem paths.
- Object publication precedes database binding. A crash can leave an unreferenced
  immutable object, never a referenced partial object.
- Checkpoints are accepted only from the currently active, unexpired fenced
  attempt and form a monotonic hash-linked sequence.
- Context compilation consumes snapshots, never the original source path.
- Skills are untrusted by default. If an OS isolation guarantee is unavailable,
  sandbox-required execution is refused.
- Local audit authentication is reported as local integrity only; no external
  trust anchor is claimed.

## Migration and rollback

Migration 4 only adds `kernel_*` tables. It does not alter, rename, or delete the
v2.1.1 `jobs`, `audit`, `runs`, or context projection files. The public v2.1.1
binary must continue opening the migrated database. New typed work remains
preserved but inactive under that old binary.

## Acceptance evidence

- Original input mutation cannot change a stored task payload.
- Stale attempts cannot checkpoint, acknowledge, complete, fail, cancel, or
  overwrite a newer result after database reopen.
- New and migrated legacy jobs complete through typed fenced attempts.
- Source replacement after submission does not affect context compilation.
- Unknown task types and malformed payload/checkpoint schemas fail closed.
- Sandbox-required skills fail closed when isolation is unavailable.
- Full prior suite remains green; wheel, service, doctor, integrity, audit, and
  old→v3→old rollback checks pass.
