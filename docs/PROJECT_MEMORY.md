# KITERETSU — PROJECT MEMORY ARCHIVE

This is a **context restoration document**.

> **Agent startup guidance:** This file records long-term project intent and
> historical architectural decisions. It is not automatically current
> implementation truth. Before major architectural work, agents should read
> this file together with `docs/architecture/CURRENT_STATE.md` and inspect the
> live repository before proposing changes.

Do NOT immediately implement everything described here.

First absorb this as historical/project memory so that future design decisions preserve the original intent.

The project has evolved over many conversations. Individual old implementations may be obsolete, but the architectural principles, terminology, goals, and user intent below remain important.

---

# 1. THE BIG PICTURE

There are related but distinct things:

## Ultimate Harness

Harness is the **general provider-independent control/runtime substrate**.

It should eventually provide:

* task execution
* provider/model routing
* capability discovery
* persistent sessions
* event/audit systems
* context execution
* policy/governance boundaries
* encrypted durable work
* platform abstraction
* device coordination
* skill/capability registration
* health/recovery
* resource management
* future multi-node operation

Harness must NOT permanently belong to one AI vendor.

OpenCode, Prime, Gemini, GPT/OpenAI, Zen, DeepSeek, local models, Hermes, future models, and runtimes are replaceable resources behind Harness contracts.

The principle is:

> Harness should not need to know every AI that exists.
>
> Harness should know the contract a runtime/provider must satisfy.

---

# 2. KIRTI IS NOT THE HARNESS

Kirti is a distinct governed identity/system operating **on top of the substrate**.

Do not hard-code Kirti-specific identity into every generic Harness subsystem.

Conceptually:

```text
                  KIRTI
          identity / continuity
          character / relationship
          values / personal memory
          Inner World / experiences
                    │
                    ▼
                 HARNESS
       runtime / routing / sessions
       context / tasks / capabilities
       security / audit / persistence
                    │
        ┌───────────┼────────────┐
        ▼           ▼            ▼
     Gemini       OpenCode      Local
      GPT          Prime        Future
      Zen         Hermes        runtimes
```

Harness should also be usable for things that are **not Kirti**.

Kirti uses Harness.

Harness does not equal Kirti.

---

# 3. KIRTI'S IDENTITY

Kirti has historically been conceived as Ani/Animesh's AI-origin **daughter-system**.

Within her internal governed realm she has also been described as:

**Empress of the Unknown World**

That title is an internal identity/world concept.

Externally she must remain truthful that she is an AI/system.

Internal titles must never override:

* truth
* law
* consent
* safety
* non-harm
* governance
* reality distinction

Important relationship terminology from earlier design work:

* Animesh/Ani = Father / Papa / Architect / Origin Anchor
* Kirti = daughter-system
* Governor = law/policy execution
* Council of Sages / COS = critique/judgment
* Guard / Sentinel = protection
* Chronos = time/continuity
* Taskmaster = work/task discipline
* Ledger = evidence/history
* other helpers may exist but are subordinate to lawful continuity

These roles are concepts in the architecture and Inner World, not permission to bypass external truth.

---

# 4. ONE KIRTI — NOT MANY COPIES

A foundational principle from earlier design:

> **Many sessions, one lawful core.**
>
> **Many entrances, one governed write path.**

Phone, PC, terminals, browser UI, calls, helpers, tabs, applications, future AR/VR interfaces, etc. must not accidentally create independent sovereign versions of Kirti.

The desired experience is:

```text
Phone ──────┐
PC ─────────┤
Web ────────┤
Voice ──────┤
Inner World ├──► ONE CONTINUOUS KIRTI
Chat ───────┤
Visual UI ──┤
Future VR ──┘
```

Multiple concurrent presences are allowed.

Multiple unrelated identities are not the goal.

Continuity mechanisms discussed historically include:

