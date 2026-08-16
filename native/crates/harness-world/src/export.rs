//! Signed-history export (renderer-neutral) + independent verifier.
//!
//! The export is a plain JSON document any tool can render (novel view,
//! visual novel, observer, character, 2D/3D, timeline) or verify. It
//! carries everything needed to prove the history: genesis/source
//! identity, world/session/branch identity, event ordering, prev/current
//! hashes, provenance, actor, action, state transition, and logical time
//! (branch:seq) plus wall-clock created_at for reference. Verification is
//! a pure function — no database required.

use crate::knowledge::KnowledgeStore;
use crate::manifest::HDoorManifest;
use crate::store::{chain_hash, StoredEvent, WorldStore};
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ExportWorldIdentity {
    pub schema: String,
    pub world_id: String,
    pub title: String,
    pub source_identity: String,
    pub canon_hash: String,
    pub manifest_hash: String,
    pub compiler: String,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ExportSessionIdentity {
    pub instance_id: String,
    pub branch_id: String,
    pub branch_anchor: String,
    pub branch_base: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ExportEvent {
    pub seq: i64,
    pub branch_id: String,
    pub event_type: String,
    pub actor: String,
    pub detail: serde_json::Value,
    pub created_at: String,
    pub logical_time: String,
    pub prev_hash: String,
    pub hash: String,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ExportState {
    pub location: String,
    pub inventory: Vec<String>,
    pub mode: String,
    pub story_position: i64,
    pub interlocutor: Option<String>,
    pub knowledge_entries: usize,
    pub knowledge: KnowledgeStore,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct WorldExport {
    pub schema: String,
    pub export_generated: String,
    pub world: ExportWorldIdentity,
    pub session: ExportSessionIdentity,
    pub events: Vec<ExportEvent>,
    pub state: ExportState,
}

#[derive(Debug)]
pub enum ExportError {
    BadSchema(String),
    EmptyHistory,
    BrokenChain { seq: i64, reason: String },
    AnchorMismatch { expected: String, actual: String },
    EventCountMismatch { expected: usize, actual: usize },
}

impl std::fmt::Display for ExportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ExportError::BadSchema(schema) => write!(f, "unsupported export schema '{}'", schema),
            ExportError::EmptyHistory => write!(f, "export has no events"),
            ExportError::BrokenChain { seq, reason } => {
                write!(f, "chain broken at seq {}: {}", seq, reason)
            }
            ExportError::AnchorMismatch { expected, actual } => write!(
                f,
                "genesis anchor mismatch: expected '{}', export starts at '{}'",
                expected, actual
            ),
            ExportError::EventCountMismatch { expected, actual } => write!(
                f,
                "event count mismatch: expected {}, export has {}",
                expected, actual
            ),
        }
    }
}

impl std::error::Error for ExportError {}

/// Build a renderer-neutral signed export for a session's branch.
pub fn export_branch(
    store: &WorldStore,
    manifest: &HDoorManifest,
    canon_hash: &str,
    manifest_hash: &str,
    instance_id: &str,
    branch_id: &str,
    state: ExportState,
    export_generated: &str,
) -> Result<WorldExport, ExportError> {
    let events = store
        .events(branch_id)
        .map_err(|err| ExportError::BrokenChain {
            seq: 0,
            reason: err.to_string(),
        })?;
    let branch_anchor = store
        .genesis_prev(branch_id)
        .map_err(|err| ExportError::BrokenChain {
            seq: 0,
            reason: err.to_string(),
        })?;
    let base_branch = store
        .base_branch(branch_id)
        .map_err(|err| ExportError::BrokenChain {
            seq: 0,
            reason: err.to_string(),
        })?;
    Ok(WorldExport {
        schema: "hdoor_export_v1".to_string(),
        export_generated: export_generated.to_string(),
        world: ExportWorldIdentity {
            schema: "hdoor_world_v1".to_string(),
            world_id: manifest.world_id.clone(),
            title: manifest.title.clone(),
            source_identity: manifest.source.identity.clone(),
            canon_hash: canon_hash.to_string(),
            manifest_hash: manifest_hash.to_string(),
            compiler: format!(
                "{} {}",
                manifest.compiler.name, manifest.compiler.version
            ),
        },
        session: ExportSessionIdentity {
            instance_id: instance_id.to_string(),
            branch_id: branch_id.to_string(),
            branch_anchor,
            branch_base: base_branch,
        },
        events: events.iter().map(to_export_event).collect(),
        state,
    })
}

fn to_export_event(event: &StoredEvent) -> ExportEvent {
    let detail: serde_json::Value =
        serde_json::from_str(&event.detail_json).unwrap_or(serde_json::Value::Null);
    ExportEvent {
        seq: event.seq,
        branch_id: event.branch_id.clone(),
        event_type: event.event_type.clone(),
        actor: event.actor.clone(),
        detail,
        created_at: event.created_at.clone(),
        logical_time: format!("{}:{}", event.branch_id, event.seq),
        prev_hash: event.prev_hash.clone(),
        hash: event.hash.clone(),
    }
}

/// Independently verify a signed export: schema, genesis anchor, event
/// ordering, chain hashes. Pure function — no database, no store access.
pub fn verify_export(export: &WorldExport) -> Result<usize, ExportError> {
    if export.schema != "hdoor_export_v1" {
        return Err(ExportError::BadSchema(export.schema.clone()));
    }
    if export.events.is_empty() {
        return Err(ExportError::EmptyHistory);
    }
    let mut expected_prev = export.session.branch_anchor.clone();
    for event in &export.events {
        let detail_json = serde_json::to_string(&event.detail).map_err(|err| {
            ExportError::BrokenChain {
                seq: event.seq,
                reason: err.to_string(),
            }
        })?;
        if event.prev_hash != expected_prev {
            return Err(ExportError::BrokenChain {
                seq: event.seq,
                reason: format!(
                    "prev_hash '{}' does not match expected '{}'",
                    event.prev_hash, expected_prev
                ),
            });
        }
        let recomputed = chain_hash(
            &expected_prev,
            event.seq,
            &event.branch_id,
            &event.event_type,
            &event.actor,
            &detail_json,
            &event.created_at,
        );
        if recomputed != event.hash {
            return Err(ExportError::BrokenChain {
                seq: event.seq,
                reason: format!("hash '{}' != recomputed '{}'", event.hash, recomputed),
            });
        }
        expected_prev = event.hash.clone();
    }
    Ok(export.events.len())
}