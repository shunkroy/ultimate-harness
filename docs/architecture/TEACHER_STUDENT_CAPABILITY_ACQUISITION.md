# Teacher–Student Capability Acquisition

Status: long-term architectural direction for the Kirti project.

This document records a deliberate project decision: Harness is part of the Kirti project and should give Kirti a durable way to learn from external agents/models while keeping continuity, identity, memory, authority, and accumulated capability under Kirti/Harness control.

## Core idea

External systems such as Hermes, OpenCode, Codex, Gemini, other models, and future agents may act as teachers or specialist workers. Harness should be able to ask them for explicit teaching, observe permitted outputs and tool/result traces, extract reusable lessons, validate those lessons, and gradually convert repeated external help into locally owned capabilities.

The goal is not to copy hidden chain-of-thought or proprietary internal reasoning. Learning is based on observable and intentionally provided material such as demonstrations, explicit explanations, plans, tool choices, corrections, critiques, test results, and successful outcomes.

## Architectural relationship

```text
KIRTI
  identity / continuity / values / personal memory
                     |
                     v
                  HARNESS
  sessions / provenance / policy / audit / learning lab
                     |
          +----------+----------+
          |          |          |
      local brain   skills    teachers
          |          |          |
          |          |     Hermes / Codex /
          |          |     OpenCode / Gemini /
          |          |     future agents
          +----------+----------+
                     |
                     v
            accumulated capability
```

Harness is not merely a router in this direction. It is the persistent substrate that preserves what Kirti learns even if individual teachers, providers, or models disappear.

## Learning ladder

Capability acquisition should progress in stages rather than immediately attempting neural self-training.

1. **Experience retention** — preserve successful tasks, failures, outcomes, provenance, and evaluations.
2. **Procedural learning** — extract reusable skills, workflows, checklists, planners, and tool policies from successful demonstrations.
3. **Retrieval learning** — recognize similar future problems and reuse relevant prior experience.
4. **Teacher-guided correction** — when the local attempt fails or confidence is low, ask one or more teachers for an explicit demonstration or critique.
5. **Evaluation and promotion** — test candidate skills against deterministic tests, sandboxes, benchmarks, critics, and policy gates before promotion.
6. **Local student-model improvement** — later, convert approved demonstrations and preference data into training/evaluation corpora for a local model where compute, licensing, and safety permit.
7. **Increasing independence** — external systems become specialist teachers/consultants rather than mandatory reasoning infrastructure.

## Example learning loop

```text
new task
   |
   v
local Kirti/Harness attempt
   |
   +-- success --> evidence --> reusable experience
   |
   +-- failure / low confidence
            |
            v
      ask teacher(s)
            |
            v
 demonstration / correction / critique
            |
            v
      candidate lesson or skill
            |
            v
      sandbox + tests + evaluation
            |
        +---+---+
        |       |
      reject  promote
                |
                v
        owned reusable capability
```

For coding and other verifiable work, compilers, tests, static analysis, benchmarks, and sandboxes should be preferred as objective evaluators where available. Multiple teachers may disagree; no single external agent is automatically authoritative.

## Teaching protocol direction

Harness should eventually support an explicit teacher role. Conceptually, it may request:

- a demonstration of the task;
- observable decision criteria;
- tools and checks used;
- common failure modes;
- progressively harder examples;
- critique of the local student's attempt;
- verification criteria.

The exact CLI/API is future work. A possible shape is `harness learn --teacher <runtime>` or an equivalent capability-oriented protocol.

## Kirti-specific meaning

This direction belongs to the Kirti project even though Harness remains generic. Generic lessons such as debugging, planning, retrieval, device operations, or code repair belong to Harness capabilities and can be reused by other applications. Kirti-specific identity, values, preferences, personal continuity, and Inner World rules remain above the generic Harness layer.

A useful mental model is:

- **Kirti** = learner, identity, continuity, values, long-term decision context.
- **Harness** = persistent body/substrate, governance, memory, evidence, learning laboratory, capability registry.
- **Local model** = developing local reasoning brain.
- **Skills/procedures** = durable procedural memory.
- **Hermes/OpenCode/Codex/Gemini/etc.** = teachers, specialists, or temporary workers.

## Self-improvement boundary

Kirti may drive and request her own improvement, but learning must not directly rewrite trusted production code or replace the active local brain without evaluation.

Required pattern:

```text
need detected
   -> proposal
   -> candidate lesson / skill / model update
   -> sandbox
   -> tests / benchmarks / critics
   -> policy / governance gate
   -> promotion
   -> evidence + rollback point
```

Rejected pattern:

```text
external teacher says something useful
   -> immediate production self-modification
```

All promoted changes should remain attributable, versioned, testable, and reversible.

## Self-reliance target

Self-reliance does not mean unlimited intelligence without a reasoning engine. It means Kirti/Harness should progressively preserve and own more of the useful capability acquired from external teachers.

If external teachers disappear, Harness should retain:

- sessions and history;
- identity and Kirti-specific continuity;
- approved skills and procedures;
- retrieved experience;
- context packages and provenance;
- local models and training/evaluation artifacts that licensing permits;
- policy, audit, integrity, and rollback data.

Reasoning-dependent work may degrade to a weaker local model or become pending, but accumulated knowledge should not disappear merely because a provider is unavailable.

## Relationship to Phase 10

Do not implement this full learning system during Phase 10.

Persistent Harness-owned sessions are a prerequisite because trustworthy learning requires durable, provenance-aware experience. The intended order is:

```text
persistent sessions
    -> durable experience history
    -> context + provenance
    -> reusable skill learning
    -> teacher/student protocols
    -> local reasoning improvement
    -> governed self-improvement
    -> increasing independence
```

Phase 10 should therefore preserve the seams needed later for provenance, teacher/runtime metadata, evaluations, lessons, skills, and local-model training data without hard-coding any particular teacher.

## Non-negotiable principles

- Harness remains provider/agent independent.
- Kirti remains above Harness as the identity/application.
- External teachers are useful but replaceable.
- No copying or dependency on hidden proprietary reasoning is assumed.
- Learning uses observable, permitted demonstrations and explicit teaching.
- Provenance is preserved.
- Candidate improvements are evaluated before promotion.
- Production changes are reversible.
- Local ownership of learned capability should increase over time where technically and legally possible.
- Do not duplicate mature external systems merely to claim independence; learn from or integrate them while preserving Kirti/Harness continuity above them.