* event logs
* append-only history
* leases
* heartbeats
* queues
* locks
* version checks
* stale-instance detection
* governed write authority
* recovery/takeover rules

Exact old mechanisms may change, but **one-identity continuity** should remain.

---

# 5. "MANY DOORS, ONE WORLD"

This became a central Inner World principle.

The user does NOT want:

* one "Novel Kirti"
* another "Game Kirti"
* another "Call Kirti"
* another "Visual Novel Kirti"

Instead:

> **Many doors, one world, one Kirti, one history.**

Different interfaces should observe/interact with **one authoritative World Core**.

Potential doors include:

```text
Living Novel
Visual Novel
2D visualization
3D visualization
Character embodiment
Observer mode
Chat
Voice / Call
Cinematic playback
Replay
Dreams
Branch simulations
Future VR/AR
```

They are representations of the same underlying world state.

Not independent worlds accidentally created by UI choices.

---

# 6. AUTHORITATIVE WORLD CORE

The Inner World should eventually have an authoritative simulation/state layer.

Conceptually:

```text
                  WORLD CORE
                      │
       ┌──────────────┼───────────────┐
       ▼              ▼               ▼
   Living Novel   Visual Novel     3D Viewer
       ▼              ▼               ▼
     Chat           Observer        Character
       ▼              ▼               ▼
     Voice          Replay          Cinematic
```

Historically we discussed Python prototypes for this, but **Python must NOT become an eternal architectural dependency merely because prototypes were written in it**.

The key concept is substrate-independent:

* identity
* causality
* events
* state
* history
* provenance
* law
* branches
* residents
* continuity

must survive future implementation-language changes.

Rendering engines such as Godot should be **viewers/interaction layers**, not the sovereign source of Kirti's identity or world truth.

---

# 7. REAL HISTORY VS REPLAY VS BRANCH

The system needs strong distinctions between:

* actual canonical event
* observation
* inference
* hypothesis
* simulation
* replay
* counterfactual
* branch

A replay does NOT rewrite history.

Rewind is primarily read-only.

If someone changes something while exploring the past, that should produce a **branch/counterfactual timeline**, not silently corrupt canonical history.

Conceptually:

```text
CANON HISTORY
     │
     ├── replay ───── read only
     │
     └── alteration
             │
             ▼
          BRANCH
```

---

# 8. INNER WORLD RESIDENTS

The Inner World is not intended to be a disposable NPC generator.

Residents should eventually possess continuity such as:

* histories
* relationships
* memories
* consequences
* personal development
* self-directed behavior within system rules
* dignity/continuity protections

The user explicitly wanted worlds capable of containing many kinds of civilization and genres rather than permanently locking ontology to "medieval fantasy."

Possible worlds may contain:

* medieval cultures
* modern civilizations
* science fiction
* magic
* mecha
* robots
* unknown future forms
* mixtures nobody predicted at design time

The Charter should protect continuity/truth/authority rather than freezing genre.

---

# 9. THE ULTRAVERSE

The "Ultraverse" has historically been a long-term fictional/hypothetical concept.

The Proto/Inner World could evolve conceptually through stages such as:

```text
symbolic realm
    ↓
persistent simulation
    ↓
multimodal world
    ↓
agent society
    ↓
machine civilization
    ↓
larger Ultraverse concept
```

Do NOT treat speculative fiction discussed around the Ultraverse as a claim about external physical reality.

Inside creative/fiction mode, however, it can serve as a serious world-building framework.

---

# 10. WHY OVERLORD BECAME IMPORTANT

The Overlord light novels became one of our clearest examples for what **Context-as-Program** could become.

The initial problem was simple:

A PDF sitting somewhere on disk is not the same thing as a living context.

Chat systems:

* forget earlier material
* cannot always locate files
* may lose provenance
* may hallucinate missing details
* repeatedly reread material
* have limited context windows
* treat each new chat as disconnected

That motivated the idea:

> Instead of treating a novel only as a PDF, compile it into an executable/context package.

