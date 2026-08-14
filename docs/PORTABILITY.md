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

`harness platform --json` exposes the normalized platform identity and primitive
capability states. Android/Termux and Linux are supported runtime targets.
macOS and Windows currently have import/CLI smoke evidence only; `/proc`-backed
always-active process identity is not claimed portable to those hosts.

Deployment templates are in `deploy/`: systemd user service (Linux), launchd
plist (macOS), and PowerShell Task Scheduler installer/uninstaller (Windows).
