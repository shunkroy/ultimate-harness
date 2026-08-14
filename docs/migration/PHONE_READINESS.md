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
| 6. Fresh-session launch | PASS (simulated) | Clean-env (`env -i`) execution of launcher; physical proot entry requires a real Termux session — one command below |
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

## From a real Termux session

```sh
~/bin/harness-phone            # Ubuntu PRoot shell, Harness supervisor up
~/bin/harness-phone status     # one-shot status
~/bin/harness-phone run --engine opencode "prompt"
```

Launcher: `proot-distro login ubuntu -- bash -c '… svc up … harness "$@"'`
