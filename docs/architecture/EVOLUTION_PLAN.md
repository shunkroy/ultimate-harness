# Evolution plan

## Operating method

Every phase follows:

```text
audit -> specify -> implement smallest coherent slice -> test -> review
      -> checkpoint -> promote or roll back
```

Status labels are `idea`, `designed`, `prototyped`, `implemented`, `tested`,
`benchmarked`, `stable`, and `deprecated`. Executable-context research also
records rejected experiments rather than hiding them.

## Phase 0 — verified baseline

Status: tested.

- Harness v2 adapters, jobs, security, supervision, platform simulation, event
  parsing, audit, and integrity checks exist.
- Baseline on 2026-08-12: 122 tests passed and one filesystem-emulation test
  skipped before the new architecture slice.
- Guardian, OpenCode, and Prime health were observed. Zen and local remained
  disabled; Hermes remained implemented but not safely routable.

## Phase 1 — kernel seam, discovery, executable-context proof

Status: tested locally; not benchmarked or stable.

Deliverables:

1. provider-independent kernel contracts and typed registries;
2. evidence-bearing provider/capability descriptors;
3. bounded declarative CLI discovery;
4. `ContextCompiler -> ContextIR -> ContextPackage -> ContextRuntime`;
5. deterministic `query`, `transform`, and provenance-bound `generate`;
6. private context package validation and tamper detection;
7. explicit context compilation queue;
8. always-active service heartbeat and bounded queue processing;
9. architecture fitness and failure-path tests.

Non-goals: automatic web ingestion, arbitrary generated code execution,
distributed state, context inheritance, context bytecode, autonomous skill
promotion, model benchmarking, and a visual dashboard.

Acceptance:

- removing any one provider does not prevent kernel construction;
- context source instructions cannot request shell/network authority;
- generated output is source-supported and traceable;
- service status distinguishes desired mode from observed activity;
- a queued context compiles while no chat request is being processed;
- all old and new tests pass;
- wheel, audit, integrity, status, and doctor checks are verified.

Verification evidence on 2026-08-12:

- 147 tests passed; one PRoot filesystem ownership-emulation test skipped;
- detached service reported exact PID, fresh heartbeat, and repeated cycles;
- a queued context compilation completed without a chat event;
- the resulting package executed validated `query` and `generate` operations;
- doctor, audit chain, and integrity checks passed (Prime source retained one
  unrelated generated-file warning);
- the first-slice wheel was subsequently superseded by the portable 2.1.0
  release wheel documented in the project README.

Rollback: stop `harness svc`, preserve state, restore the previous package, and
restart. No destructive database migration is part of this phase.

## Phase 2 — unified lifecycle and event model

Status: designed.

- typed task/run/attempt/checkpoint state machine;
- append-oriented typed event bus;
- one retry owner per execution layer;
- cancellation and lease heartbeat;
- context execution traces and semantic result schemas;
- health, budget, and resource observations.

## Phase 3 — Skill Foundry and workflow engine

Status: idea/design boundary.

- skill manifest, provenance, versions, dependencies, tests, and trust;
- candidate extraction from repeated verified episodes;
- sandbox validation and independent review;
- skill benchmarks, conflict detection, deprecation, and garbage collection;
- workflow composition and repeated-workflow detection.

## Phase 4 — context state, composition, and optimization

Status: idea.

- state schemas and transactional snapshots;
- dependency resolution, imports, overrides, forks, merges, and semantic diffs;
- semantic execution cache and invalidation;
- hot/warm/cold lifecycle;
- validators and targeted self-revision;
- Context REPL before any IDE investment.

## Phase 5 — evaluation and empirical routing

Status: idea.

- permanent capability benchmark corpus;
- provider scores by task, reliability, latency, cost, privacy, and tool use;
- executable-context comparisons against prompting, RAG, graphs, traditional
  software, and fine-tuning;
- evidence-driven routing and upgrade promotion.

## Phase 6 — capability gap, absorption, and invention

Status: designed concept, not implemented.

- record why tasks underperformed;
- rank gaps by frequency, importance, feasibility, and leverage;
- choose deliberately among skill, tool, adapter, workflow, protocol,
  evaluator, context program, or agent;
- prototype in experimental state;
- test, red-team, benchmark, and only then register/promote.

## Language extraction gates

Python remains the default. Rust or TypeScript enters only at a measured
boundary. Protocol schemas and stored semantic state must remain
language-independent so an implementation can be replaced without redefining
the harness.
