# Milestone 3 — Completion Report (conversational loop + native foundation)

STATUS: SEALED candidate — branch `milestone3-conversation-native-foundation`
Base: `d2a2159` (M2 sealed). 11 commits, remote==local (`839232f`), divergence 0/0.

## Capability matrix

| # | Capability | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Offline lexical interpretation (no canned keyword matching) | IMPLEMENTED + TESTED | pipeline intents incl. Advance/Mode/Branch; topic extraction; m3_proof 01–15 |
| 2 | Persistent dialogue state (multi-turn, context-aware) | IMPLEMENTED + TESTED | interlocutor + dialogue history (cap 20); `status` shows speaker; m3_01 |
| 3 | Knowledge boundaries — no omniscience | IMPLEMENTED + TESTED | KnowledgeStore, 6 provenance sources; m3_02, m3_03, m3_12 |
| 4 | Story mode — advancement as state transitions | IMPLEMENTED + TESTED | chapter_advance events, story gate, cap at timeline; m3_05, m3_06 |
| 5 | Runtime branching with provenance | IMPLEMENTED + TESTED | fork genesis event (prev=parent tip), genesis_prev anchor, kv copy; m3_08 |
| 6 | Second proving corpus (original, synthetic) | IMPLEMENTED + TESTED | station-echo.txt; m3_12 (no-omniscience, ambiguity, where-answers, story, export) |
| 7 | Signed-history export + independent verification | IMPLEMENTED + TESTED | hdoor_export_v1 + pure verify_export (chain recompute); tamper rejection; m3_10; CLI `world export` device-verified |
| 8 | Deterministic replay | IMPLEMENTED + TESTED | injected clock, one tick per act; identical hashes + snapshot; m3_11, m3_15 |
| 9 | Restart/resume with state integrity | IMPLEMENTED + TESTED | open resumes branch, chain verifies on close; m3_01, m3_08, m3_15 |
| 10 | Canon/provenance separation, zero-cloud | IMPLEMENTED + TESTED | canon hash immutable; knowledge = branch kv only; no network |
| H | C ABI (harness-ffi) | IMPLEMENTED + TESTED | compile/open/act/export/close; ABI tests; gcc C-caller device proof PASS |
| I | Capability-governed lowlevel | IMPLEMENTED + TESTED | request→policy→governor→adapter→op→audit; unsafe path DESIGNED-only, denied at runtime |

## Gates

- Rust workspace: **106/106** tests (15 core + 5 contracts + 3 ffi + 7 lowlevel + 45 world + 15 m3_proof + 16 m2 proof).
- Python baseline: 397 ran — **3 pre-existing environment failures** (test_adapters, test_direct, test_platforms) reproduced identically on sealed base `d2a2159`; not M3 regressions. 393 passed + 1 skipped.
- Integrity: `harness-native status` — schema 5, migrations contiguous, **68 pins**.
- Device sessions: Overlord instance resumed (no regression); Station Echo full dialogue chain via CLI (go/take/where/talk/drop/status) with honest knowledge answers; `world export` produced verified hdoor_export_v1.
- FFI device proof: 10/10 CHECK PASS, `FFI PROOF: PASS` (gcc on-device, /tmp/hdoor-ffi-proof).

## Hard architecture law (frozen, commit `bbe3e2d`)

Harness = world-agnostic substrate. Kirti Realm remains Kirti's own
governed system — never absorbed, never contaminated by imports.
Crossovers are explicit governed operations with provenance and
timeline boundaries. `.hdoor` worlds are portable external canon.

## Binary-first native notes (committed `NATIVE_SYSTEMS.md`)

State formats are byte-order-neutral (SQLite + JSON); ABI uses only
i32/u32/u64 + char pointers, no structs (no alignment issues); symbols
are never removed, `harness_abi_version` is the contract; lowlevel
default policy = safe probes only, audit log is JSON-exportable.

## Files touched (commits)

- `bbe3e2d` law: ARCHITECTURE_LAW.md
- `ab0e719` docs: M3_PLAN.md
- `a508ed5` knowledge.rs
- `dfc21da` pipeline.rs
- `1cae3f0` store.rs + runtime.rs
- `55df34a` export.rs + replay.rs + lib.rs
- `d4bcde4` station-echo.txt + m3_proof.rs + compiler lexicons
- `79a0fb7` CLI world export
- `f7ec1b4` harness-ffi + device/ffi_proof/main.c
- `fafde12` harness-lowlevel
- `839232f` NATIVE_SYSTEMS.md + workspace wiring

## Remaining (next milestone candidates)

- CLI: dialogue/story/branch subcommands beyond export; replay command.
- Device test on a second Android machine (true multi-device generality).
- Desktop consumer using the C ABI (first polyglot binding).
