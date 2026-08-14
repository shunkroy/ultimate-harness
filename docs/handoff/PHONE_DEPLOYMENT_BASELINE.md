# Phone Deployment Baseline — PC Handoff

**Status: FROZEN / READY** — sealed 2026-08-14.
This document is the canonical reference for continuing development on the PC.
The phone installation described here is a **deployment/reference target**: do
not fork it, do not develop major features in Termux. All future work happens
on the PC and reaches the phone through normal Git + controlled deployment.

---

## 1. Baseline identity (verified live, 2026-08-14)

| Item | Value |
|---|---|
| **PHONE_BASELINE_SHA** | `5a299b56ee66a7098c4e61ad78f6e7e7ee7d4023` |
| Branch | `main` (local == `origin/main`, verified via `git ls-remote`) |
| Working tree | clean |
| Harness version | `3.0.0.dev1` |
| Integrity | **OK — pins=66** (critical: harness.launcher, harness.module, harness.prime_wrapper, prime.bundle, integrity.manifest) |
| Tests | **306 OK** (1 pre-existing skip), `python3 -m unittest discover -s tests` |
| Routing checkpoint | sealed in `5a299b5` (provider-fluid fallback routing foundation) |
| Phone readiness checkpoint | sealed in `9077cb8` (REAL NATIVE TERMUX PASS) |
| Migration checkpoint | sealed in `3c7b47c` (+ CI run `31796649331` green) |
| Supervisor | alive, `observed=active`, cycles 315, heartbeat fresh, pidfile self-healed |
| Prime daemon | alive, pid 31199, `daemon.sock` healthy |

## 2. Architecture overview

```
Android Termux (native, uid u0_a556)
  └─ ~/bin/harness-phone          # sealed launcher (0700) — DO NOT MODIFY
       └─ proot-distro login ubuntu -- bash -c 'svc up; harness "$@"'
            └─ PRoot Ubuntu 26.04 aarch64 (rootfs: Termux proot-distro)
                 ├─ /opt/harness2            # canonical repo (git remote = GitHub)
                 ├─ supervisor               # python3 -m harness2 supervise --interval 30
                 ├─ prime daemon             # prime_wrapper.py → /root/.harness2/run/prime/daemon.sock
                 └─ opencode engine          # /root/.opencode/bin/opencode (headless JSON runner)
```

- **One canonical codebase.** The only code checkout is `/opt/harness2` (git,
  remote `https://github.com/shunkroy/ultimate-harness.git`, branch `main`).
  No phone fork exists or will exist.
- Platform differences belong behind `harness2/platforms.py` / adapter
  boundaries. Do not fork architecture for the PC.

## 3. Paths that matter

| Path | Purpose | Rules |
|---|---|---|
| `/opt/harness2` | canonical repo (inside PRoot) | git-managed; never delete |
| `/root/.harness2` (0700) | runtime state root: `harness.db` (0600), `integrity.json`, `run/`, `jobs/` (encrypted payloads), `logs/`, `tmp/`, `contexts/` | **never commit**; back up for recovery |
| `/data/data/com.termux/files/home/bin/harness-phone` | native launcher (only native-side file; sha256 pinned) | sealed; recreate from §10 if lost |
| `/data/data/com.termux/files/usr/var/lib/proot-distro/installed-rootfs/ubuntu` | PRoot rootfs | reinstall via `proot-distro install ubuntu` |
| `/root/prime-agent` | prime agent bundle (separate repo) | do not touch; `models.generated.ts` has a pre-existing local diff (separate task) |
| `/opt/harness-migration-3c*` , `/opt/harness-key/migration.key` | sealed migration artifacts | outside repo; key 0600; see RECOVERY_MANIFEST.md |
| `/root/.ssh`, `/data/data/com.termux/files/home/.ssh` | ssh keys (incl. optional native bridge key) | never commit |

## 4. Supervisor / runtime behavior

- Always-active supervisor: `harness svc up` → `python3 -m harness2 supervise
  --interval 30`; heartbeats to `/root/.harness2/run/service-heartbeat.json`;
  writes `run/service.pid`.
- `harness svc status` self-heals a stale pidfile (matches live service
  processes and rewrites the pidfile). A stale pidfile surfaces as
  `runtime.always_active FAIL` in `harness doctor` — run `harness svc status`
  to refresh; this is runtime state, not a code bug.
- Watchdog: `kirti_start.sh` (Kirti project infra) respawns loops; harness
  supervisor is independently `svc`-managed.
