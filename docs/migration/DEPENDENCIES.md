# Migration dependency report

## Required core runtime

| Dependency | Requirement | Purpose |
|---|---|---|
| Python | 3.11 or newer | Harness runtime, tests, migration and restore tools |
| SQLite | Python stdlib build with backup API | Durable state and consistent snapshots |
| OpenSSL | `enc` with PBKDF2 and AES-256-CTR | Object/job encryption and migration envelopes |
| Git | Current supported Git | Canonical history, bundle creation and restore |
| setuptools/pip | Build environment | Wheel construction and clean installation |

The Python project declares no third-party runtime package dependencies.

## Linux PC

Recommended Debian/Ubuntu baseline:

```sh
sudo apt install git python3 python3-venv python3-pip openssl curl coreutils
```

Provider CLIs are optional. A clean PC without OpenCode, Prime Agent, Hermes, or
Node still runs the core, tests, context runtime, state verification, and package
restore path. Missing optional providers must remain unavailable rather than
being simulated.

## Android / Termux

Native Termux baseline:

```sh
pkg install git python openssl curl coreutils
```

PRoot additionally needs its distribution's Python/pip tooling. The Harness
preserves `PREFIX` and explicit PRoot markers in provider environments and uses a
private state-root temporary directory rather than assuming `/tmp` exists.

## Optional capability providers

| Provider/tool | Required? | Notes |
|---|---|---|
| OpenCode | No | Private-file prompt transport; bounded JSONL output |
| Prime Agent | No | Native CLI everywhere; hardened source wrapper on Linux/Termux/PRoot when available |
| Node | No for core | Needed only for the hardened Prime source bundle |
| Hermes | No | Disabled by default; task text is argv-visible |
| Local model server | No | Must be explicitly enabled and loopback-only |
| Termux:API | No | Android bridge/notifications are not implemented in 3C |

## Architecture and shell assumptions

- Core Python and wheel are architecture-independent.
- The current phone is Android/PRoot on `aarch64`; no portable-core code pins
  that architecture.
- `install.sh` is the Linux/Termux POSIX installer and deliberately remains
  pinned to stable v2.1.1.
- Windows uses the Python console script/PowerShell launchers rather than
  `install.sh`.
- macOS and Windows are smoke-tested, not claimed behaviorally equivalent to the
  Linux/Android always-active service.
