# Target architecture

## Architectural identity

The Harness Core is the center. OpenCode, Prime, Hermes, models, MCP servers,
skills, CLIs, and future runtimes are replaceable capability providers.

```text
users / APIs / files / timers / watchers
                  |
                  v
          provider-independent kernel
       identity | lifecycle | policy | events
                  |
        +---------+----------+
        |                    |
 capability/provider bus   context subsystem
        |                  compiler -> IR -> runtime
        v                    |
 agents / models / tools / skills / context programs
        |                    |
        +---------+----------+
                  v
       bounded execution and verification
                  v
       experience, benchmarks and evolution
```

## Kernel boundaries

The deterministic kernel owns:

- task, run, attempt, event, and checkpoint identity;
- lifecycle state and legal transitions;
- provider/runtime descriptors;
- capabilities, evidence, maturity, and health;
- permissions and risk classes;
- execution plans, retries, fallback, cancellation, and budgets;
- append-oriented audit events and recovery metadata.

The kernel speaks through language-neutral typed objects. JSON Schema is the
initial interchange format. No kernel module imports a concrete provider.

## Capability and provider buses

A capability is independent of its provider. A provider descriptor declares
interfaces, transports, authentication names, limitations, observed health,
and evidence. Capability trust is never inferred from marketing or mere binary
presence.

The universal runtime port starts with:

```text
descriptor()
status()
execute(request)
```

Streaming, cancellation, sessions, estimates, and provider-specific extensions
are capability-gated future additions rather than fake common-denominator
methods.

## Executable context subsystem

```text
raw source -> ContextCompiler -> ContextIR -> ContextPackage
                                                |
                                                v
                                         ContextRuntime
                                      plan -> execute -> validate
```

ContextIR is language-independent and preserves source hashes, line-level
provenance, uncertainty, concepts, rules, procedures, operations, contracts,
permissions, and tests. A ContextProgram is distinct from an agent, model, and
skill. It is executable only when operations have defined semantics and runtime
integration.

Initial semantic instructions are deliberately small: `FIND`, `LOAD_INPUT`,
`TRANSFORM`, `COMPOSE`, `VALIDATE_PROVENANCE`, and `RETURN`. They map to
deterministic code first. Model execution becomes an explicit planned backend,
not an implicit concatenated prompt.

## Always-active runtime

`ALWAYS_ACTIVE=true` is the desired default. It is not proof of activity.
Observed state requires all of:

1. an exact live service process;
2. a fresh private heartbeat;
3. bounded successful service cycles;
4. truthful reporting of recent errors and last work.

The initial maintenance loop supervises Prime when configured, processes one
encrypted run job, processes one explicit context-compilation job, writes a
heartbeat, and sleeps. Future watchers, indexing, tests, consolidation, and
profiling are added only with budgets and evidence. Chat is one event source;
absence of chat is not service idle or service death.

## Programming-language architecture

Python is the canonical initial implementation language for orchestration,
registries, discovery, context compilation/runtime, evaluation, skills,
workflows, and evolution logic.

Rust is reserved for measured needs such as process supervision, secure native
execution, high-concurrency transport, resource governance, filesystem
watching, or portable binaries. A Python-to-Rust extraction requires profiling,
compatibility tests, regression tests, and rollback.

TypeScript is used at TypeScript-native boundaries such as OpenCode plugins,
SDK integration, browser UI, and dashboards. It is not the core merely because
OpenCode uses it.

All major boundaries use language-neutral contracts so Python can later be
replaced without losing capabilities, context packages, memory, benchmarks, or
task history.

## Evolution controls

Capability absorption and invention follow:

```text
discover -> model -> adapt/prototype -> test -> benchmark -> register
         -> preserve provenance -> promote or reject
```

Self-improvement follows:

```text
observe -> hypothesis -> candidate patch -> test -> benchmark -> review
        -> checkpoint -> promote/rollback
```

Stable, candidate, experimental, quarantined, deprecated, and rejected states
remain explicit. Model confidence never substitutes for evidence.
