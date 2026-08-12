# Portability contract

The deterministic core, SQLite store, policies, adapters, circuit breakers,
JSONL parsers, encrypted jobs and audit ledger are shared across all platforms.

## Platform backends

| Platform | Process launch | State root | Prime lifecycle |
|---|---|---|---|
| Windows | `CREATE_NEW_PROCESS_GROUP` / native console script | `%LOCALAPPDATA%\Harness2` | native Prime CLI; `supervise` via Task Scheduler/service manager |
| macOS | POSIX process sessions | `~/Library/Application Support/Harness2` | hardened wrapper when source bundle is installed |
| Linux | POSIX process sessions | `$XDG_STATE_HOME/harness2` | hardened wrapper when source bundle is installed |
| Termux | POSIX process sessions | `~/.harness2` | native/source Prime as available |
| PRoot | PRoot-safe exact parent identity + AF_UNIX | `~/.harness2` | hardened wrapper, locks, pidfiles and socket probe |

Executable and path discovery are centralized in `harness2.platforms`; engine
adapters do not hardcode `/root`, `/usr/bin`, PowerShell, bash or CMD.

Deployment templates are in `deploy/`: systemd user service (Linux), launchd
plist (macOS), and PowerShell Task Scheduler installer/uninstaller (Windows).
