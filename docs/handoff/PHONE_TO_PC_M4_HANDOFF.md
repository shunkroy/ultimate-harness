# PHONE → PC M4 HANDOFF

STATUS: READY — sealed by phone-side Kiteretsu (handoff/freeze mode)
Purpose: transfer Milestone 4 primary implementation authority to the PC.
Phone retains one role: **Android/Termux Harness Field Node**.

## A. Phone repository

- Absolute repo path: `/opt/harness2`
- Branch: `milestone3-conversation-native-foundation`
- HEAD: `ab84ff617b6dcb82233cd80e953b29d053228f6e`
- Upstream: `origin/milestone3-conversation-native-foundation` (configured)
- Remote: `origin` → `https://github.com/shunkroy/ultimate-harness.git`
- Ahead/behind: `0 0`
- Working tree: CLEAN (no modifications, no untracked files)

## B. M3 verification (actual run, 2026-08-16, on-device)

| Gate | Command | Result |
|------|---------|--------|
| Rust workspace | `cargo test --workspace` | **106/106 pass** (15 core + 5 contracts + 3 ffi + 7 lowlevel + 45 world-lib + 15 m3_proof + 16 m2 proof) |
| Python compatibility | `python3 -m pytest -q` | **393 pass, 1 skip, 3 fail** — the 3 failures (`test_adapters`, `test_direct`, `test_platforms`) reproduce identically on sealed base `d2a2159`; pre-existing environment drift (opencode config/HOME), not M3 regressions |
| C ABI proof | gcc on-device + `device/ffi_proof/main.c` | **10/10 CHECK PASS, FFI PROOF: PASS** |
| CLI export chain | `world compile station-echo → open --act go/take/talk/status → world export` | `hdoor_export_v1`, 4 events (move, take, talk, status), verified |
| Integrity | `harness-native status` | schema 5, migrations contiguous, **68 pins**, 2 sessions |

## C. Milestone 3 implementation (genuinely implemented)

- Knowledge boundaries: `knowledge.rs` — 6 provenance sources (CanonFact/Witnessed/Learned/Rumored/PlayerSupplied/Inferred), participant-limited canon seeding, canonical-key matching, no omniscience.
- Conversational dialogue: multi-turn Talk, topic extraction, ambiguity fail-closed (`AmbiguousTopic`), honest "I don't know" answers, teaching ("tell X about Y" → PlayerSupplied + learn event), witnessing.
- Story mode: chapter advancement as recorded state transitions, story gate, cap at timeline; mode switching (story/traveller/chat/watcher/replay) recorded as events; watcher read-only.
- Runtime branching: `fork_branch` with branch_fork genesis event (prev = parent tip), `genesis_prev` anchor, kv copy, branch-diverged provenance.
- Signed export: `hdoor_export_v1` + pure `verify_export` (chain recompute, tamper rejection proven).
- Deterministic replay: injected clock, one tick per act (leading dummy tick), identical hashes + snapshots.
- Second corpus: `station-echo.txt` (original sci-fi) + 15-proof `m3_proof.rs`.

## D. Native/polyglot foundation (exists now)

- `harness-ffi` crate: stable C ABI — `harness_abi_version`, `harness_version`, `harness_last_error`, `harness_string_free`, `harness_world_compile/open/act/export_json/close`. i32 codes, opaque u64 handles behind live-handle registry (stale-handle rejection, double-close containment, no unwind across ABI). Proof: Rust ABI tests + on-device gcc C caller.
- `harness-lowlevel` crate: request → capability check → policy → governor → adapter → op → audit. Default policy grants only safe probes (CPU features, endianness, page size); `RawCpuinfoMmap` DESIGNED-ONLY, denied at runtime, audit JSON-exportable.
- `docs/architecture/NATIVE_SYSTEMS.md`: polyglot stack chart, ABI contract, binary-first notes (endianness-neutral state, no structs across ABI, versioned symbols).
- CLI: `world compile / open / list / export`.

## E. Conversation/world foundation (exists now)

As in C — plus M2 foundation intact: compiler, manifest/package, index, store (SQLite event log, hash chain), pipeline, runtime, 45 lib tests + 16 integration proofs.

## F. Known defects

