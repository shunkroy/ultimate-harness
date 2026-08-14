# Phone Harness — Recovery Manifest

Goal: restore this exact phone installation on a fresh compatible
Termux/Ubuntu environment. Prefer Git + hashes over archives. No secrets are
reproduced here (referenced by path only).

## Source of truth

- **Canonical repository:** `https://github.com/shunkroy/ultimate-harness.git`
- **PHONE_BASELINE_SHA:** `5a299b56ee66a7098c4e61ad78f6e7e7ee7d4023` (branch `main`)
- **Sealed checkpoints (history):** `3c7b47c` migration tooling · `8d285d5` +
  `4c075ae` + `9077cb8` phone readiness + REAL NATIVE TERMUX PASS ·
  `5a299b5` provider-fluid routing foundation.
- **Integrity manifest (self-describing):** `/root/.harness2/integrity.json`
  — paths + sha256 for launcher, module, prime wrapper, prime bundle, and all
  source files (pins=66). After restore, `harness integrity verify` validates
  the entire installation.

## What to restore (in order)

1. **Termux packages:** `pkg install proot-distro openssh` (sshd optional,
   dev bridge only), then `proot-distro install ubuntu`.
2. **Canonical repo:** inside the PRoot,
   `git clone https://github.com/shunkroy/ultimate-harness.git /opt/harness2`
   and `git checkout 5a299b56ee66a7098c4e61ad78f6e7e7ee7d4023`.
3. **Native launcher:** recreate `/data/data/com.termux/files/home/bin/harness-phone`
   (chmod 0700) — full content in `docs/handoff/PHONE_DEPLOYMENT_BASELINE.md`
   §10 (sha256 `698a4ab5…`). Required native-side: `proot-distro` on PATH.
4. **Runtime state** (from backup of the old installation, or fresh):
   - `/root/.harness2/` — `harness.db`, `integrity.json`, `run/`, `jobs/`
     (encrypted payloads), `contexts/`, `logs/`, `tmp/`. Modes: dir 0700,
     db 0600. If restoring from backup, preserve modes and hash-chain
     (audit `verify` self-checks).
   - Encrypted job payloads migrate via the sealed migration package
     (`/opt/harness-migration-3c.tar.gz` + `/opt/harness-key/migration.key`,
     0600/0700) — see `scripts/migration/restore_state.py` and
     `docs/migration/` (sealed at `3c7b47c`).
5. **Prime agent:** `/root/prime-agent` (separate repo; do not re-clone over
   it — keep `packages/ai/src/models.generated.ts` local diff as-is).
6. **Provider configuration:** env vars by name only — `GEMINI_API_KEY`,
   `GROQ_API_KEY`, `OPENAI_API_KEY`, `YOUTUBE_API_KEY`; opencode auth at
   `~/.local/share/opencode/auth.json` (OAuth/API entries). These are the
   only credential-adjacent items; they live outside the repo and are never
   committed.

## Verify after restore

```sh
harness integrity verify      # ok=True, pins=66 (re-pin only if files differ)
harness doctor                # integrity + runtime.always_active OK
harness status                # engines: opencode active, prime active
harness svc status            # observed_state=active, heartbeat fresh
cd /opt/harness2 && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests   # 306 OK
```

Then run the USER VERIFIED native smoke (§11 of the handoff doc) from the
native Termux prompt.

## Deliberately NOT archived

Generated caches, `__pycache__`, venvs, `*.whl`, logs, the runtime DB (it is
recreatable; only `jobs/` encrypted payloads + key need backup), and any
credential material. Normal Git + the documented runtime backup above are
sufficient — no giant opaque archives.
