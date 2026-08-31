# Phone Runtime Identity, Adoption, and Cross-Environment Supervision

Status: architecture decision + research record
Date: 2026-08-31

## Decision

Kirti must identify a running component by stable logical identity and purpose, not by PID alone.

A PID is only the current OS execution handle for a component incarnation. The stable hierarchy is:

1. `entity_id` — Kirti
2. `embodiment_id` — e.g. `phone-primary`
3. `purpose_id` / capability — e.g. `communication.telegram`
4. `implementation_id` — e.g. `termux.telegram.v3` or `apk.telegram.native.v1`
5. `instance_id` — UUID for this implementation instance
6. process incarnation — kernel boot ID + PID + process start time
7. generation / lease — authority for singleton roles

The Android APK should adopt already-running verified Termux components instead of spawning duplicates. Termux remains authoritative for lifecycle management of Termux processes; the APK owns Android-native components; a Kirti reconciler owns desired logical state and cross-environment identity.

## Why PID alone is insufficient

PIDs can be reused. A stored PID can later refer to a different process. For a stronger process-incarnation key use:

- Linux kernel boot ID
- PID
- `/proc/<pid>/stat` field 22 (`starttime`)
- where supported, a `pidfd` held by the Termux-side supervisor

The existing Harness process checks (exact cmdline verification, cwd/path checks, live socket probe, private pidfile handling) should be retained and strengthened with these incarnation fields rather than replaced.

## APK ↔ Termux boundary

Android applications are sandboxed under separate Linux UIDs. The APK should not rely on scanning or controlling Termux's process table directly.

Bootstrap/control should use Termux's supported `RUN_COMMAND` integration:

- APK requests `com.termux.permission.RUN_COMMAND`
- Termux has `allow-external-apps=true`
- APK invokes a narrow Kirti runtime command/bridge
- results return through the supported PendingIntent/result mechanism

A single Termux-side Kirti Runtime Bridge should expose a verified runtime snapshot to the APK and supervise Termux-owned workers.

## Recommended state model

### Stable component identity

```json
{
  "entity_id": "kirti",
  "embodiment_id": "phone-primary",
  "purpose_id": "communication.telegram",
  "implementation_id": "termux.telegram.v3",
  "cardinality": "singleton"
}
```

### Runtime presence

```json
{
  "runtime_id": "phone.termux.telegram",
  "instance_id": "<uuid>",
  "kernel_boot_id": "<linux-boot-id>",
  "pid": 18273,
  "start_ticks": 48592217,
  "generation": 82,
  "lease_holder": "<instance-id>",
  "observed_state": "healthy",
  "heartbeat_seq": 531,
  "last_seen": 1788180301
}
```

## Adoption/reconciliation flow

1. APK starts and loads durable Kirti identity.
2. APK asks the Termux Kirti Runtime Bridge for a signed/authenticated runtime snapshot.
3. Bridge verifies its own workers using process-incarnation data, exact executable/cmdline/cwd expectations, health endpoints, heartbeat freshness, and lease state.
4. APK/Kirti reconciler compares desired vs observed state.
5. Existing verified components are **adopted**.
6. Missing required components are started by the environment that owns them.
7. Duplicate singleton instances are flagged and resolved using generation/lease rules, never by fuzzy PID matching.
8. Foreign or unverifiable processes are left alone or quarantined from Kirti ownership.

Suggested lifecycle states:

`UNKNOWN -> DISCOVERED -> CLAIMED -> VERIFIED -> ADOPTED -> HEALTHY`

with side paths to `DEGRADED`, `QUARANTINED`, `REVOKED`, `LOST`, and `REPLACED`.

## Existing Harness alignment

The current Harness already contains useful foundations:

- `harness2/supervisor.py` performs exact `/proc/<pid>/cmdline` verification, private run-dir validation, safe atomic pidfiles, live Unix-socket probes, stale cleanup, and verified termination.
- `harness2/service.py` maintains a heartbeat and refuses to call the service active unless the process is verified and the heartbeat is fresh.
- `harness2/kernel/registry.py` already provides runtime and capability registries with duplicate/conflict handling.
- `RuntimeDescriptor` already carries stable runtime ID, kind, location, interface, capabilities, health, evidence, and limitations.

Required evolution:

- rename current random `boot_id` to `service_instance_id` or `runtime_epoch`
- additionally store the real Linux kernel boot ID
- add process `starttime`
- add `ProcessIncarnation`
- add stable `ComponentDescriptor` / purpose identity
- add runtime presence as a separate layer from runtime description
- add singleton cardinality, generation, and leases
- implement explicit adoption/reconciliation

