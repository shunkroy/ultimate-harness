//! Canonical language-neutral object model (v1).
//!
//! These types are the semantic contract of Harness, deliberately defined
//! independently of any implementation language. Persistent representation
//! is versioned JSON envelopes — never Python pickle, never Java or .NET
//! serialization internals, never Rust memory layout.
//!
//! Status semantics follow the project law:
//! DESIGNED / PROTOTYPED / IMPLEMENTED / TESTED / DEVICE-VERIFIED /
//! BLOCKED / PLANNED — a type existing here does not mean a subsystem
//! is implemented.

use serde::{Deserialize, Serialize};

/// Version of the canonical model in this crate.
pub const CANONICAL_SCHEMA: u32 = 1;

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum IdentityKind {
    Harness,
    User,
    Device,
    World,
    Resident,
}

/// A lawful identity anchor. Devices do not own Harness identity; they
/// hold identities of kind `Device` bound to a `Harness` identity.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct HarnessIdentity {
    pub id: String,
    pub name: String,
    pub kind: IdentityKind,
}

// ---------------------------------------------------------------------------
// Sessions (mirrors the canonical `sessions` schema in the state database)
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum SessionState {
    Open,
    Closed,
}

/// A Harness-owned persistent session. Survives provider/model/process/UI
/// changes by design. Fields map 1:1 to the canonical v5 schema.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Session {
    pub id: String,
    pub title: String,
    pub state: SessionState,
    pub created_at: f64,
    pub updated_at: f64,
    pub metadata_json: String,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum TurnRole {
    User,
    Assistant,
    System,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum TurnStatus {
    Completed,
    Failed,
    Running,
}

/// One turn inside a session. Provider-fluid: the engine/provider/model
/// are recorded facts, not ownership.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Turn {
    pub seq: i64,
    pub session_id: String,
    pub role: TurnRole,
    pub status: TurnStatus,
    pub text: String,
    pub engine: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub provider_session_id: Option<String>,
    pub run_id: Option<String>,
    pub sensitive: bool,
    pub untrusted: bool,
    pub error_code: Option<String>,
    pub duration_ms: Option<f64>,
    pub created_at: f64,
}

// ---------------------------------------------------------------------------
// Provenance / events
// ---------------------------------------------------------------------------

/// Provenance labels: distinguish canon fact, branch event, dream state,
/// simulation state, hypothesis and replay. Dream is not Canon; Branch is
/// not Canon; this is preserved at the type level.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Provenance {
    pub source: Option<String>,
    pub evidence: Option<String>,
    /// sha256 signature for append-only signed ledgers (world pattern).
    pub signature: Option<String>,
}

/// Append-only event record, aligned with the signed world-ledger pattern
/// `{event, detail, ts, iso, sig}`.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub seq: i64,
    pub event_type: String,
    pub detail_json: String,
    pub ts: f64,
    pub iso: Option<String>,
    pub sig: Option<String>,
    pub provenance: Option<Provenance>,
}

// ---------------------------------------------------------------------------
// World / canon / branch layers
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum WorldKind {
    Fiction,
    SciFi,
    Modern,
    Historical,
    Mecha,
    Realistic,
    Mixed,
    Unforeseen,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct World {
    pub world_id: String,
    pub title: String,
    pub kind: WorldKind,
    pub canon_source: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum WorldMode {
    Watcher,
    Traveller,
    LiveInside,
    Character,
    Observer,
    Isekai,
    RealLifeSimulator,
    Replay,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct WorldInstance {
    pub instance_id: String,
    pub world_id: String,
    pub timeline_id: String,
    pub mode: WorldMode,
}

/// What layer a timeline/branch belongs to. Canon is immutable
/// source-derived history; everything else is labelled and separable.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum BranchSource {
    Canon,
    Branch,
    Dream,
    Simulation,
    Hypothesis,
    Replay,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Branch {
    pub branch_id: String,
    pub world_id: String,
    pub instance_id: String,
    pub timeline_id: String,
    pub parent_branch_id: Option<String>,
    pub created_at: f64,
    pub source: BranchSource,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Timeline {
    pub timeline_id: String,
    pub world_id: String,
    pub branch_ids: Vec<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Resident {
    pub resident_id: String,
    pub world_id: String,
    pub name: String,
    pub role: Option<String>,
    pub note: Option<String>,
}

// ---------------------------------------------------------------------------
// Capabilities / engines / runs
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CapabilityStatus {
    Designed,
    Prototyped,
    Implemented,
    Tested,
    DeviceVerified,
    Blocked,
    Planned,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Capability {
    pub name: String,
    pub status: CapabilityStatus,
    pub detail: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EngineKind {
    Deterministic,
    Rule,
    StateMachine,
    Retrieval,
    Dialogue,
    Story,
    World,
    Simulation,
    Dream,
    Procedural,
    LocalModel,
    RemoteAi,
    Hybrid,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EngineStatus {
    pub name: String,
    pub kind: EngineKind,
    pub available: bool,
    pub detail: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RunRequest {
    pub prompt: String,
    pub engine: Option<String>,
    pub model: Option<String>,
    pub timeout_secs: Option<u64>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RunOutcome {
    pub success: bool,
    pub text: Option<String>,
    pub error: Option<String>,
    pub error_code: Option<String>,
    pub engine: String,
    pub duration_ms: f64,
    pub metadata: serde_json::Value,
}
// ---------------------------------------------------------------------------
// Provenance tags (Milestone 2): the layer a fact/event belongs to.
// Dream is not Canon. Branch is not Canon. Simulation is not Canon.
// These tags ride on every compiled fact and every runtime event.
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProvenanceTag {
    Canon,
    Inferred,
    Simulated,
    UserCreated,
    BranchDiverged,
    Hypothetical,
    Dream,
    Replay,
}
