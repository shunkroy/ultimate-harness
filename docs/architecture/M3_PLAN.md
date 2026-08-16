# Milestone 3 — Conversation + Native Foundation (Plan)

Branch: `milestone3-conversation-native-foundation` (from sealed `d2a2159`).
All statuses below use the exact vocabulary: DOCUMENTED / DESIGNED /
IMPLEMENTED / TESTED / DEVICE-VERIFIED / SEALED.

## 1. Inventory (actual code, verified)

| Crate | Files | Role |
|---|---|---|
| harness-core | 8 files (~1.3k LOC) | sealed v1 types, engine contract, state root, status |
| harness-world | 10 src files (~3.1k LOC) | .hdoor V1, compiler, index, pipeline, store, runtime |
| harness-native | main.rs | CLI: status/session/runtime/engine + world compile/open/list |

Rust gate: 78 tests (15 core + 5 contracts + 42 world + 16 proof). Python
baseline: 397 ran / 396 pass / 1 classified / 1 skip. Integrity: 68 pins.

## 2. What stays Rust, what is new

- The conversational world loop is **all harness-world Rust** — no Python.
- New crates (incremental, no destructive restructure):
  - `harness-ffi` — thin C ABI over harness-world (opaque handles, error
    buffers, no panics across the boundary). DESIGNED now, IMPLEMENTED this
    milestone at minimum scope.
  - `harness-lowlevel` — capability-governed machine-access layer. This
    milestone: the governance flow (request → policy → adapter → op →
    audit) + safe adapters (CPU feature detection, endianness). Unsafe
    adapters are DESIGNED, not implemented.
- Python keeps: AI/ML integrations, corpus tools, evaluation, scripts.

## 3. M3 vertical slice (in order)

1. Knowledge boundaries — `knowledge.rs`: per-character knowledge with
   sources (canon fact / witnessed / learned / rumor / player-supplied /
   inferred), stored as events, never canon mutation. Characters are not
   omniscient by default.
2. Conversational dialogue — persistent dialogue state (interlocutor,
   topic history) in branch kv; Talk intent rework: target character +
   topic extraction; deterministic state-aware answers labeled with
   knowledge source + provenance.
3. Story mode — chapter/event advancement as state transitions
   (story_position in kv, `advance`/`next` intent, chapter event records);
   mode switching (story/traveller/chat/watcher/replay) recorded as events.
4. Runtime branching — `branch <name>` forks current state from a
   checkpoint; fork event carries parent branch + parent hash + seq;
   provenance BRANCH_DIVERGED; new branch genesis chains from parent's
   last hash.
5. Signed-history export — renderer-neutral export schema v1 with genesis/
   source identity, world/session/branch identity, event ordering, prev/
   current hashes, provenance, actor, action, state transition, logical
   time, schema version. Independent verifier (pure function, no DB).
6. Deterministic replay — clock injection (TimeSource) so replay replays
   the recorded timeline; identical command sequence + injected time →
   identical snapshots AND identical hashes; wall-clock nondeterminism is
   explicitly recorded in history (created_at per event).
7. Second proving corpus — original synthetic sci-fi fixture
   (Station Echo) committed to testdata; same machinery, no corpus-specific
   code; tests prove generality + a distinct ambiguity case.
8. FFI boundary — small stable C ABI: compile/open/act/export/close/
   version; catch_unwind at the boundary; ABI tests + device proof via a
   C caller compiled on-device.
9. Low-level layer (minimum) — capability registry + policy + governor
   audit + safe platform adapter (CPU feature detection via std::arch /
   /proc/cpuinfo). No arbitrary memory/syscall interface. Unsafe paths
   DOCUMENTED + DESIGNED only.

## 4. Architecture decisions frozen in this milestone

- Chain formula stays M2-compatible: hash = sha256(prev‖seq‖branch‖type‖
  actor‖detail‖created_at). Determinism via injected clock, nondeterminism
  recorded in history.
- Export is renderer-neutral JSON v1; schema stays stable; binary
  representations are DESIGNED (endian/alignment/versioning notes in
  NATIVE_SYSTEMS.md), not invented now.
- FFI boundary: C ABI preferred (Kotlin/Swift/C/Zig/WASM all bind C);
  internal Rust APIs not exposed directly.
- Security: every low-level op flows through capability check → policy →
  governor → adapter → audit. World scripts/untrusted packages never get
  raw access.

## 5. Completion gates

All M2 guarantees (78 tests pass, Python baseline, integrity pins,
M2 corpus, tamper rejection, canon immutability, deterministic restart) +
M3 proofs: dialogue persistence, knowledge boundaries, story advance,
fork isolation, export verification, replay identity, second corpus,
FFI smoke, audit trail. Git: atomic commits, no force-push, push + verify
local/remote HEAD and divergence.