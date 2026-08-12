# Capability map

This registry-oriented map separates implementation maturity, live health, and
evidence. It is a human-readable projection of the machine contracts introduced
in the first implementation slice.

| Capability ID | Provider(s) | Maturity | Current evidence | Notes |
|---|---|---|---|---|
| `reason.general` | OpenCode, Prime, local | tested/implemented | adapter tests; live OpenCode/Prime probes | local is disabled/down |
| `code.execute` | OpenCode, Prime | tested | mocked adapter tests and live CLI availability | provider result quality not benchmarked |
| `research.general` | OpenCode | implemented | binary/status observation | no benchmark history |
| `agent.durable` | Prime | tested | exact PID plus reachable private socket | provider credits remain independent |
| `agent.recursive` | Prime | implemented | installed feature documentation | not harness-benchmarked |
| `message.send` | Hermes | implemented | binary discovery only | argv-visible; upstream provider must be repaired |
| `reason.private` | local | implemented | loopback-only adapter tests | disabled/down, so not active |
| `job.encrypted` | Harness | tested | queue and crypto tests | active when service/worker runs |
| `failure.circuit_breaker` | Harness | tested | persistent circuit tests | no load benchmark |
| `audit.hash_linked` | Harness | tested | chain and tamper tests | not externally signed |
| `integrity.verify` | Harness | tested | SHA-256 pin tests and live verification | private local manifest |
| `provider.discover.cli` | Harness | tested | bounded probe tests | observation only; not capability validation |
| `context.compile.text` | Harness | tested | compiler/package tests | deterministic compiler |
| `context.execute.query` | Harness | tested | runtime and provenance tests | deterministic, provenance-bearing |
| `context.execute.transform` | Harness | tested | allowlist and failure tests | allowlisted transformations only |
| `context.execute.generate` | Harness | tested | evidence/refusal tests | evidence brief; no unsupported claims |
| `runtime.always_active` | Harness service | tested and active on this device | exact PID, fresh heartbeat, bounded queued compilation | not benchmarked/stable |
| `skill.foundry` | Harness | idea | external systems have skill formats | native lifecycle absent |
| `capability.invent` | Harness | designed | architecture and gate defined | no autonomous promotion yet |
| `context.compose` | Harness | idea | target semantics documented | deferred |
| `context.state.transaction` | Harness | idea | target semantics documented | deferred |
| `context.bytecode` | Harness | idea | research hypothesis only | implement only after experiments |

## Discovery sources

- explicit declarative CLI probe specifications;
- current adapter descriptors;
- configured skill directories and manifest metadata;
- future MCP/plugin schemas and package-manager metadata.

Discovery records presence and interfaces. Validation records demonstrated
behavior. Benchmarking records comparative performance. These are different
events and must not be collapsed into one `available=true` flag.

## Evidence kinds

Machine descriptors use evidence kinds equivalent to:

- `user_provided`
- `local_observation`
- `test_verified`
- `documentation`
- `web_research`
- `model_inference`
- `speculation`

Only controlled validation can promote a discovered capability to tested.
