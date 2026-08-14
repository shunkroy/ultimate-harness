# Phone Readiness Checkpoint — Android/Termux + Ubuntu PRoot

Sealed: 2026-08-14. Continues from sealed Checkpoint 3C commit `3c7b47c`
(origin/main, CI run 31796649331 green). No migration or integrity work was
redone; this document records the runtime-readiness stage only.

## Integrity state (confirmed, not redone)

`harness integrity verify` → `{'ok': True, 'manifest': '/root/.harness2/integrity.json'}`

| Check | Result |
|---|---|
| harness.launcher | PASS (sha256:698a4ab5…) |
| harness.module | PASS (sha256:9c35c829…) |
| harness.prime_wrapper | PASS (sha256:e761f2e0…) |
| prime.bundle | PASS (sha256:2262471e…) |
| integrity.manifest | PASS verified pins=64 |

`git status --short` in /opt/harness2: empty (0 changes), HEAD == `3c7b47c0dbf6f6caf7478b36c463558e8c85320f`.

## Runtime evidence (this session)

| Test | Result | Evidence |
|---|---|---|
| 1. Launch/runtime smoke | PASS | `harness version` 3.0.0.dev1; `status --json`: kernel healthy schema=4; always_active active, pid verified, heartbeat fresh (13 s), 70+ cycles; `platform`: kind=proot, android-proot, all core capabilities supported; `doctor`: 64 pins OK, DB modes 0700/0600, audit chain OK, engines healthy |
| 2. CLI/chat entry | PASS | `harness` command set complete; `harness run` routes (auto→opencode dry-run); `harness task {list,submit,inspect,cancel,retry}`; audit verify `ok: True, bad_seq: None`; opencode 1.18.18 present |
| 3. Provider discovery/routing | PASS | `harness providers`: harness kernel + opencode + prime healthy/enabled; zen, hermes, local disabled by config as designed; `validation_errors: []`; no generated catalogs modified (repo clean) |
| 4. Persistence/restart | PASS | audit chain survived restart (51 entries, verified); encrypted job store intact; `svc down` → `svc up` → new pid 16481 process_verified, heartbeat 3 s, kernel healthy |
| 5. Termux launcher | PASS | `~/bin/harness-phone` (0700, bash -n clean); standalone run reaches proot-distro login boundary; post-login logic verified (idempotent `svc up`, status healthy) |
| 6. Fresh-session launch | **PASS — REAL NATIVE TERMUX** | executed from native Android Termux (u0_a556) without manual PRoot entry: status/version/doctor/platform --json all PASS through the launcher |
| 7. Readiness sealed | PASS | this document + commit |

## Known state (non-blocking)

- Provider inference is quota-blocked today (OpenAI subscription exhausted,
  DeepSeek free tier usage limit). Routing, spawning, capture, and typed
  failure isolation all work; real inference resumes when quota resets.
- `doctor` reports `prime.source dirty files=1` — the pre-existing
  `/root/prime-agent/packages/ai/src/models.generated.ts` diff, tracked
  separately; not touched.
- 2 failed tasks in store = the two provider-quota runs (expected, isolated).
- `verify-after-clone.sh` venv smoke needs `python3-venv` (absent in PRoot);
  wheel install/import/CLI verified equivalently.

## Launcher fix (2026-08-14, found via real native Termux bash -x trace)

Root cause: the launcher received `CMD=status`, invoked
`proot-distro login ubuntu -- bash -c '...' _ status`, so inside the PRoot
script `$0=_`, `$1=status` — but a stray PRoot-side `shift` deleted `status`
before `exec harness "$@"`, leaving Harness with no command.

Fix (only this): remove the PRoot-side shift. The native-Termux shift after
`CMD="${1:-shell}"` is correct and kept.

Correct block:

```sh
if [ "$1" = "shell" ]; then
    exec bash -l
fi
exec harness "$@"
```

Verified from inside PRoot with the argv-exact reproduction
(`bash -c '<inner>' _ <args>` — the proot layer passes argv through verbatim):
`status`, `version`, `doctor` PASS; argument-bearing `platform --json`
(1 arg) and `audit --tail 2 tail` (3 args) PASS — all arguments survive.

## REAL NATIVE TERMUX PASS (2026-08-14)

Executed directly from native Android Termux (uid `u0_a556`), without
manually entering Ubuntu PRoot first.

| Command | Result |
|---|---|
| `whoami` | `u0_a556` — native Termux |
| `~/bin/harness-phone status` | Harness v3.0.0.dev1, KERNEL healthy schema=4, ALWAYS_ACTIVE desired=True observed=active |
| `~/bin/harness-phone version` | 3.0.0.dev1 |
| `~/bin/harness-phone doctor` | full doctor executed through the native launcher; integrity manifest verified pins=64 |
| `~/bin/harness-phone platform --json` | valid android-proot platform/execution profile returned |

The Termux → Ubuntu PRoot → Harness argument-forwarding boundary is now
verified on the actual phone. Commands and extra arguments no longer
disappear across the boundary.

## Separate existing/non-blocking findings (not fixed here)

- `prime.source dirty files=1` — pre-existing modified generated file in
  `/root/prime-agent` (models.generated.ts), tracked separately.
- `engine:hermes binary missing` — doctor label for the hermes adapter;
  hermes is disabled by policy; the wrapper script exists on the Termux
  side. Labeling nuance only.

Neither was modified as part of this seal; both require a separate explicit
task if they are ever to be addressed.

## From a real Termux session

```sh
~/bin/harness-phone            # Ubuntu PRoot shell, Harness supervisor up
~/bin/harness-phone status     # one-shot status
~/bin/harness-phone run --engine opencode "prompt"
```

Launcher: `proot-distro login ubuntu -- bash -c '… svc up … harness "$@"'`