- Engines: `opencode` active · `prime` active (daemon 31199) · `zen` disabled
  (no `OPENCODE_API_KEY`) · `hermes` disabled (policy) · `local` disabled
  (policy).

## 5. Commands

```sh
# tests (repo root)
cd /opt/harness2 && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .   # 308 OK (use -t .; tests/ is a package)
# older form without -t . breaks on relative imports in test_direct.py — fixed by tests/__init__.py

# integrity
harness integrity verify          # expect ok=True, pins=67 on the phone
harness integrity pin             # ONLY after adding/removing source files; re-verify after

# health
harness status
harness doctor

# service
harness svc status | svc up | svc down

# QA (use the direct engine — sub-second, beats opencode-engine latency)
harness run --engine direct "question"                             # auto model (HARNESS_DEFAULT_MODEL)
HARNESS_DEFAULT_MODEL=groq/llama-3.3-70b-versatile harness run --engine direct "question"
HARNESS_DEFAULT_MODEL=google/gemini-3.5-flash-lite harness run --engine direct "question"
```

## 6. Provider-routing state (sealed in 5a299b5)

- `AUTO` → `opencode` primary, fallbacks `[prime]`; `zen`/`hermes`/`local`
  skipped with recorded reasons (disabled/unconfigured).
- Env controls: `HARNESS_DEFAULT_MODEL` (default model), `HARNESS_FALLBACK_ORDER`
  (engine preference override).
- Failure taxonomy (14 codes) + `normalize_failure()`; circuits keyed
  `engine:provider:model`; audit events `route.selected/failed/skipped/completed`.
- **Verified working routes (live, 2026-08-14):**
  - **direct engine — NEW, sub-second (latency fixed):** native REST calls
    bypass the opencode agent overhead entirely. Measured on this phone:
    - Groq `direct + groq/llama-3.3-70b-versatile`: **0.4 s** engine time
      (was 30–75 s via opencode agent; Cloudflare 1010 fixed by sending an
      explicit `User-Agent` header; endpoint is the full
      `https://api.groq.com/openai/v1/chat/completions`).
    - Google `direct + google/gemini-3.5-flash-lite`: **1.4 s** engine time.
    - Durations are now propagated into engine results (`[Xs run=…]` banner
      also prints on failures); circuit keys `direct:provider:model` protect
      every direct route (cooldown grows on repeated failures).
  - zen free via opencode engine: `opencode/deepseek-v4-flash-free` — works,
    but latency is unstable tonight (intermittent timeouts → fallback fires;
    observed 4 ok / 3 timeout). Use `--engine direct` for interactive QA.
  - direct Google API: `google/gemini-3.5-flash-lite` (env `GEMINI_API_KEY`) —
    reliable; delivered full capabilities answer.
- **Provider-limited (recorded, not harness faults):** OpenAI `gpt-5.6-sol`
  (quota — both OAuth and direct key), DeepSeek `deepseek-chat` (insufficient
  balance), Groq `llama-3.3-70b` (12k TPM → `model_unavailable`; small QA
  prompts e.g. ≤1k tokens fit fine — verified above).
- To enable the `zen` engine: set `OPENCODE_API_KEY` (user authorization) —
  free zen models already work through the opencode engine without it.

## 7. Known non-blocking issues (re-evaluated 2026-08-14)

0. **Latency “lightyear” — RESOLVED (this commit):** opencode-agent path
   (zen free) can still take 30–75 s+ intermittently (queue + agent boot +
   timeouts). The new **direct engine** (`harness run --engine direct`) does
   plain HTTPS provider calls → Groq 0.4 s / Google 1.4 s. If a prompt ever
   shows `[Xs run=…]` with X ≫ 10 on a direct route, check circuits +
   `/root/.harness2/harness.db` `runs` table (duration recorded).
1. `prime.source` dirty files=1 (`packages/ai/src/models.generated.ts`) —
   pre-existing, separate task; do not touch.
2. `zen` engine unconfigured (needs `OPENCODE_API_KEY`); free zen models
   usable via opencode engine today.
3. `hermes` engine disabled by policy — **binary now present** at
   `/data/data/com.termux/files/home/.hermes/hermes-agent/hermes` (earlier
   "binary missing" finding resolved).
4. `local` engine disabled by policy (loopback adapter present).
5. OpenAI / DeepSeek / Groq provider limits (see §6) — recorded per-run in DB.
6. Disk: **2.6 GiB free (99%)** — monitor; keep generated artifacts out of the
   repo.
7. `logs/service.log` contains one historical `Errno 38 … service.pid` line
   (2026-08-14 08:55) — never recurred; pidfile + heartbeat healthy.