1. **Cosmetic double-article in entity names**: canonical keys/ids are correct but display names may retain a leading "the" (e.g. `"You take the The Docking Key."`, `"the the Greenhouse"`). Display-only; hashes/state unaffected. Fix deferred (would touch name normalization in compiler).
2. **Python baseline drift (phone env)**: 3 pre-existing failures reproduce on sealed M2 base `d2a2159` — environment-related (opencode config/HOME/CLI exit-code expectations), not code regressions. PC environment may pass them; do not treat as M3 blocker.
3. **`m3_11` leftover dead code**: harmless (live-run hash push overwritten by export-based hashes; `let _ = created_at`). Optional cleanup.
4. **Storage pressure**: `/` at 98% used (5.8G free of 227G); `target/` is the bulk of `/opt/harness2` (1.4G). Do not run additional full rebuilds on phone without freeing space; do not delete caches without explicit order.
5. **sqlite3 CLI absent** on phone (library `libsqlite3.so.0` present; Rust uses rusqlite — fine).

## G. Deferred M3 items (genuinely remaining)

- Richer dialogue/story/branch CLI commands (only `world export` added in M3).
- Replay CLI command (replay exists as library API only).
- First non-Rust/C polyglot ABI consumer (Kotlin/Java/other) — M4 work.
- Second Android-device verification (single-device proof only so far).

## H. Phone environment

- Android 15 (ro.build.version.release), ABI arm64-v8a
- Kernel: `Linux localhost 6.17.0-PRoot-Distro #1 SMP PREEMPT_DYNAMIC aarch64`
- Termux/PRoot-Ubuntu 26.04 LTS; rustc/cargo **1.93.1** (PRoot toolchain only — never Termux bionic toolchain)
- gcc/g++ 15.2.0 (Ubuntu 15.2.0-16ubuntu1)
- OpenJDK 17.0.19; **Kotlin 2.4.0 available**
- **zig: NOT installed**
- Python 3.14.4; git 2.53.0
- libssl.so.3, libsqlite3.so.0 (AArch64)
- `harness-native` installed at `/usr/local/bin/harness-native` (v0.1.0)
- State root `/root/.harness2` (128M; 2 sessions, 68 pins); backup `/root/.harness2-backup-20260816-062003`
- Repo `/opt/harness2` 1.4G (mostly `native/target/`)
- Storage: `/` 98% used, ~5.8G free

Runs successfully on phone: Rust workspace suite (106), Python suite (393+1 with 3 env failures), FFI C caller proof, CLI compile/open/export chain, integrity verify, M2 Overlord session resume.

## I. PC starting instructions (exact, safe)

```sh
git fetch --all --prune
git switch milestone3-conversation-native-foundation
git pull --ff-only
git rev-parse HEAD
git status
```

Do NOT reset destructively. Do NOT create a separate phone fork.

## J. Expected starting commit

`ab84ff617b6dcb82233cd80e953b29d053228f6e` (branch tip on `origin/milestone3-conversation-native-foundation`; may advance by the handoff commit below).

## K. M4 authority transfer

«Milestone 4 primary implementation authority transfers to the PC after this handoff. The phone remains an Android/Termux field and verification node unless specifically assigned an isolated development task.»

## Continuity model

```
                 Git remote (authoritative)
                /                        \
   PC DEVELOPMENT (M4 forge)     PHONE NODE (Android/Termux)
   implementation/integration    verification/portability/ARM tests
                \                        /
                 \                      /
              ONE architecture, ONE repository lineage.
              Platform-specific adapters only where necessary.
```

## Architectural context preserved for PC

- Rust remains the preferred safe systems core.
- The Inner World Heart is deliberately C.
- C/C++, Zig and other native languages available where suitable.
- Assembly intentionally supported for x86-64, AArch64, RISC-V behind safe boundaries.
- Kotlin/JVM/Java and C# are first-class ecosystem targets.
- Python optional, not the default architectural foundation.
- Harness is binary-first/native-first where appropriate; offline operation fundamental; AI providers optional enhancements.
- Knowledge boundaries and provenance remain mandatory.
- `.hdoor`/world/story is one subsystem of Harness, not the whole.
- Inner World and Dream Engine remain distinct architectural concepts.
- Future graphic novel, visual novel, watcher, traveller and live-inside modes must remain possible.
- Direct binary/memory/syscall/ABI/FFI/device access belongs behind isolated low-level capability boundaries.
- World/document transformations require explicit provenance/source maps.
- Prior research (offline engines, parser adventures, IF, visual novels, game engines, state machines, event sourcing, replay, deterministic simulation) remains applicable.