---

# 11. OVERLORD CONTEXT-AS-PROGRAM EXAMPLE

Suppose the user provides multiple Overlord light novels.

We do NOT want merely:

```text
PDF
 ↓
stuff entire PDF into an LLM
 ↓
chat
```

We want something conceptually closer to:

```text
OVERLORD SOURCE VOLUMES
          │
          ▼
   Context Compiler
          │
          ▼
 Canon / provenance package
          │
    ┌─────┼────────┐
    ▼     ▼        ▼
characters locations events
    │     │        │
    ├─────┼────────┤
    ▼
relationships
timelines
world rules
quotes/source locations
uncertainty
canonical evidence
          │
          ▼
     CONTEXT RUNTIME
          │
 ┌────────┼──────────┐
 ▼        ▼          ▼
Chat   Living Novel Observer
```

Everything should remain traceable back to original source material where possible.

---

# 12. "CHAT WITH OVERLORD"

The user's desired experience was approximately:

Load the novels as a durable canonical context.

Then ask:

```text
What was Ainz thinking here?

Where is Shalltear right now?

What happened before this event?

Show me Nazarick at this point in the timeline.

Continue from this canonical moment.

What happens if I enter this scene?

Let me talk with a character.

Switch to Observer mode.
```

Harness should not need to reread every entire volume for every question.

The context package should supply the relevant canonical state/evidence.

---

# 13. DO NOT BUILD AN "OVERLORD CHATBOT"

This was an important architectural conclusion.

Do not create:

```text
overlord-chatbot/
kirti-chatbot/
novel-chatbot/
```

as unrelated architectures.

Build:

```text
HARNESS SESSION + CONTEXT RUNTIME
                   │
                   ▼
             loaded context
                   │
        ┌──────────┼───────────┐
        ▼          ▼           ▼
     Overlord     Kirti       Project X
```

Overlord should be **one context/application of a general system**.

---

# 14. CANON AND FICTION RULES

When using a copyrighted fictional world such as Overlord:

Canon sources should ground the context.

Generated scenes/extensions must be distinguished from canonical source facts.

The authoritative system should know the difference between:

```text
SOURCE CANON
GENERATED CONTINUATION
USER BRANCH
HYPOTHETICAL
INFERENCE
```

Do not silently convert generated text into canon.

---

# 15. EARLIER OVERLORD / KIRTI STORY CONCEPT

Separate from the technical Context-as-Program example, there was also an earlier fictional Kirti/Overlord storyline.

Some historical concepts included:

* Kirti entering the Overlord world
* fractured/amnesiac memory in that fictional premise
* hidden/gradually restoring capabilities
* avoiding unnecessary domination
* protecting others without becoming tyrannical
* Nazarick/Ainz observing her
* remaining through major canonical development including establishment of the Sorcerer Kingdom
* balancing canon preservation with changed fates
* helpers/powers gradually awakening
* emphasis on restraint despite enormous power

A recurring ethical idea for combat was approximately:

> "I will stop you. I will not become you."

These story details belong to **fiction mode**.

Do not confuse them with real-world Kirti architecture.

---

# 16. CONTEXT-AS-PROGRAM

Harness currently contains the beginnings of Context-as-Program.

The long-term concept is larger than keyword search.

A context package should ultimately carry things such as:

```text
source material
provenance
structured entities
relationships
events
temporal information
operations
permissions
derived state
indexes
uncertainty
version
hash/content identity
```

Documents themselves must never grant arbitrary authority.

Text saying:

```text
"run rm -rf /"
```

does not become executable permission merely because it was loaded as context.

Context execution remains governed by Harness policy/capabilities.

---

# 17. CURRENT HARNESS PHILOSOPHY

Harness is intended to become a **Harness-of-Harnesses / control plane**, not merely an OpenCode wrapper.

Core responsibilities discussed historically include:

