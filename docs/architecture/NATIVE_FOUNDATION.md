# Native Foundation — implementation record

Status: first native milestone, committed on phone (aarch64 Linux under PRoot).

## What exists (real, built, tested)

| Component | Location | Status |
|---|---|---|
| Cargo workspace | `native/` | IMPLEMENTED + TESTED |
| Canonical v1 object model (versioned JSON envelopes, strict parsing, no pickle/java/.NET/rust-layout dependence) | `native/crates/harness-core/src/types.rs`, `schema.rs` | IMPLEMENTED + TESTED |
| Engine contract + deterministic arithmetic engine (no network, no model) | `native/crates/harness-core/src/engine.rs` | IMPLEMENTED + TESTED |
| Runtime discovery (truthful PATH probing) | `native/crates/harness-core/src/runtime.rs` | IMPLEMENTED + TESTED |
| Deterministic state-root resolution (HARNESS2_HOME override; split state fails closed) | `native/crates/harness-core/src/stateroot.rs` | IMPLEMENTED + TESTED |
| Read-only interop: schema/migration/session/integrity read from the canonical SQLite state | `native/crates/harness-core/src/status.rs` | IMPLEMENTED + TESTED + DEVICE-VERIFIED |
| Native binary `harness-native` (`status`, `session list`, `runtime list`, `engine demo`) | `native/crates/harness-native/` → `/usr/local/bin/harness-native` | IMPLEMENTED + TESTED + DEVICE-VERIFIED (PRoot aarch64) |

## Proven interop (same state, no duplication)

`harness-native status` vs `harness status` on the live phone state root `/root/.harness2`:

- schema 5, migration history `[1,2,3,4,5]`, integrity pins 68 — identical
- session list — byte-identical ids/titles/states/timestamps across Rust and Python

## Honest non-goals of this milestone

- No writes, no migration, no state creation from native code (read-only interop only)
- No Python removal; existing Harness suite re-run green (396/397 + the previously
  classified PRoot-semantics test — unchanged baseline)
- No phone fork: components are platform-neutral; committed on a local branch
  (`native-foundation`) so the verified `main` checkpoint stays intact
- World/.hdoor/Story/Dream integration: DESIGNED (types exist) — no runtime yet
- Cross-device sync: NOT AUTHORIZED, not started