# Portability contract

The deterministic core, SQLite store, policies, adapters, circuit breakers,
JSONL parsers, encrypted jobs and audit ledger are shared across all platforms.

## Platform backends

| Platform | Process launch | State root | Prime lifecycle |
|---|---|---|---|
| Windows | `CREATE_NEW_PROCESS_GROUP` / native console script | `%LOCALAPPDATA%\Harness2` | native Prime CLI; import/CLI smoke-tested, service lifecycle not equivalent to Linux |
| macOS | POSIX process sessions | `~/Library/Application Support/Harness2` | native Prime CLI; import/CLI smoke-tested, service lifecycle not equivalent to Linux |
| Linux | POSIX process sessions | `$XDG_STATE_HOME/harness2` | hardened wrapper when source bundle is installed |
| Termux | POSIX process sessions | `~/.harness2` | native/source Prime as available |
| PRoot | PRoot-safe exact parent identity + AF_UNIX | `~/.harness2` | hardened wrapper, locks, pidfiles and socket probe |

Executable and path discovery are centralized in `harness2.platforms`; engine
adapters and POSIX launchers do not assume a fixed checkout or installation
path. Platform candidate paths remain bounded and explicit.

## State-root resolution (deterministic, never existence-based)

A directory counts as Harness state only when it contains a readable Harness
kernel database: `harness2.platforms.is_harness_state_root` requires a SQLite
store (`harness.db`/`harness.sqlite`) carrying the `kernel_schema_migrations`
table at version >= 1. An empty or unrelated `~/.harness2` directory never
counts and cannot hijack the canonical root (the 2026 split-brain failure).

Resolution order (Linux/macOS/Windows; Termux/PRoot use `~/.harness2` as
their canonical root, so the two names coincide there):

1. `HARNESS2_HOME` override, unchecked.
2. Canonical root valid -> canonical root.
3. Only legacy `~/.harness2` valid -> legacy root (compat).
4. Both valid -> `HarnessSplitStateError` (exit code 2 with both paths and
   the override hint). Never a silent pick: the CLI fails closed with an
   actionable diagnostic.
5. Neither valid -> canonical root (created on first use).

`harness platform --json` exposes the normalized platform identity and
primitive capability states. Android/Termux and Linux are supported runtime
targets. macOS and Windows currently have import/CLI smoke evidence only;
`/proc`-backed always-active process identity is not claimed portable to
those hosts.

Deployment templates are in `deploy/`: systemd user service (Linux), launchd
plist (macOS), and PowerShell Task Scheduler installer/uninstaller (Windows).