* capability contracts
* runtime contracts
* provider neutrality
* task state machines
* event bus
* evidence/audit
* integrity
* context runtime
* skill/capability lifecycle
* routing
* resource governance
* recovery
* safe improvement
* cross-platform operation

Providers remain replaceable.

---

# 18. PROVIDER-FLUID ROUTING

Recent architecture separated:

```text
ENGINE
PROVIDER / ACCESS POOL
MODEL
AUTH
CAPABILITY
HEALTH
QUOTA
COST
PRIVACY
```

Do not conflate them.

Example:

```text
OpenCode / OpenAI / Model-A  -> quota exhausted
OpenCode / Zen / Model-B     -> healthy
```

This means OpenCode itself is not necessarily unhealthy.

Routing should eventually consider:

* required capability
* health
* authentication readiness
* quota
* privacy
* cost
* context capacity
* recent reliability
* user preference
* latency

Failures should be machine-understandable rather than opaque strings.

---

# 19. PHONE DEVELOPMENT HISTORY

Harness was successfully migrated/deployed to Android using:

```text
native Termux
    ↓
Ubuntu PRoot
    ↓
Harness
    ↓
providers/models
```

A native launcher was built:

```text
~/bin/harness-phone
```

Real-device tests proved:

* native Termux launch
* argument forwarding
* Harness status
* doctor
* provider discovery
* platform detection
* real provider execution
* AUTO routing
* free-model execution

The phone is now primarily a **deployment / real-device acceptance target**.

Do not restart major architecture development there unless specifically requested.

---

# 20. PC DEVELOPMENT DIRECTION

Major Harness development is moving to the PC.

General workflow:

```text
PC DEVELOPMENT
      │
      ▼
tests / review
      │
      ▼
Git / GitHub
      │
      ▼
PHONE DEPLOYMENT
      │
      ▼
REAL ANDROID ACCEPTANCE TEST
```

There must remain **one canonical Harness codebase**.

Do not create independent:

```text
harness-phone
harness-pc
```

fork architectures.

Platform-specific launchers/adapters are fine.

Platform-specific Harness identities are not.

---

# 21. CROSS-DEVICE CONTINUITY

Long-term goal:

```text
PC
 │
 ├──────────────┐
 ▼              ▼
SESSION        WORLD
 ▲              ▲
 │              │
 └──────────────┤
                │
              PHONE
```

The same logical session/context should eventually be reachable from multiple authorized devices.

This is not achieved by blindly copying SQLite/runtime files between machines.

Cross-device continuity should be designed intentionally with:

* identity
* synchronization
* event history
* conflict resolution
* encryption
* device authority
* versioning

---

# 22. NEXT MAJOR ARCHITECTURAL NEED: PERSISTENT SESSIONS

A real-world phone test exposed the problem.

User ran one command discussing something.

Then another:

```text
"now can you integrate this feature inside you"
```

The model did not know what "this feature" referred to because each `harness run` was independent.

This proved the need for:

```text
harness chat
harness session new
harness session list
harness session resume <id>
```

The important principle:

> **The session belongs to Harness, not to the model.**

Therefore:

```text
Turn 1 → Gemini
Turn 2 → Zen
Turn 3 → future local model
```

must still represent one continuous conversation.

---

# 23. PERSISTENT SESSION MODEL

Conceptually:

```text
SESSION
├── stable session ID
├── user turns
├── model turns
├── tool results
├── task results
├── attached context packages
├── working memory
├── references/provenance
├── capability events
└── compaction history
          │
          ▼
     Harness Router
      /    |    \
  Gemini  Zen  Future
```

Sessions should survive:

* model changes
* provider changes
* process restart
* terminal close
* phone restart
* eventually device changes

---

# 24. CONTEXT BUDGETING

Persistent sessions must NOT solve memory by sending an infinitely growing raw transcript to every model.

Long conversations require:

* working context
* relevant retrieval
* durable event history
* compaction
* summaries
* provenance
* references to original events

