# Checkpoint 3C platform and path audit

Audit target: all tracked files in `ultimate-harness`.

## Classification

| Finding | Classification | Disposition |
|---|---|---|
| `/root` references in policy tests | Legitimate test fixture | Retained; no production dependency |
| Termux prefix strings in platform tests | Legitimate Android fixture | Retained and explicitly modeled |
| `${TMPDIR:-/tmp}` in stable installer | Legitimate POSIX fallback | Retained; README no longer hard-codes its download path |
| `/usr/bin/node` Prime wrapper default | Portable-core bug | Removed; `--node` is now required/discovered |
| `/usr/bin/node` and OpenSSL candidates | Bounded platform candidates | Retained after PATH/override discovery |
| `127.0.0.1:8080` local model default | Legitimate configurable loopback default | Retained; non-loopback endpoints fail closed |
| PRoot PATH replacement | Portability bug | Fixed; validated inherited entries and standard PRoot entries are combined |
| Inherited temporary directory | Execution-boundary bug | Fixed; providers receive private state-root temp paths |
| Hidden host environment during explicit tests | Reproducibility bug | Fixed; explicit empty PATH stays empty and config resolves from captured platform environment |
| `.ps1` accepted as directly executable | Windows discovery bug | Fixed by refusing it until an explicit PowerShell host exists |
| Provider CLI probes bypassing command prefixes | Windows discovery bug | Fixed with explicit platform command prefixes |
| Hermes status disabled but direct `run()` enabled | Policy bypass | Fixed in the adapter |
| Unbounded CLI/HTTP output | Resource/safety bug | Fixed by bounded execution/body readers |
| Malformed/trailing provider streams accepted in execution | Result-integrity bug | Fixed with strict adapter parsing |
| SQLite-controlled job payload deletion path | Filesystem-authority bug | Restricted to the current `jobs/` namespace with safe relocation |
| Durable job CWD authority drift | Execution/migration-sensitive state | Submission now materializes one canonical path plus filesystem identity in the authenticated payload; projections must agree, and nonterminal work is refused for migration |
| Absolute context-job result/package paths | Migration-sensitive state | Successful package references are integrity-checked and rewritten under the fresh restore root; legacy source paths are replaced by one-way identifiers |
| Linux `/proc` service identity | Legitimate Linux/Android backend | Supported on migration targets; not claimed equivalent on macOS/Windows |

## Production-path result

Portable core code contains no fixed current checkout, username, `/root`, shared
storage, `/storage/emulated/0`, or CPU-architecture dependency. Platform-specific
candidates remain isolated in `harness2.platforms`. Current provider paths under
the phone home are observations, not packaged source configuration.

## Evidence levels

| Target | Evidence |
|---|---|
| Android/PRoot phone | **TESTED** on the current `aarch64` host before sealing; final sealed-SHA rerun is required |
| Ubuntu/Linux | **TESTED** in Python 3.11, 3.12 and 3.13 GitHub CI |
| Native Termux reinstall procedure | **SUPPORTED**, verified through portable install/provider-absent fixtures; fresh destructive reinstall not performed |
| macOS | **SMOKE-TESTED** import and CLI only |
| Windows | **SMOKE-TESTED** import and CLI only |
| Android bridge/notifications | **PLANNED / NOT IMPLEMENTED** |
| Cloud relay/multi-node runtime | **NOT IMPLEMENTED** |

The migration target is Linux. macOS/Windows always-active service limitations
therefore remain documented rather than being hidden or misrepresented.
