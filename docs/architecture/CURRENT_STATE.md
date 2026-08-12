# Harness current state

Audit date: 2026-08-12

First-slice verification update: the provider-independent kernel contracts,
capability/provider registries, bounded discovery, executable-context v1, and
always-active heartbeat/queue loop described below are now implemented and
covered by the local test suite. They remain unbenchmarked and are not labeled
stable.

This document is the evidence-based baseline for the Ultimate Harness work. It
describes the v2.1.1 source release; external products are capability providers,
not the identity of the harness. No component is called stable or benchmarked
without corresponding evidence.

## A. Current architecture

The current system is a Python 3.11+ deterministic orchestration layer:

```text
CLI -> configuration -> static adapter registry -> policy -> orchestrator
                                                   |          |
                                                   |          +-> retries/circuits
                                                   +------------> SQLite/audit

service loop -> Prime supervision + encrypted durable-job worker
```

Implemented foundations include private prompt-file transport, credential
scoping, OpenCode and Prime JSONL parsing, retries, circuit breakers, encrypted
jobs, exact-process Prime supervision, a private SQLite store, and a hash-linked
metadata audit ledger. Runtime construction is still centered in
`harness2/cli.py`, and provider selection is partly name- and keyword-based.

## B. Existing providers and CLIs

| Provider/runtime | Local observation | Harness status |
|---|---|---|
| Harness | Python package 2.1.1 | implemented and tested |
| OpenCode | optional CLI | private-file input and JSONL output implemented |
| Prime Agent | optional CLI/source bundle | private-file input, JSONL output and supervision implemented |
| Hermes | optional CLI | adapter implemented; task text is argv-visible |
| OpenCode Zen | optional provider | implemented; requires explicit credential/configuration |
| Local OpenAI-compatible endpoint | optional loopback endpoint | implemented; disabled by default |
| Git, Python, Node, OpenSSL and build tools | environment dependencies/tools | discovered when present; not capability evidence by presence alone |

Authentication names are represented without credential values. Harness knows
`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, and
`OPENCODE_API_KEY`. Secret values must remain in environment-specific secret
storage and must not enter prompts, skills, or logs.

## C. Existing capabilities

Test-backed capabilities include OpenCode execution, Prime execution parsing,
Prime supervision, deterministic routing, persistent circuits, encrypted jobs,
private credential storage, platform discovery, integrity pinning, and audit
verification. Hermes invocation and the local loopback adapter are implemented
but lack direct end-to-end tests against healthy providers.

## D. Existing skills

Harness does not yet own a skill model or Skill Foundry. It can indirectly use:

- OpenCode skills under `~/.config/opencode/skills`;
- Prime built-in skills under `packages/coding-agent/skills`;
- Hermes skill manifests under `~/.hermes/skills`.

Those directories are external skill sources. Presence is not capability
evidence. Harness currently loads the Genius classifier as Python code in the
process; that is a compatibility integration, not a general skill runtime.

## E. Duplication and conflicts

- `build/lib/harness2` is generated duplication and is not architectural
  authority.
- `EngineStatus.capabilities` and `capabilities.py` duplicate declarations.
- Zen is modeled as an engine although it is an OpenCode-backed provider.
- Retry ownership exists in both foreground orchestration and durable jobs.
- Provider-to-secret maps occur in more than one module.
- The README's former description of OpenCode as the brain conflicts with
  provider independence.
- Engine choices are repeated in the CLI parser.
- Service existence and desired always-active mode are not the same as a fresh
  service heartbeat; status must distinguish them.

## F. Historical gaps closed in v2.1.0

The first slice closed the provider-independent kernel/catalog, capability
registry, bounded declarative discovery, executable-context types/runtime, and
truthful always-active heartbeat gaps. Remaining gaps are:

1. No native skill lifecycle, benchmarks, or garbage collection.
2. No persistent typed event bus or unified task state machine.
3. No empirical model/provider score history.
4. No execution-cost/resource governor.
5. No semantic context composition, cache, or semantic diff.

## G. What must be preserved

- Existing CLI compatibility unless an explicit migration is published.
- Metadata-only run records and encrypted queued prompts.
- Private prompt transport for OpenCode and Prime.
- Strict terminal-event parsing and partial-output error handling.
- Credential-minimized subprocess environments.
- Exact PID/socket supervision and filesystem/symlink defenses.
- Guarded-root policy and read-only untrusted-agent behavior.
- Additive, recoverable changes; no destructive database migration.

## H. Proposed Harness Kernel

The kernel owns identity, typed lifecycle contracts, runtime/provider
descriptors, capabilities and evidence, policy decisions, execution plans,
events, and checkpoints. It depends on ports (`RuntimeDriver`, state, audit,
secrets, clock), never on OpenCode, Prime, Hermes, or any model directly.

## I. First implementation slice

The implemented first slice is intentionally bounded:

1. provider-independent kernel contracts and runtime/capability registries;
2. declarative, non-destructive CLI discovery;
3. compatibility mapping for current adapters;
4. a minimal executable-context pipeline supporting deterministic `query`,
   `transform`, and provenance-bound `generate` operations;
5. a real service heartbeat and bounded context-compilation queue so
   `ALWAYS_ACTIVE=true` means work can continue without chat input.

It does not yet implement autonomous skill promotion, distributed execution,
semantic bytecode, or automatic model learning.

## J. Test and acceptance criteria

- Kernel code imports no concrete provider adapters.
- A provider can be removed without preventing registry/kernel construction.
- Capabilities carry maturity and evidence separately from live health.
- Discovery is declarative, bounded, and does not execute arbitrary help text.
- Context packages have operations, input contracts, execution semantics,
  validation, hashes, and source provenance.
- Context execution never grants shell/network authority from source text.
- `generate` fails rather than fabricating unsupported evidence.
- A detached service publishes a fresh heartbeat and performs bounded work when
  no chat request exists.
- Desired always-active mode and observed active state are reported separately.
- Existing tests continue to pass; new architecture fitness tests pass.

## K. Risks and rollback

Primary risks are behavioral drift, false capability promotion, stale service
processes, package tampering, source-path leakage, and accidentally treating
ingested instructions as authority. Mitigations are compatibility façades,
fail-closed validation, private state, bounded queues, deterministic execution,
and explicit evidence labels.

Rollback is file-level: stop the Harness service, retain the SQLite database and
context sources, restore the previous tagged source or installed wheel, and
restart the old service. The first slice uses additive files and avoids
destructive schema changes.