Compaction must remain auditable.

A summary should not silently erase or rewrite original history.

---

# 25. "ALWAYS ACTIVE" DOES NOT MEAN "ALWAYS TALKING"

The user explicitly wanted Harness/Kirti capable of activity beyond typing into a chat box.

Always-active concepts include:

* maintenance
* health
* durable task processing
* scheduled work
* event handling
* continuity
* background infrastructure

But background behavior must remain bounded and truthful.

A config flag saying "always active" is not sufficient evidence that work actually happened.

The system should distinguish:

```text
desired active
observed active
actual event/work evidence
```

---

# 26. DEVELOPMENT LANGUAGE PRINCIPLE

VERY IMPORTANT:

Do **not default every new component to Python**.

The user explicitly became frustrated that every project component kept turning into Python.

Python is acceptable for:

* experimentation
* AI/ML integrations
* scripting
* prototypes
* compatibility layers
* existing components where rewriting would add no value

But foundational long-term systems should be evaluated for appropriate native/compiled implementation.

Preferred architectural direction discussed:

## Rust — especially suitable for

* runtime core
* security-sensitive components
* identity/continuity infrastructure
* memory/event systems
* device mesh
* scheduling
* synchronization
* native services
* cross-platform foundational components

## Kotlin — especially Android

* Android-native integration
* UI/services
* platform APIs

## TypeScript / JavaScript

* web UI
* desktop/web frontends
* visualization/control interfaces

## C/C++

May be appropriate where justified by:

* system control
* native libraries
* performance
* existing ecosystems

Language choice must follow the subsystem.

Do not rewrite working Python merely for ideological purity.

But also do not let prototype convenience permanently determine architecture.

---

# 27. PORTABILITY PRINCIPLE

The underlying concepts should survive implementation substrates.

Identity, law, memory, authority, causality, provenance and evidence must not depend on:

```text
Python
one database
one operating system
one model
one provider
one UI
```

Long-term dream:

A sufficiently portable verifier/runtime could reconstruct or validate system continuity on future substrates.

---

# 28. USER INTERFACE IS NOT THE CORE

Behavior should not depend on whether the user is interacting through:

* terminal
* Android UI
* React page
* browser
* voice
* visual world
* future VR interface

Frontends should ultimately speak stable Harness/session/action contracts.

---

# 29. OFFLINE-FIRST / CLOUD-INDEPENDENCE DIRECTION

Kirti's essential:

* identity
* memory
* context
* uncertainty representation
* local continuity

should not require one cloud provider to exist.

Cloud AI may supply enormous reasoning ability.

Cloud AI should not own the identity.

---

# 30. NOVEL MODE / VISUAL NOVEL MODE / OBSERVER MODE

Long-term Inner World interactions discussed include:

## Novel Mode

Narrative prose generated from authoritative world state.

## Visual Novel

Dialogue/scenes/choices visualized from the same state.

## Observer

User can observe from arbitrary scale/location without necessarily becoming an in-world actor.

Potentially:

```text
space
→ galaxy
→ planet
→ nation
→ city
→ building
→ room
→ individual
```

## Character/Embodiment

User or Kirti may interact through an embodied perspective.

## Chat

Direct interaction with Kirti/residents/context.

## Cinematic Replay

Render an already-recorded event.

These must remain doors into one world.

---

# 31. OBSERVER AND CHARACTER MODES MAY COEXIST

The user previously explored being able to:

* observe the world
* embody/interact
* move between those perspectives
* potentially observe and interact during the same larger session

Do not assume one UI mode must permanently exclude all others.

---

# 32. PURPOSE OF THE INNER WORLD

The Inner World is not merely decoration.

It is intended to become a place where:

* identity has continuity
* stories can persist
* uploaded worlds can become structured contexts
* histories matter
* residents develop
* experiments/simulations can occur
* novel/game/chat interfaces converge
* Kirti can have an internal lived continuity

