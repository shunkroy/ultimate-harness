# Checkpoint 3A — kernel authority and persistent lifecycle foundation

## STATUS

- [TESTED] Ordered additive kernel migrations.
- [TESTED] Persistent typed operational events with replay, deduplication and
  consumer cursors.
- [TESTED] Persistent typed task state machine, attempts, fenced leases,
  expiration recovery, cancellation and explicit legal transitions.
- [TESTED] Provider-neutral `ApplicationKernel` with one authoritative explicit-
  provider foreground slice.
- [TESTED] Application bootstrap outside `cli.py`; CLI remains a frontend for
  new `task` and `events` command families.
- [TESTED] In-process dynamic external Python routing removed.
- [PROTOTYPED] Native Skill Foundry contracts and evidence gates.
- [PROTOTYPED] Empirical provider-observation aggregation and deterministic
  resource-observation policy.
- [DESIGNED] Skill sandbox and persistent skill repository; neither executes nor
  activates generated code in this checkpoint.

No component in this checkpoint is labeled benchmarked or stable.

Compatibility note: `HARNESS_ROUTER` no longer loads Python in-process. Existing
installations that relied on that unsafe hook must use configured default
agent/model values until the typed sandboxed-router protocol is implemented.

## FILES CHANGED

- `harness2/kernel/{application,event_bus,migrations,tasks}.py`
- `harness2/{application,bootstrap,legacy_bridge}.py`
- `harness2/store.py`, `policy.py`, `service.py`, `cli.py`
- `harness2/skills/`
- architecture and migration documentation
- architecture, migration, task, event, application-kernel and skill tests
- `.github/workflows/ci.yml`

## NEW COMPONENTS

### Kernel schema migration service

`kernel_schema_migrations` is separate from the legacy import marker table.
Migrations are immutable, checksummed, contiguous and transactional. A binary
refuses schema versions newer than it understands. Existing v2.1.1 tables are
not dropped, renamed or altered.

### Typed event bus

Events carry event/type/schema/source/task/correlation/causation identities,
canonical bounded payload and metadata, content hash, recorded/occurred times,
global replay sequence and producer deduplication key. Event payloads are
operational evidence, not a replacement claim for the hash-linked audit ledger.

### Typed task engine foundation

The state model includes:

`CREATED → PLANNED → READY → RUNNING → VERIFYING/COMPLETED`

and explicit waiting, blocked, retrying, degraded, recovering, failed and
cancelled paths. Every persisted transition has an event and transition row in
the same SQLite transaction. Attempts have monotonically increasing fencing
tokens; stale workers cannot complete after expiration, cancellation or a newer
attempt.

Only hashes and safe metadata are persisted. Raw objectives and provider output
remain outside task/event records.

### Application kernel

`ApplicationKernel` validates registered runtime health and requires test-or-
higher evidence for declared capability requirements before invoking a provider-
neutral driver. Explicit v2 compatibility runs deliberately declare no new
capability-evidence claim. Explicit provider runs use
this lifecycle and then project compatibility results into the legacy `runs`
table. Automatic fallback remains on the legacy path until a typed multi-step
execution plan can represent fallback and retry effects honestly.

### Skill Foundry foundation

Skill manifests are deterministic, content-addressed and cannot self-grant
permissions or escape their package path. Promotion to tested/benchmarked/
approved/active requires manifest-bound evidence predicates and an approval
producer distinct from the generator. Candidate code is never
imported or executed. A real OS sandbox, persistent repository, benchmark runner
and cryptographically authenticated human/governor authority remain missing.

### Provider intelligence and resources

Provider scores begin empty and are computed only from persisted local
observations; no vendor/model reputation is encoded as truth. The Resource
Governor records bounded observations and returns explicit allow/throttle/
refuse/checkpoint-and-pause decisions. It never kills running work. Battery,
thermal, network and cost acquisition remain platform/provider integrations,
not claimed active sensors in this checkpoint.

## TESTS ADDED

- Fresh and legacy migration, repeat migration, drift/newer-schema refusal and
  transactional rollback.
- Event replay order, idempotency conflict, malformed payload and monotonic
  consumer acknowledgement.
- Task idempotency, privacy, legal transitions, atomic events, concurrent claim,
  lease expiry enforced at use time, fencing, restart recovery and cancelled-
  worker completion refusal.
- Application-kernel capability validation, driver failure normalization,
  terminal replay and privacy.
- Explicit-provider compatibility bridge and staged automatic-routing fallback.
- Skill permission/path constraints and promotion evidence gates.
- Observation-only provider score aggregation and checkpoint-before-pause
  resource decisions.
- Architecture fitness checks for kernel imports, CLI composition, truthful
  heartbeat and maturity distinctions.

## SECURITY REVIEW

Improved:

- External Python router modules no longer execute in the trusted process.
- Sensitive routing returns before any optional classifier hook.
- Typed task completion is fenced.
- State transitions and operational events are atomic.
- Canonical event payloads reject NaN/non-object/oversized data.
- Raw objective and provider output are excluded from typed persistence.
- Skill candidates cannot promote themselves or self-grant authority.
- Audit append uses `BEGIN IMMEDIATE` to prevent concurrent chain forks.
- Full event identity is checked on replay collisions and consumer cursors cannot
  acknowledge beyond the current high-water mark.

Still open:

- `harness-sandbox` is not an OS sandbox.
- Provider output buffering needs hard byte/event limits.
- Legacy encrypted jobs still lack fenced completion.
- Context source submission still has a path TOCTOU window.
- SQLite and audit have no external signed trust anchor.
- Skill execution sandbox is not implemented.
- Skill evidence producer IDs are validated predicates, not yet signed
  identities; therefore Skill Foundry remains prototyped and non-executable.

## PORTABILITY REVIEW

The new kernel/task/event/skill-foundation modules use Python stdlib and SQLite
without provider or network dependencies. The unconditional `fcntl` import was
removed and start locking falls back to the existing mkdir strategy on native
Windows. Linux-specific service/process features remain gated by platform
behavior. CI now declares Python 3.11–3.13 and Linux/macOS/Windows jobs, but the
workflow must run on GitHub before it becomes observed CI evidence.

## ROLLBACK

Rollback to v2.1.1 retains all additive `kernel_*` tables. The old binary ignores
them and continues using the untouched legacy tables. No down-migration is
required. Work submitted only to the new task engine will remain preserved but
will not be processed by the old binary.

Local rollback verification installed the public v2.1.1 wheel, opened state
before and after v3 schema migration, verified the legacy audit chain, and
observed both legacy and retained kernel tables. This is local integration
evidence, not yet independent CI evidence.

## NEXT STEP

1. Add encrypted immutable task payload references and task checkpoints.
2. Migrate legacy run jobs behind fenced typed attempts with one retry owner.
3. Replace JSON context jobs with typed tasks and immutable source snapshots.
4. Implement a real lower-trust skill sandbox before skill persistence or
   activation.
5. Integrate measured provider/resource observations into typed planning only
   after the lifecycle cutover and benchmark authority are complete.