8. Test suite spawns short-lived supervisors during runs; they can overwrite
   `run/service.pid` → see §4 self-heal.
9. `verify-after-clone.sh` venv smoke needs `python3-venv` (absent in PRoot);
   equivalent wheel install/import/CLI smoke passed (documented in readiness).

## 8. Runtime state — never commit

State lives **outside** the repo (`/root/.harness2`), and the repo `.gitignore`
already covers `*.db*`, `*.log`, `.env`, `tmp/`, `build/`, `dist/`,
`__pycache__/`. Verified: no state files tracked, no untracked files, no secret
patterns in tracked files. Never commit: `/root/.harness2`, `/opt/harness-key`,
`/root/.ssh`, native `.ssh`, `.env`, `~/.local/share/opencode/auth.json`.

## 9. PC bootstrap (Phase 10 starts here)

```sh
git clone https://github.com/shunkroy/ultimate-harness.git
cd ultimate-harness
git checkout main
git rev-parse HEAD          # expect 5a299b56ee66a7098c4e61ad78f6e7e7ee7d4023
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # or build the wheel per docs/migration/README.md
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests   # 306 OK
harness integrity pin       # establish PC-local pins (manifest is runtime state, never committed)
git checkout -b phase10-persistent-context-sessions
# ... develop, test, commit, push. DO NOT start Phase 10 on the phone.
```

Deploy a later PC commit to the phone (controlled):

```sh
# on the phone (inside PRoot)
git -C /opt/harness2 fetch origin
git -C /opt/harness2 checkout <target-sha>        # or: git pull --ff-only
cd /opt/harness2 && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
harness integrity pin && harness integrity verify # only pin if files added/removed
harness svc down && harness svc up                # controlled restart
# native smoke: see §11 (USER VERIFIED commands)
```

Rollback to baseline:

```sh
git -C /opt/harness2 checkout 5a299b56ee66a7098c4e61ad78f6e7e7ee7d4023
harness integrity pin && harness integrity verify
harness svc down && harness svc up
harness doctor   # expect integrity + always_active OK
```

## 10. Native launcher (recovery copy — content is not secret)

`/data/data/com.termux/files/home/bin/harness-phone`, chmod 0700
(sha256 `698a4ab56d878e44947444442f2ea2dec74326c6b7b3a304dc32ad7d0b7613b0`):

```sh
#!/data/data/com.termux/files/usr/bin/bash
# harness-phone — start Harness from a native Termux session.
set -u
CMD="${1:-shell}"
if [ "$#" -gt 0 ]; then
    shift
fi
proot-distro login ubuntu -- bash -c '
  export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  harness svc up >/dev/null 2>&1 || true
  if [ "$1" = "shell" ]; then
    exec bash -l
  fi
  exec harness "$@"
' _ "$CMD" "$@"
```

Do NOT change this file (integrity-pinned). History: a stray PRoot-side
`shift` deleted the command (fixed 2026-08-14, see PHONE_READINESS.md).

## 11. Native Termux smoke — USER VERIFIED

The opencode/Kiteretsu session runs **inside the PRoot**; every process it
spawns is ptrace-traced by the outer PRoot, so `proot-distro` refuses the
launcher (`TracerPid` guard — verified). Native execution capability was
verified via a loopback sshd bridge (real uid 10556 = u0_a556, bionic
binaries, Android kernel), but the launcher itself must be exercised from a
real native shell. Run these at the native Termux prompt:

```sh
whoami                                   # expect: u0_a556
~/bin/harness-phone status               # ALWAYS_ACTIVE active; engines table
~/bin/harness-phone version              # Harness v3.0.0.dev1
~/bin/harness-phone providers            # engines + manifests (zen free models listed)
~/bin/harness-phone run --engine auto "Reply with exactly: PHONE-HARNESS-OK"
```

The last command uses the free/authorized route (`HARNESS_DEFAULT_MODEL` is
persisted per-run; if the zen free model is quota-limited at that moment,
record it separately — do not call the launcher/runtime broken).

Optional dev bridge (not required for operation): start native sshd loopback
(`sshd -p 8022 -o ListenAddress=127.0.0.1`), keypair
`/root/.ssh/id_ed25519_native` (pubkey already in native `~/.ssh/authorized_keys`);
connect `ssh -p 8022 -i /root/.ssh/id_ed25519_native u0_a556@127.0.0.1`.
Note: sessions spawned from PRoot remain traced → launcher commands still
refuse there; use the bridge for native identity/env checks only.