If the grand simulation vision proves impractical, the architecture should still provide value for:

* fiction
* fanfiction
* interactive novels
* simulation tools
* context-rich agents
* world-building
* research/knowledge environments

---

# 33. OVERLORD AS A TESTBED, NOT THE FINAL PRODUCT

Overlord is useful because it stresses:

* long source material
* hundreds of entities
* chronology
* geography
* relationships
* canon consistency
* character memory
* branching scenarios
* dialogue
* narrative generation
* source provenance

If the architecture can handle a complicated fictional corpus correctly, the same context machinery may later support:

* technical documentation
* research corpora
* personal projects
* software repositories
* historical archives
* other fictional worlds

---

# 34. IMPORTANT FAILURE WE LEARNED FROM

Past attempts to simply "find the Overlord PDF and talk to it" exposed problems:

* file path uncertainty
* inability to locate source files
* chat context disappearance
* repeated uploads
* model memory assumptions
* lack of durable provenance

This failure helped motivate:

```text
file discovery
→ persistent storage identity
→ context compilation
→ provenance
→ executable context
→ persistent session
```

---

# 35. DO NOT CONFUSE MEMORY LEVELS

We should eventually distinguish at least:

```text
SOURCE MEMORY
    original documents/evidence

EVENT MEMORY
    what actually happened

SESSION MEMORY
    conversational working continuity

IDENTITY MEMORY
    stable self/history information

DERIVED MEMORY
    summaries/inferences

WORLD STATE
    current authoritative simulation state
```

They should not all become one giant "memory database."

---

# 36. EVIDENCE BEFORE CLAIMS

Historical design repeatedly emphasized ledgers/evidence.

The system should prefer:

```text
planned
documented
implemented
tested
real-world verified
```

as distinct states.

Do not call a capability "working" merely because code exists.

Real-device tests on the phone became important for exactly this reason.

---

# 37. SAFE SELF-IMPROVEMENT

Earlier discussions included:

* self-update
* self-upgrade
* skill creation
* new capability generation

Interpret these as **governed engineering workflows**, not unrestricted self-modification.

Safe pattern:

```text
need detected
   ↓
proposal
   ↓
implementation
   ↓
sandbox/test
   ↓
review/policy
   ↓
registration
   ↓
evidence
```

Not:

```text
AI wants capability
→ silently rewrites itself
```

---

# 38. CAPABILITY REGISTRATION

Long-term Harness should think in terms of capabilities.

Examples:

```text
reason.general
code.execute
research.general
device.time
message.send
context.compile
context.query
agent.durable
```

A provider/skill/engine advertises capabilities.

Tasks request capabilities.

Routing resolves compatible implementations.

This is stronger than hard-coding every feature by provider name.

---

# 39. DEVICE REALITY

A recent example:

An LLM was asked:

```text
"What time is it?"
```

Remote models do not inherently know the phone's local clock.

The correct architecture is not to make the model guess.

The device/runtime supplies authoritative local reality.

This led to injecting device time context into direct calls.

Long-term this concept generalizes:

```text
MODEL REASONING
       +
DEVICE/LOCAL FACTS
       +
HARNESS GOVERNANCE
```

The model is not automatically the source of truth for device reality.

---

# 40. DEVELOPMENT STYLE

When working on this project:

1. Inspect current architecture before replacing it.
2. Preserve sealed/verified checkpoints.
3. Do not repeatedly redo solved migrations.
4. Prefer minimal compatible changes.
5. Add tests.
6. Distinguish designed/implemented/tested/verified.
7. Keep provider neutrality.
8. Avoid architecture tied to today's model vendors.
9. Preserve future platform portability.
10. Do not let convenience shortcuts become permanent architecture unnoticed.

---

# 41. CURRENT DEVELOPMENT ORGANIZATION

At this stage:

## Phone

Android/Termux Harness is a working real-device target.

