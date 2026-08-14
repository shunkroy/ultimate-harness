# Checkpoint 3C — bounded execution and migration reproducibility

Baseline: `2a50f050b5f6a15d9d8800bb304a4c60e1214809` (`3.0.0.dev1`)

## Scope

Checkpoint 3C seals the existing Python/SQLite harness for migration. It does
not introduce a new workflow architecture, provider bus, OS sandbox, distributed
mesh, Context v2, GUI, or post-migration MAIN_CORE.

The checkpoint owns these invariants:

1. External CLI providers run only through one bounded, shell-free subprocess
   boundary.
2. Working directories are resolved once to an existing canonical directory,
   bound to filesystem identity, persisted before durable submission, and
   identity-checked again without re-resolution before spawn.
3. Provider environments are allowlisted, credentials are scoped separately,
   and temporary files use a Harness-owned private directory.
4. Timeout, output overflow, spawn failure, non-zero exit, malformed JSONL,
   missing terminal events, and trailing records fail closed with typed results.
5. Provider executable paths are canonicalized and revalidated before use;
   unsupported PowerShell-script overrides are not represented as executable.
6. Each invocation has a deterministic configuration SHA-256. Private prompt
   paths and secret environment values are replaced by fixed markers before the
   fingerprint is computed.
7. Platform identity and primitive capabilities are explicit. Android/Termux
   functionality remains supported rather than being replaced by PC-only logic.
8. Durable payload cleanup cannot use an arbitrary path supplied by SQLite.
9. Migration state uses SQLite's online backup API, full integrity checks,
   authenticated encrypted archives, a verified Git bundle, and separate secret
   transfer.
10. The normal bundle is scoped to sealed `HEAD` ancestry and scanned against
    current secret material/strong credential signatures; a full-ref bundle is
    available only inside the encrypted emergency artifact.

## Implemented boundary

`harness2.execution` provides immutable `ProcessRequest` and `ProcessResult`
contracts, bounded concurrent stdout/stderr readers, timeout and overflow
termination, POSIX process-group cleanup, best-effort Windows process-group
cleanup, canonical CWD validation, bounded HTTP-body reads, and redacted
configuration fingerprints.

OpenCode, Prime Agent, Hermes, provider discovery, and diagnostic version probes
use this boundary. Local loopback HTTP responses are byte-bounded. Hermes also
enforces its disabled-by-default policy inside the adapter, so direct invocation
cannot bypass routing policy.

The configuration fingerprint is reproducibility evidence for the invocation
configuration. It is **not** a claim that an external model/provider will return
identical output. External provider execution remains nondeterministic and
unbenchmarked unless separately proven.

## Platform capability model

The platform report distinguishes `android-termux`, `android-proot`, `linux`,
`macos`, and `windows`. It reports filesystem, shell, provider CLI, network,
process spawning, persistent storage, notifications, and Android bridge support
independently. Notifications and Android bridge integration remain explicitly
`not_implemented`; no capability is invented from the platform name alone.

## Compatibility

- Kernel schema remains additive at version 4; migrations 1–4 are unchanged.
- The stable public installer remains exactly v2.1.1 with its existing checksum.
- v2.1.1 tables and old-reader behavior are retained.
- No V3 public release is created by this checkpoint.
- Automatic multi-provider fallback remains the explicitly labeled compatibility
  path; explicit provider execution continues through the typed kernel lifecycle.

## Security properties

- No provider invocation uses `shell=True`.
- OpenCode and Prime prompts remain in mode-0600 private temporary files and are
  removed after execution.
- Output memory is bounded; overflow terminates execution rather than truncating
  and claiming success.
- Strict adapter parsing rejects malformed or post-terminal records while the
  parser's tolerant diagnostic mode remains available to callers that request it.
- Migration encryption is AES-256-CTR/PBKDF2 with encrypt-then-MAC HMAC-SHA256;
  the key is never included in the package.
- Classified current secret values are excluded from manifests, reports, and
  normal package plaintext. Sealed Git ancestry is scanned for exact matches and
  strong credential signatures; unknown historical credentials remain an
  explicitly documented residual risk requiring independent review/scanning.

## Honest limitations

- A real OS sandbox is still not implemented.
- Exact descendant termination is tested on POSIX. Native Windows termination is
  best effort until a Job Object backend exists.
- Executable revalidation narrows symlink/path races but cannot remove every
  filesystem TOCTOU race available to a privileged attacker.
- CWD identity checks reject ordinary path/symlink retargeting, but Python's
  cross-platform `Popen(cwd=...)` remains pathname-based and cannot provide a
  fully descriptor-bound spawn on every supported OS.
- macOS and Windows have import/CLI smoke evidence only; always-active lifecycle
  equivalence is not claimed there.
- Existing external model output is not bit-reproducible.
- Android notifications and Termux:API bridging remain future adapters.

## Migration outputs

The tracked package builder and restore tools are under `scripts/migration/`.
The final package is built only after the sealed commit is pushed and CI is
green, allowing its external manifest to record the exact immutable SHA without
creating a Git commit-hash self-reference.
