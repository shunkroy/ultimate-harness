# First slice specification

Status: tested locally on 2026-08-12. It is not yet benchmarked or stable.

## Kernel contracts

`harness2.kernel.contracts` defines immutable, JSON-serializable contracts:

- evidence kind and maturity;
- capability evidence and descriptors;
- runtime/provider descriptors;
- execution request, plan, event, and outcome.

`harness2.kernel.registry` owns runtime and capability registration without
importing concrete adapters. Concrete adapters are translated by a composition
layer outside the kernel.

## Discovery contract

CLI discovery accepts allowlisted declarative specifications. A probe may run
only a fixed version command with a short timeout. It records location, version,
transport, authentication variable names, limitations, and declared
capabilities. Discovery produces `local_observation`; it never produces
`test_verified` capability evidence.

## Context package contract

The initial package is a private directory containing:

```text
manifest.json
ir.json
sources/<content-addressed source>.txt
```

The manifest includes schema version, persistent ID, package version, compiler
version, source hash, IR hash, operation contracts, pure/stateful semantics,
permissions, and provenance. Package loading verifies hashes and rejects
symlinks or malformed contracts.

## Compiler grammar

The deterministic text compiler recognizes explicit headings and declarations:

```text
# Concepts
Term: definition

# Rules
Rule text [source line retained]

# Procedures
Procedure: step one -> step two

# Operations
query(topic: string) -> EvidenceSet
transform(text: string, mode: string) -> Text
generate(topic: string) -> EvidenceBrief
```

When no operation section exists, only `query` and `transform` are exposed.
`generate` is not inferred merely because source text asks the system to run a
command. Runtime authority remains `none`; context text never grants shell,
filesystem-write, network, credential, or subprocess permission.

## Execution semantics

- `query`: case-insensitive term search over compiler-created semantic units;
  returns matching excerpts plus source/line/hash provenance.
- `transform`: allowlisted deterministic modes `upper`, `lower`, `title`,
  `compress_whitespace`, and `deduplicate_lines`.
- `generate`: builds an evidence brief from query results and refuses with
  `insufficient_evidence` when no supported units exist. It does not call a
  model in this slice.

Every operation validates required input names and types. Results include an
execution ID, context/version, operation, backend, output, evidence, validation
status, and trace events. No hidden chain-of-thought is stored.

## Always-active contract

Configuration defaults to desired always-active mode. The service writes a
private heartbeat after startup and after each bounded cycle. The heartbeat
contains service PID, boot ID, desired mode, observed state, cycle count,
timestamps, last work type/ID, and a redacted last error.

Each cycle performs at most:

1. one Prime health/supervision action;
2. one encrypted run job;
3. one context compilation job;
4. one heartbeat write.

Service status is `active` only when exact process identity is live and the
heartbeat is fresh. Otherwise it reports `configured`, `stale`, or `down`.

## Acceptance and rollback

- Existing CLI behavior remains compatible.
- `harness platform`, `status`, `doctor`, `audit`, and integrity remain usable.
- New commands: `providers`, `context`, and enhanced `svc status`.
- Source text cannot create arbitrary operations or permissions.
- A compilation job submitted before service startup completes without a chat
  event once the detached service runs.
- All source and result hashes verify.
- All old and new tests pass.

Rollback: stop the service, restore previous code, leave additive context
packages and state untouched. No destructive schema migration is required.
