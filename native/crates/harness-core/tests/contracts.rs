//! Cross-module contract tests for the native foundation.

use harness_core::engine::DeterministicArithmeticEngine;
use harness_core::schema::envelope;
use harness_core::types::*;
use harness_core::{Engine, Envelope};

#[test]
fn canonical_model_round_trips_all_core_types() {
    let world = World {
        world_id: "overlord-v1".into(),
        title: "Overlord Volume 1".into(),
        kind: WorldKind::Fiction,
        canon_source: Some("provenance:lawful-source-copy".into()),
    };
    let instance = WorldInstance {
        instance_id: "inst-1".into(),
        world_id: world.world_id.clone(),
        timeline_id: "tl-1".into(),
        mode: WorldMode::Traveller,
    };
    let branch = Branch {
        branch_id: "br-1".into(),
        world_id: world.world_id.clone(),
        instance_id: instance.instance_id.clone(),
        timeline_id: instance.timeline_id.clone(),
        parent_branch_id: None,
        created_at: 1.0,
        source: BranchSource::Branch,
    };
    let dream_branch = Branch {
        branch_id: "br-dream".into(),
        world_id: world.world_id.clone(),
        instance_id: instance.instance_id.clone(),
        timeline_id: "tl-2".into(),
        parent_branch_id: Some(branch.branch_id.clone()),
        created_at: 2.0,
        source: BranchSource::Dream,
    };
    // Dream is not Canon; Branch is not Canon — enforced at the type level.
    assert_ne!(branch.source, BranchSource::Canon);
    assert_ne!(dream_branch.source, BranchSource::Canon);

    let envelope = envelope("branch", &branch);
    let encoded = serde_json::to_string(&envelope).unwrap();
    let decoded: Envelope<Branch> = serde_json::from_str(&encoded).unwrap();
    assert_eq!(decoded.payload, branch);
    assert_eq!(decoded.schema, 1);
}

#[test]
fn event_carries_signed_ledger_fields() {
    let event = Event {
        seq: 1,
        event_type: "world_genesis".into(),
        detail_json: "{\"world\":\"Realm\"}".into(),
        ts: 1785500223.5,
        iso: Some("2026-07-31T12:17:03+0000".into()),
        sig: Some("027e7a133b904171dcc6b6077afc7e6127cd85189f51644f4bf42083fc45fd2e".into()),
        provenance: Some(Provenance {
            source: Some("registry".into()),
            evidence: Some("registry_sha256".into()),
            signature: None,
        }),
    };
    let encoded = serde_json::to_string(&event).unwrap();
    let decoded: Event = serde_json::from_str(&encoded).unwrap();
    assert_eq!(decoded.sig.as_deref().map(|s| s.len()), Some(64));
    assert_eq!(decoded, event);
}

#[test]
fn capability_statuses_are_explicit() {
    let implemented = Capability {
        name: "status.read".into(),
        status: CapabilityStatus::DeviceVerified,
        detail: Some("harness-native status on aarch64 PRoot".into()),
    };
    let designed = Capability {
        name: "world.compile".into(),
        status: CapabilityStatus::Designed,
        detail: None,
    };
    assert!(matches!(implemented.status, CapabilityStatus::DeviceVerified));
    assert!(matches!(designed.status, CapabilityStatus::Designed));
}

#[test]
fn deterministic_engine_satisfies_engine_contract() {
    let engine = DeterministicArithmeticEngine::default();
    let status = engine.status();
    assert!(status.available);
    assert_eq!(status.kind, EngineKind::Deterministic);
    let outcome = engine.run(&RunRequest {
        prompt: "what is 6 times 7?".into(),
        engine: None,
        model: None,
        timeout_secs: None,
    });
    assert!(outcome.success);
    assert_eq!(outcome.text.as_deref(), Some("42"));
    assert_eq!(outcome.engine, "deterministic-arithmetic");
}

#[test]
fn turn_model_is_provider_fluid() {
    let turn = Turn {
        seq: 1,
        session_id: "s-1".into(),
        role: TurnRole::User,
        status: TurnStatus::Completed,
        text: "hello".into(),
        engine: Some("direct".into()),
        provider: Some("google".into()),
        model: Some("gemini-3.5-flash-lite".into()),
        provider_session_id: None,
        run_id: Some("run-1".into()),
        sensitive: false,
        untrusted: false,
        error_code: None,
        duration_ms: Some(6300.0),
        created_at: 1786862671.0,
    };
    // Provider identity is a recorded fact, not ownership: changing it
    // does not change the turn's identity.
    let mut changed = turn.clone();
    changed.provider = Some("groq".into());
    changed.model = Some("llama-3.3-70b".into());
    assert_eq!(changed.seq, turn.seq);
    assert_eq!(changed.session_id, turn.session_id);
}