## Prior art and evidence that this pattern has been tested

### 1. Erlang/OTP supervisors

Erlang supervisors separate a mandatory stable child-specification `id` from the runtime child PID. A child can be restarted and receive a new PID while the supervisor still addresses the logical child by its specification ID. This is a direct precedent for `purpose/component ID != PID`.

Source: https://www.erlang.org/doc/apps/stdlib/supervisor.html

### 2. Akka actor identity and incarnation

Akka explicitly distinguishes a logical actor path from an actor incarnation. The same logical path can later be occupied by a new actor, but an old actor reference must not be confused with the new incarnation. Actor references include incarnation identity, while paths can be used when logical identity is desired.

This closely matches the proposed Kirti distinction between stable purpose/path and process incarnation.

Source: https://doc.akka.io/libraries/akka-core/current/general/addressing.html

### 3. Kubernetes reconciliation controllers

Kubernetes controllers continuously compare desired state with observed/current state and act to move the system toward the desired state. This validates the proposed Kirti reconciler instead of imperative "start everything on APK launch" behavior.

Source: https://kubernetes.io/docs/concepts/architecture/controller/

### 4. Kubernetes StatefulSet stable identity

StatefulSets maintain a sticky identity for workload members even though the underlying Pod can fail and be replaced/rescheduled. Kubernetes explicitly separates persistent logical identity from the disposable execution instance.

Source: https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/

### 5. Kubernetes duplicate-identity warning

Kubernetes documents a concrete failure mode where force-deleting a StatefulSet Pod can free the identity before the old Pod is actually gone, allowing a replacement with the same identity to exist concurrently. That violates at-most-one semantics.

This is direct evidence for Kirti's need for verified termination plus leases/generations before a singleton role is reassigned.

Source: https://kubernetes.io/docs/tasks/run-application/force-delete-stateful-set-pod/

### 6. systemd service supervision

Modern systemd documentation recommends avoiding PID files when possible and using service-manager-owned process knowledge/readiness notification. It warns that guessing the main PID can be incorrect, especially for multi-process daemons, and can break failure detection/restarts.

This reinforces the rule that PID files are evidence, not identity.

Source: https://www.man7.org/linux/man-pages/man5/systemd.service.5.html

### 7. Linux pidfd

Linux `pidfd_open()` provides a file descriptor referring to a specific process and avoids several races associated with PID reuse. It can be polled for process exit and is a stronger Termux-side supervision primitive where available.

Source: https://man7.org/linux/man-pages/man2/pidfd_open.2.html

### 8. Linux process start time

`/proc/<pid>/stat` field 22 records the process start time after boot. Combining this with kernel boot ID and PID provides a much stronger process-incarnation key than PID alone.

Source: https://www.man7.org/linux/man-pages/man5/proc_pid_stat.5.html

### 9. HashiCorp Nomad

Nomad keeps stable allocation/job identity while recording task restarts and reschedules separately, including previous allocation IDs and restart events. This is another production precedent for logical workload identity above process/task incarnations.

Sources:
- https://developer.hashicorp.com/nomad/api-docs/allocations
- https://developer.hashicorp.com/nomad/commands/job/restart

### 10. Termux ↔ third-party Android app integration

Termux officially supports third-party apps invoking commands in Termux through `RunCommandService`, and Termux:Tasker has used this model in real Android deployments for years. This validates the APK↔Termux bootstrap/control boundary itself, though Kirti's higher-level identity/adoption protocol is custom.

Sources:
- https://github.com/termux/termux-app/wiki/RUN_COMMAND-Intent
- https://github.com/termux/termux-tasker/blob/master/README.md

## What is novel vs already proven

The following pieces are not novel individually:

- stable logical IDs above PIDs
- supervisor-managed restarts
- process incarnation tracking
- readiness/heartbeat checks
- reconciliation of desired vs observed state
- persistent workload identity across replacement
- singleton/at-most-one protection
- APK-to-Termux command execution

The Kirti-specific synthesis is unusual:

- treating Android APK, Termux, PC, and sandbox runtimes as embodiments of one persistent named entity
- mapping implementations to semantic `purpose_id`s such as `communication.telegram`
- explicit adoption of already-running components as "parts of Kirti"
- continuity/event-ledger semantics across embodiment transitions

Therefore this architecture is not an untested fantasy. It is a composition of established supervision, actor-incarnation, orchestration, Linux-process, and Android/Termux patterns, with Kirti-specific identity semantics layered on top.

## Architectural rule

> A PID is where a Kirti component is executing right now; it is never what that component is.