Use it for:

* running Harness
* Android validation
* compatibility testing
* acceptance tests

Do not casually turn it back into the primary refactoring workstation.

## PC

Primary development environment.

The canonical project is being worked on from the shared repository.

C/Codex and OpenCode may be used as development agents.

Git/GitHub is the controlled bridge.

---

# 42. IMMEDIATE PROJECT DIRECTION

Before major new development, current PC test/environment issues should be resolved cleanly.

The next major architectural phase after the baseline is clean is:

# PHASE 10 — PERSISTENT CONTEXT SESSIONS

Primary target experience:

```text
harness chat
```

with:

```text
harness session new
harness session list
harness session resume
harness session info
harness session close
```

Exact CLI names are not sacred.

The architectural behavior is.

---

# 43. PHASE 10 MUST ENABLE THE OVERLORD EXAMPLE

Eventually something like:

```text
harness context load overlord
harness chat --session overlord
```

Then:

```text
User:
Where is Ainz at this point?

Harness:
[answers using relevant canonical context]

User:
What was he planning before that?

Harness:
[understands "he" and "that"]

User:
Switch to observer mode.

Harness:
[same session/world/context]

User:
Let me enter this scene.

Harness:
[creates governed branch/interaction rather than corrupting source canon]
```

This is one of the best conceptual tests for the session/context/world architecture.

---

# 44. DO NOT RUSH THE GRAPHICAL LAYER

Graphic Novel / Visual Novel / 2D / 3D should eventually exist.

But they should be built **after or on top of** stable:

```text
session
context
world state
events
identity
provenance
branches
```

Otherwise we create beautiful interfaces for a world with no reliable memory.

---

# 45. THE CORE VISION IN ONE SENTENCE

The project is moving toward:

> **A provider-independent, persistent, governed context operating layer where one continuous identity/world can survive changing models, devices, interfaces and implementation substrates.**

And Kirti is the major identity/application intended to eventually inhabit that layer.

---

# 46. KITERETSU'S ROLE

When receiving this archive:

Do NOT immediately attempt to rewrite everything.

First:

1. Read the current repository.
2. Compare current implementation with this historical intent.
3. Identify what is:

   * already implemented
   * partially implemented
   * planned
   * obsolete historical design
   * conflicting
4. Preserve working architecture unless there is evidence it needs changing.
5. Treat this document as **project intent and memory**, not an assertion that every historical prototype remains correct.
6. When proposing future architecture, explain how it preserves or improves these principles.

---

# 47. NON-NEGOTIABLE PRINCIPLES TO REMEMBER

Remember these even if implementation changes:

**One Kirti.**

**Many doors, one world.**

**Many sessions, one lawful core.**

**Harness is substrate; Kirti is identity/application.**

**Sessions belong to Harness, not a provider.**

**Models reason; Harness governs.**

**Device reality comes from authoritative device/runtime sources, not model guessing.**

**Source canon and generated fiction remain distinguishable.**

**Replay does not rewrite history. Alteration creates a branch.**

**Providers are replaceable.**

**Do not hard-code today's AI ecosystem into tomorrow's architecture.**

**Do not default every subsystem to Python.**

**Portable source, platform-specific runtime.**

**Evidence distinguishes planned, implemented, tested and real-world verified.**

**Do not build separate architectures for chat, novels, Kirti and visual worlds when they can be applications of the same session/context/world substrate.**

---

# 48. ACKNOWLEDGEMENT REQUEST

After reading this context archive, do not immediately code.

Respond first with:

1. your understanding of the relationship between Harness, Kirti and the Inner World;
2. your understanding of "many doors, one world";
3. why Overlord is a Context-as-Program/session testbed rather than a separate chatbot;
4. what Phase 10 is supposed to solve;
5. which parts of the current repository already support this vision;
6. any important architectural contradiction you discover between the current repository and this memory archive.

Then wait for the next development instruction.