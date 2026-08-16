# OmniRouter — Managed External Agents (M4.x, DOCUMENTED / DESIGNED)

Status: **DOCUMENTED** (requirement recorded by Animesh, 2026-08-16). Not
IMPLEMENTED — no IMPLEMENTED claim until a real external runtime
actually executes through Harness.

## Principle

Harness is a meta-runtime / OmniRouter. It runs and orchestrates
external agents — OpenCode, Hermes, Prime Agent, C/Codex, local model
runtimes, cloud APIs, approved CLIs and services, future harnesses —
as **managed, replaceable workers**. Harness stays authoritative over
identity, context, memory, events, provenance, permissions, secrets,
jobs, routing, fallback, tool access, cwd, lifecycle, logging and
state. **No external agent ever owns Harness state**: identity, state,
memory, context, worlds and events survive any provider's loss.

## Adapter classes

1. **API Provider Adapter** — cloud/API AI (e.g. Gemini, Groq).
2. **Local Model Adapter** — local model runtimes (ollama, llama.cpp,
   etc.), including deterministic offline engines.
3. **CLI Agent Adapter** — agent CLIs (opencode, hermes, prime, codex).
4. **Process Runtime Adapter** — arbitrary governed subprocesses.
5. **IPC / Service Adapter** — long-lived local services (named pipes,
   sockets, DBus).
6. **Harness / peer-runtime Adapter** — another Harness instance.

## Managed agent job features (required by the model)

launch/spawn; controlled cwd; governed environment; input/context
injection; capability/tool permissions; secret isolation; timeout and
cancellation; stdout/stderr capture; structured tool-event capture;
exit/result status; provenance; job/session identity; resource limits;
retries; fallback; lifecycle supervision; health detection.

## Routing

Capability-aware routing, never hard-coded to one AI:
Context IR → required capabilities → OmniRouter → selected worker.
Example: a coding task needing repo + compiler routes to C or
OpenCode; an offline world summary routes to the deterministic
engine; dialogue prose routes to an approved local/cloud AI.
If a backend disappears, Harness falls back among ≥2 compatible
backends without losing state.

## Integration

- **Context-as-Agent**: the Context IR/Compiler output is itself an
  agent-capable context consumed by whichever worker Harness selects.
- **Teacher/Student**: a strong worker teaches through Harness gates
  (validation, sandbox, governor); accepted learning lands in the
  offline student. The teacher is not permanently required.

## M4.x checkpoint plan (added to the M4 sequence)

Runs AFTER the foundational Context Runtime (M4.3–M4.5) is stable:

1. OmniRouter contract + capability model (capability registry,
   job descriptor, result envelope).
2. Native managed-process provider (spawn/govern/capture/cancel/status).
3. First real external-agent adapter — one actual external runtime
   executing through Harness end-to-end.
4. Capability discovery + routing (Context IR → worker).
5. Provenance + lifecycle supervision for managed jobs.
6. Fallback: ≥2 compatible backends, automatic switch, no state loss.

Before designing the native implementation, inspect the existing
Python adapters (`harness2/adapters/{opencode,prime,direct,local,hermes}`)
and preserve their proven behavior without copying the Python
architecture unchanged.