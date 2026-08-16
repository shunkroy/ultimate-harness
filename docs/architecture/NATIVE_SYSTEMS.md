# Native Systems — Harness Polyglot Architecture

STATUS: IMPLEMENTED (ffi, lowlevel) / DOCUMENTED (unsafe paths)

## 1. One engine, many bindings

The world runtime (`harness-world`) is the single engine. Language
consumers never link it directly; they link the stable C ABI
(`harness-ffi`), and anything touching platform resources goes through
the capability-governed layer (`harness-lowlevel`).

```
┌────────────────────────────────────────────────────────────┐
│ Consumers: C/C++ · Kotlin (Android) · Swift (iOS) · Zig ·  │
│            WASM · desktop GUI/CLI/TUI · game engines       │
└──────────────────────────┬─────────────────────────────────┘
                           │ stable C ABI (harness-ffi)
┌──────────────────────────▼─────────────────────────────────┐
│ harness-world  (compiler, store, pipeline, runtime,        │
│                 knowledge, export, replay)                 │
└──────────────────────────┬─────────────────────────────────┘
                           │ capability requests
┌──────────────────────────▼─────────────────────────────────┐
│ harness-lowlevel  (policy → governor → adapter → audit)    │
│                 platform: CPU features, endianness, pages  │
└────────────────────────────────────────────────────────────┘
```

## 2. The C ABI (harness-ffi, ABI version 1)

| Symbol | Purpose |
|--------|---------|
| `harness_abi_version` | ABI version (u32). Bump on breaking change. |
| `harness_abi_is_compatible` | Negotiation: 1 iff requested version == current, else 0. |
| `harness_version` | Human-readable version (malloc'd, free via `harness_string_free`). |
| `harness_last_error` | Per-thread last error (malloc'd). |
| `harness_string_free` | Free any returned string (NULL-safe). |
| `harness_world_compile` | source.txt + id + title → `.hdoor` package dir. |
| `harness_world_open` | Open/resume session; returns opaque u64 handle. |
| `harness_world_act` | One utterance → JSON `{ok,text,event}`. |
| `harness_world_export_json` | Signed branch history (schema `hdoor_export_v1`). |
| `harness_world_close` | Persist + chain verify; consumes handle. |

ABI rules:
- `i32` return codes: `0` ok, `-1` error (details via `harness_last_error`).
- Opaque u64 handles; a live-handle registry makes stale/forged
  handles fail cleanly (no use-after-free, no double-free).
- Every entry point catches panics — nothing unwinds across the ABI.
- All strings are heap-allocated; ownership transfers to the caller.
- Handle lifetime: open → (act|export)* → close; never reuse.
- Output pointers (`handle_out`, `result_out`) must be non-null or the
  call fails cleanly (`-1` + last-error) — null outputs are never
  dereferenced.
- Input C strings may be null or contain invalid UTF-8: both fail
  cleanly with an error, never a crash; output result pointers are
  left untouched on failure.
- Consumers MUST call `harness_abi_is_compatible()` before any other
  symbol and refuse to continue on 0; unknown/future versions are
  rejected so a newer consumer cannot misread this library.
- Handles are bound to the thread that opened them (per-thread
  last-error); distinct handles may be used from distinct threads
  concurrently.

## 3. Binary-first notes (endianness / alignment / versioning)

- **Endianness**: aarch64 targets are little-endian. The engine never
  assumes byte order in persisted state: the SQLite event log and
  JSON export are byte-order-neutral formats. `harness-lowlevel`
  exposes the detected order for consumers that must know.
- **Alignment**: the ABI surface uses only `i32`, `u32`, `u64`,
  `*const c_char`, `*mut c_char` — no structs cross the boundary, so
  padding/alignment differences between compilers are a non-issue.
- **Versioning**: `harness_abi_version()` is the contract version;
  `harness_abi_is_compatible()` is the single negotiation gate every
  consumer must pass before use. Symbols are never removed; new
  functionality adds new symbols and bumps the version.
  `harness_version()` reports the crate version.
- **Integer width**: handles are `u64` on all platforms; `size_t` is
  never part of the ABI.

## 4. Capability governance (harness-lowlevel)

Flow: `request → capability check → policy → governor → adapter → op → audit`.

- Every platform operation requires an authorized `Capability`.
- The governor records **every** request (grant or denial) in an
  append-only audit log, exportable as JSON.
- Default policy grants only the safe adapter surface
  (`CpuFeatureProbe`, `EndiannessProbe`, `PageSizeProbe`).
- `RawCpuinfoMmap` is DESIGNED-ONLY: an unsafe path exists in
  `unsafe_adapter` as a specification artifact, is denied by the
  default policy, and is never called. Safe code does the same job.
- There is deliberately **no unrestricted interface**.

## 5. Status vocabulary

- `harness-ffi`: IMPLEMENTED / TESTED (Rust ABI tests: negotiation,
  null/malformed inputs, use-after-close, thread concurrency; device C
  caller proof at `device/ffi_proof/main.c` incl. negative checks).
- `harness-lowlevel`: IMPLEMENTED / TESTED (governor + adapter tests).
- `unsafe_adapter::raw_cpuinfo_mmap`: DOCUMENTED / DESIGNED only.

## 6. Platform targets

- Primary: Termux / PRoot-Ubuntu aarch64 (rustc 1.93.1).
- Portable by construction: the ABI is C, state is SQLite + JSON,
  and the only platform calls are read-only probes.