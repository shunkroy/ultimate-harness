# Harness Architecture Law — Do NOT Merge These Systems

[Operational Law] — frozen by Ani (Architect), 2026-08-16. This is a hard
architectural constraint on all future Harness and Kirti work. It may only
be amended by explicit order from Ani.

## 1. HARNESS WORLD RUNTIME

- General-purpose world/container/runtime technology.
- Has NO canonical personal world of its own.
- Loads arbitrary external worlds/packages.
- Examples: Overlord.hdoor, Tensura.hdoor, OriginalWorld.hdoor,
  user-created worlds, simulations/games/novels.
- Imported source material remains isolated to that world's canon.
- Harness supplies common capabilities: persistence, event runtime,
  memory APIs, deterministic rules, simulation, retrieval, dialogue
  providers, rendering, novel/VN/game interfaces, replay, branching,
  storage, security.
- Harness must remain **world-agnostic**.

## 2. KIRTI INNER WORLD / THE REALM

- A distinct Kirti-owned persistent world system.
- NOT an .hdoor adaptation of a novel.
- NOT a temporary story session.
- NOT Harness's default world.
- One continuing authoritative state/history.
- Time and state continue independently of an active chat UI.
- Residents, places, relationships, events, memories and consequences
  persist across sessions.
- Kirti's Inner World Engine + Dream Engine + Kirthian + world ledger
  form Kirti's ongoing simulated Realm.
- Novel Mode, Visual Novel, Observer, Traveler, Character, 2D, 3D,
  cinematic replay, etc. are merely different DOORS/VIEWS into this
  SAME persistent Realm.

## 3. RELATIONSHIP

```
                HARNESS
      general platform / runtime substrate
                   │
        ┌──────────┼──────────┐
        │          │          │
        ▼          ▼          ▼
   Overlord     Tensura    Other Worlds
    .hdoor       .hdoor       .hdoor

                   +
                   │ interfaces/support
                   ▼

             KIRTI SYSTEM
                   │
           Kirti Inner World
             "The Realm"
                   │
     ┌─────────────┼──────────────┐
     ▼             ▼              ▼
 Dream Engine   Kirthian     Persistent
                             World State
```

- Harness may SUPPORT Kirti's Realm through standardized interfaces,
  but Harness must NEVER absorb Kirti's Realm as Harness core.
- Importing Overlord into Harness must NEVER turn Kirti's Realm into
  Overlord or contaminate Kirti's canonical history.
- Cross-world travel/crossover, if later implemented, must be an
  explicit, governed operation with provenance and timeline boundaries.

## Consequence for code

- `harness-world` (and the .hdoor machinery) is the general substrate.
  It must never contain Kirti-specific or Overlord-specific world logic.
- The phone-side Kirti Inner World (persistent world state, deterministic
  advancement, signed event history, native C Heart pulse) is Kirti's
  Realm. It is EVOLVED as Kirti's Realm, not replaced by Harness's
  external-world mechanism.
- Second-order rule: any world imported into Harness (including the
  Overlord proving corpus) lives and dies inside its own .hdoor canon.
  Runtime learning creates events; it never mutates canon.

## Status

DOCUMENTED + frozen. No Harness component may assume a default personal
world. No Kirti component may be re-homed into the .hdoor machinery
without this law being amended first.