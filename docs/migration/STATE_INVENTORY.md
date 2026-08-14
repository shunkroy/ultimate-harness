# Phone-side state inventory

Observed source platform: Android/Termux Ubuntu PRoot, `aarch64`.

The exact final file list, modes, sizes and SHA-256 values are generated into the
external migration package. This tracked document defines ownership and backup
policy without embedding secret values.

## A. Git-tracked source

- `harness2/`: core, adapters, kernel, storage, context, sandbox and skill code.
- `tests/`: deterministic unit/integration and migration-package tests.
- `docs/`, `deploy/`, `bin/`, `.github/`, `scripts/migration/`.
- `pyproject.toml`, stable `install.sh`, README and license.

GitHub is canonical. The normal package contains only the sealed `HEAD` and its
required ancestry. A separate full-ref `git bundle --all` exists only inside the
authenticated encrypted emergency artifact.

## B. Persistent runtime state

Current state root: `/root/.harness2` (source-host observation only).

| Entry | Owner | Backup treatment |
|---|---|---|
| `harness.db` | Store/kernel/jobs/audit | SQLite online backup plus full `PRAGMA integrity_check` |
| `integrity.json` | Installed-source integrity | Encrypted persistent-state package |
| `jobs/*.bin` | Legacy durable queue | Encrypted persistent-state package |
| `contexts/` | Context compiler/runtime | Encrypted persistent-state package; may contain private plaintext |
| `context-jobs/` | Context queue | Encrypted persistent-state package |
| `objects/` | Authenticated object storage | Encrypted persistent-state package |

Observed database state before sealing:

- Schema version: 4.
- Full integrity check: `ok`.
- Legacy jobs: one terminal `succeeded` record; no queued/running/retry job.
- Typed tasks: no active records.
- One encrypted `jobs/*.bin` file has no matching active job row and is preserved
  as an orphan rather than deleted.

## C. Secrets

Secret files are never added to Git or normal package plaintext:

- `secrets.json` or Windows `secrets.dpapi`;
- `job.key`;
- `object-store.key`;
- any future private key explicitly classified by the package policy.

Observed secret variable **names only**: `OPENAI_API_KEY`, `GEMINI_API_KEY`.
Values are not logged or manifested. Secrets are transferred in a separate
authenticated encrypted artifact; its key is stored outside all migration
artifacts and must travel separately. Windows DPAPI material is machine/account
bound and normally requires credential re-entry rather than cross-host reuse.

## D. Rebuildable/disposable data

Excluded:

- `run/` PID files, sockets, locks and heartbeats;
- `tmp/` private temporary inputs;
- `logs/` unless a separate forensic decision is made;
- `__pycache__`, bytecode, pytest caches;
- SQLite `harness.db-wal`, `harness.db-shm`, and rollback journals after their
  committed contents are captured through the online backup API;
- `build/`, `dist/`, wheel caches and egg metadata;
- virtual environments and downloaded packages.

## E. Platform-specific assets

- POSIX checkout launchers and Linux systemd template.
- Windows CMD/PowerShell launchers and Task Scheduler templates.
- macOS launchd template.
- Current external provider installations, Termux paths and Prime source checkout
  are dependencies/observations, not copied into the canonical source package.

## Consistency rule

The final snapshot is taken with the Harness maintenance service paused. The
builder refuses nonterminal work, checks recorded and `/proc`-discoverable
supervisors, holds an SQLite write reservation, and compares pre/post non-database
state fingerprints plus two independent SQLite online backups. Prime may remain
independently healthy because its runtime locks/logs/sockets are excluded. The
Harness service is restored and verified immediately after packaging. No
original source, database, context, key or user state is deleted.
