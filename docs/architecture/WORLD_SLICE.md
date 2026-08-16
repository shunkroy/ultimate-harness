# Milestone 2 — Offline World Vertical Slice (Completed)

- **Branch:** `milestone2-world-slice` (pushed, verified)
- **Base:** `native-foundation` @ `0dfb781` (M1, sealed)
- **Statuses used:** IMPLEMENTED / TESTED / DEVICE-VERIFIED (nothing claimed beyond evidence)

## What was built

A real offline world runtime, not a toy demo — world-neutral machinery that
treats the proving corpus (Overlord V1, local-only) as just another input.

| Component | Status | Evidence |
|---|---|---|
| `.hdoor` V1 package contract (manifest + canon + index + seed) | TESTED | proof_01, package unit tests |
| Deterministic source compiler (byte-identical recompile) | DEVICE-VERIFIED | proof_02 + CLI recompile diff on real text |
| Explicit provenance tags (CANON/INFERRED/...) | TESTED | proof_03, `ProvenanceTag` in harness-core |
| Source identity binding (sha256, genesis anchor) | TESTED | proof_04, store bind_canon |
| Fail-closed package validation | TESTED | proof_05 (tamper/schema/kind/corrupt/missing) |
| Offline lexical NL pipeline (no cloud, no model) | DEVICE-VERIFIED | proof_06, live CLI session on real text |
| Normalization + typo tolerance | TESTED | proof_07 (ligatures, accents, case, edit-1 typos) |
| Ambiguity fail-closed | TESTED | proof_08, runtime guards |
| World runtime actions | DEVICE-VERIFIED | proof_09, live CLI (status/inspect/go/take/drop) |
| Signed chained events (sha256 per event) | DEVICE-VERIFIED | proof_10, live ledger inspection |
| Tamper detection | TESTED | proof_11 (ChainBroken on edited event) |
| Branch isolation | TESTED | proof_12 (main vs alt, same canon anchor) |
| Canon immutability | TESTED | proof_13 (package bytes untouched by runtime) |
| Restart/resume identical state | TESTED | proof_14 (byte-identical snapshots) |
| Negative tests (canon mismatch, action guards) | TESTED | negative_* tests |

## Verification gates

- Rust: **78 tests pass** (15 core unit + 5 contracts + 42 world unit + 16 proof).
- Python baseline unchanged: 397 ran, 396 pass, 1 classified PRoot failure, 1 skip.
- `harness integrity verify`: 68 pins OK — canonical state untouched.
- Device verification: Overlord V1 prologue segment compiled (36 entities,
  4 locations, deterministic), live session with signed events + resume.
  Copyrighted material exists only at `/root/world-work` — never committed.

## Architecture decisions

- Package = canon carrier; runtime state lives only in the SQLite store under
  `worlds/<world_id>/<instance_id>/` — Python remains the state authority;
  Rust writes exclusively to these new isolated M2 structures.
- Event chain: `hash = sha256(prev‖seq‖branch‖type‖actor‖detail‖time)`,
  genesis `prev_hash` = canon manifest sha256 → canon drift is detectable.
- Dream/branch/simulation are provenance layers, never canon.
- `.hdoor` is directory-based v1; the manifest is the stable contract.

## Git / GitHub sync

- **GITHUB SYNC: VERIFIED** — 7 atomic commits pushed to
  `origin/milestone2-world-slice` @ `f5c0299`; `git ls-remote` matches HEAD,
  divergence 0/0. No force-push, no history rewrite.
- Commits: ProvenanceTag → hdoor contract → compiler/index → NL pipeline →
  store/runtime → CLI+fixture+proof → fixture name cleanup.

## Next slice (Milestone 3)

Conversational offline world loop: dialogue state, story-mode chapter
advancement, world branching applied to a second proving corpus, and
renderer-agnostic export of a world's signed history.