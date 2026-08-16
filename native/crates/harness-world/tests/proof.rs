//! Milestone 2 vertical-slice proof — integration tests over the public
//! API, driven by the committed legal fixture (original text, not the
//! copyrighted proving corpus).
//!
//! Proves the 14 contract points and the negative cases:
//!  1. .hdoor V1 real package
//!  2. deterministic compiler
//!  3. explicit provenance tags
//!  4. source identity binding
//!  5. fail-closed package validation
//!  6. offline NL pipeline (no cloud, no model)
//!  7. normalization + typo tolerance
//!  8. ambiguity fail-closed
//!  9. world runtime actions
//! 10. signed chained events
//! 11. tamper detection
//! 12. branch isolation
//! 13. canon immutability
//! 14. restart/resume identical state

use harness_world::compiler::Compiler;
use harness_world::package::{read_package, validate_package, write_package, PackageError};
use harness_world::pipeline::{Intent, Pipeline};
use harness_world::runtime::{WorldSession, WorldSessionError};
use harness_world::store::WorldStore;
use harness_world::text::normalize;
use harness_core::types::ProvenanceTag;
use std::path::{Path, PathBuf};

const FIXTURE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/testdata/hollow-keep.txt");

fn fixture_text() -> String {
    std::fs::read_to_string(FIXTURE).unwrap()
}

fn temp_base(tag: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!("hdoor-proof-{}-{}", tag, std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    base
}

fn build_package(base: &Path, world_id: &str, text: &str) -> PathBuf {
    build_package_at(base, world_id, world_id, text)
}

fn build_package_at(base: &Path, dir_tag: &str, world_id: &str, text: &str) -> PathBuf {
    let package_dir = base.join(format!("package-{}", dir_tag));
    let world = Compiler::new().compile(world_id, "The Hollow Keep", "hollow-keep.txt", text);
    write_package(&package_dir, &world).unwrap();
    package_dir
}

// ---------------------------------------------------------------------------
// 1. .hdoor V1 real package
// ---------------------------------------------------------------------------
#[test]
fn proof_01_hdoor_v1_package_is_real() {
    let base = temp_base("p01");
    let package_dir = build_package(&base, "p1", &fixture_text());
    let opened = read_package(&package_dir).unwrap();
    assert_eq!(opened.manifest.schema_version, 1);
    assert_eq!(opened.manifest.package_kind, "hdoor");
    assert_eq!(opened.manifest.world_id, "p1");
    assert!(!opened.manifest.title.is_empty());
    assert!(!opened.manifest.compiler.name.is_empty());
    assert!(!opened.manifest.capabilities.is_empty());
    assert!(opened.manifest.branches.immutable_canon);
    // package layout is real files
    assert!(package_dir.join("manifest.json").exists());
    assert!(package_dir.join("canon/source.txt").exists());
    assert!(package_dir.join("canon/entities.json").exists());
    assert!(package_dir.join("canon/locations.json").exists());
    assert!(package_dir.join("canon/timeline.json").exists());
    assert!(package_dir.join("canon/facts.json").exists());
    assert!(package_dir.join("index.json").exists());
    assert!(package_dir.join("seed_state.json").exists());
    let _ = std::fs::remove_dir_all(&base);
}

// ---------------------------------------------------------------------------
// 2. deterministic compiler
// ---------------------------------------------------------------------------
#[test]
fn proof_02_compiler_is_deterministic() {
    let text = fixture_text();
    let compiler = Compiler::new();
    let first = compiler.compile("p2", "The Hollow Keep", "hollow-keep.txt", &text);
    let second = compiler.compile("p2", "The Hollow Keep", "hollow-keep.txt", &text);
    assert_eq!(
        serde_json::to_string(&first).unwrap(),
        serde_json::to_string(&second).unwrap()
    );
}

// ---------------------------------------------------------------------------
// 3. explicit provenance tags
// ---------------------------------------------------------------------------
#[test]
fn proof_03_provenance_is_explicit() {
    let world = Compiler::new().compile("p3", "The Hollow Keep", "hollow-keep.txt", &fixture_text());
    assert!(!world.entities.is_empty());
    for entity in &world.entities {
        assert_eq!(entity.tag, ProvenanceTag::Canon);
        assert!(!entity.source_ref.is_empty());
        assert_eq!(entity.confidence, 1.0);
    }
    assert!(!world.facts.is_empty());
    for fact in &world.facts {
        assert_eq!(fact.tag, ProvenanceTag::Inferred);
        assert!(!fact.source_ref.is_empty());
    }
    for location in &world.locations {
        assert_eq!(location.tag, ProvenanceTag::Inferred);
    }
}

// ---------------------------------------------------------------------------
// 4. source identity binding
// ---------------------------------------------------------------------------
#[test]
fn proof_04_source_identity_is_bound() {
    let text = fixture_text();
    let world = Compiler::new().compile("p4", "The Hollow Keep", "hollow-keep.txt", &text);
    let expected = format!("sha256:{:x}", {
        use sha2::{Digest, Sha256};
        let mut hasher = Sha256::new();
        hasher.update(text.as_bytes());
        hasher.finalize()
    });
    assert_eq!(world.manifest.source.identity, expected);
    assert_eq!(world.manifest.source.files[0].name, "hollow-keep.txt");
    assert_eq!(world.manifest.source.files[0].bytes, text.len() as u64);
}

// ---------------------------------------------------------------------------
// 5. fail-closed package validation
// ---------------------------------------------------------------------------
#[test]
fn proof_05_validation_fails_closed() {
    let base = temp_base("p05");
    let package_dir = build_package(&base, "p5", &fixture_text());

    // tampered source
    std::fs::write(package_dir.join("canon/source.txt"), "tampered!").unwrap();
    assert!(matches!(
        validate_package(&package_dir),
        Err(PackageError::SourceHashMismatch { .. })
    ));
    let _ = std::fs::remove_dir_all(&base);

    // missing manifest
    let base2 = temp_base("p05b");
    std::fs::create_dir_all(base2.join("canon")).unwrap();
    assert!(matches!(
        validate_package(&base2),
        Err(PackageError::MissingManifest(_))
    ));
    let _ = std::fs::remove_dir_all(&base2);

    // wrong schema version
    let base3 = temp_base("p05c");
    let package_dir3 = build_package(&base3, "p5c", &fixture_text());
    let manifest_path = package_dir3.join("manifest.json");
    let mut manifest: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&manifest_path).unwrap()).unwrap();
    manifest["schema_version"] = serde_json::json!(99);
    std::fs::write(&manifest_path, serde_json::to_string(&manifest).unwrap()).unwrap();
    assert!(matches!(
        validate_package(&package_dir3),
        Err(PackageError::WrongSchemaVersion(99))
    ));
    let _ = std::fs::remove_dir_all(&base3);

    // corrupt entry
    let base4 = temp_base("p05d");
    let package_dir4 = build_package(&base4, "p5d", &fixture_text());
    std::fs::write(package_dir4.join("index.json"), "{not json").unwrap();
    assert!(matches!(
        validate_package(&package_dir4),
        Err(PackageError::CorruptEntry(_))
    ));
    let _ = std::fs::remove_dir_all(&base4);
}

// ---------------------------------------------------------------------------
// 6. offline NL pipeline
// ---------------------------------------------------------------------------
#[test]
fn proof_06_offline_nl_pipeline() {
    let world = Compiler::new().compile("p6", "The Hollow Keep", "hollow-keep.txt", &fixture_text());
    let index: &'static _ = Box::leak(Box::new(world.index));
    let pipeline = Pipeline::new(index);

    let take = pipeline.parse("take the Silver Key").unwrap();
    assert_eq!(take.intent, Intent::Take);
    assert!(take.targets.iter().any(|t| t.id == "silver-key"));

    let go = pipeline.parse("go to the Deep Well").unwrap();
    assert_eq!(go.intent, Intent::Go);
    assert!(go.targets.iter().any(|t| t.id == "deep-well"));

    let inspect = pipeline.parse("inspect the Lantern of Dawn").unwrap();
    assert_eq!(inspect.intent, Intent::Inspect);

    let status = pipeline.parse("status").unwrap();
    assert_eq!(status.intent, Intent::Status);
}

// ---------------------------------------------------------------------------
// 7. normalization + typo tolerance
// ---------------------------------------------------------------------------
#[test]
fn proof_07_normalization_and_typo_tolerance() {
    let world = Compiler::new().compile("p7", "The Hollow Keep", "hollow-keep.txt", &fixture_text());
    let index: &'static _ = Box::leak(Box::new(world.index));
    let pipeline = Pipeline::new(index);

    // case + punctuation
    let loud = pipeline.parse("TAKE THE SILVER KEY!").unwrap();
    assert!(loud.targets.iter().any(|t| t.id == "silver-key"));

    // typo (edit distance 1)
    let typo = pipeline.parse("take the bronz key").unwrap();
    assert!(typo.targets.iter().any(|t| t.id == "bronze-key"));

    // normalization is deterministic
    assert_eq!(normalize("Café ﬁne!"), "cafe fine");
}

// ---------------------------------------------------------------------------
// 8. ambiguity fail-closed
// ---------------------------------------------------------------------------
#[test]
fn proof_08_ambiguity_fails_closed() {
    let world = Compiler::new().compile("p8", "The Hollow Keep", "hollow-keep.txt", &fixture_text());
    let index: &'static _ = Box::leak(Box::new(world.index));
    let pipeline = Pipeline::new(index);

    // "key" is ambiguous between silver and bronze
    let result = pipeline.parse("take the key");
    assert!(matches!(
        result,
        Err(harness_world::pipeline::ParseError::AmbiguousTarget(_))
    ));

    // unknown intent
    let result = pipeline.parse("banana the moon");
    assert!(matches!(
        result,
        Err(harness_world::pipeline::ParseError::UnknownIntent(_))
    ));

    // empty
    assert!(matches!(
        pipeline.parse("   "),
        Err(harness_world::pipeline::ParseError::Empty)
    ));
}

// ---------------------------------------------------------------------------
// 9. world runtime actions
// ---------------------------------------------------------------------------
#[test]
fn proof_09_runtime_actions() {
    let base = temp_base("p09");
    let package_dir = build_package(&base, "p9", &fixture_text());
    let state_root = base.join("state");
    let mut session =
        WorldSession::open(&package_dir, &state_root, "p9", "i1", "main").unwrap();

    let status = session.act("status").unwrap();
    assert!(status.text.contains("Hollow Keep"), "{}", status.text);

    let moved = session.act("go to the Hall of Embers").unwrap();
    assert_eq!(moved.event.unwrap().event_type, "move");

    let taken = session.act("take the Silver Key").unwrap();
    assert_eq!(taken.event.unwrap().event_type, "take");

    let dropped = session.act("drop the Silver Key").unwrap();
    assert_eq!(dropped.event.unwrap().event_type, "drop");

    // cannot take what is not here
    let result = session.act("take the Bronze Key");
    assert!(matches!(result, Err(WorldSessionError::NotHere(_))));

    session.close().unwrap();
    let _ = std::fs::remove_dir_all(&base);
}

// ---------------------------------------------------------------------------
// 10. signed chained events
// ---------------------------------------------------------------------------
#[test]
fn proof_10_events_are_signed_and_chained() {
    let base = temp_base("p10");
    let package_dir = build_package(&base, "p10", &fixture_text());
    let state_root = base.join("state");
    {
        let mut session =
            WorldSession::open(&package_dir, &state_root, "p10", "i1", "main").unwrap();
        session.act("status").unwrap();
        session.act("go to the Hall of Embers").unwrap();
        session.act("take the Silver Key").unwrap();
        session.close().unwrap();
    }
    let store = WorldStore::open(&state_root, "p10", "i1").unwrap();
    let events = store.events("main").unwrap();
    assert_eq!(events.len(), 3);
    // chain verifies end to end
    assert_eq!(store.verify_chain("main").unwrap(), 3);
    // hashes are distinct and chained
    assert_ne!(events[0].hash, events[1].hash);
    assert_eq!(events[1].prev_hash, events[0].hash);
    assert_eq!(events[2].prev_hash, events[1].hash);
    let _ = std::fs::remove_dir_all(&base);
}

// ---------------------------------------------------------------------------
// 11. tamper detection
// ---------------------------------------------------------------------------
#[test]
fn proof_11_tamper_is_detected() {
    let base = temp_base("p11");
    let package_dir = build_package(&base, "p11", &fixture_text());
    let state_root = base.join("state");
    {
        let mut session =
            WorldSession::open(&package_dir, &state_root, "p11", "i1", "main").unwrap();
        session.act("go to the Hall of Embers").unwrap();
        session.act("take the Silver Key").unwrap();
        session.close().unwrap();
    }
    // tamper directly with the ledger
    let db = state_root.join("worlds/p11/i1/world.db");
    {
        let conn = rusqlite::Connection::open(&db).unwrap();
        conn.execute(
            "UPDATE events SET detail_json = '{\"target\":\"tampered\"}' WHERE seq = 1",
            [],
        )
        .unwrap();
    }
    let store = WorldStore::open(&state_root, "p11", "i1").unwrap();
    assert!(matches!(
        store.verify_chain("main"),
        Err(harness_world::store::WorldStoreError::ChainBroken { seq: 1, .. })
    ));
    let _ = std::fs::remove_dir_all(&base);
}

// ---------------------------------------------------------------------------
// 12. branch isolation
// ---------------------------------------------------------------------------
#[test]
fn proof_12_branches_are_isolated() {
    let base = temp_base("p12");
    let package_dir = build_package(&base, "p12", &fixture_text());
    let state_root = base.join("state");
    {
        let mut session =
            WorldSession::open(&package_dir, &state_root, "p12", "i1", "main").unwrap();
        session.act("go to the Hall of Embers").unwrap();
        session.act("take the Silver Key").unwrap();
        session.close().unwrap();
    }
    {
        // alt branch starts from canon seed — silver key is back at the hall
        let mut session =
            WorldSession::open(&package_dir, &state_root, "p12", "i1", "alt").unwrap();
        let taken = session.act("go to the Hall of Embers").unwrap();
        assert_eq!(taken.event.unwrap().event_type, "move");
        let taken = session.act("take the Silver Key").unwrap();
        assert_eq!(taken.event.unwrap().event_type, "take");
        session.close().unwrap();
    }
    let store = WorldStore::open(&state_root, "p12", "i1").unwrap();
    // both branches verify independently against the same canon
    assert_eq!(store.verify_chain("main").unwrap(), 2);
    assert_eq!(store.verify_chain("alt").unwrap(), 2);
    // kv state is branch-scoped: main has the key, alt also has it now —
    // but each branch's location history is independent
    let main_events = store.events("main").unwrap();
    let alt_events = store.events("alt").unwrap();
    assert_ne!(main_events[0].hash, alt_events[0].hash);
    let _ = std::fs::remove_dir_all(&base);
}

// ---------------------------------------------------------------------------
// 13. canon immutability
// ---------------------------------------------------------------------------
#[test]
fn proof_13_canon_is_immutable() {
    let base = temp_base("p13");
    let package_dir = build_package(&base, "p13", &fixture_text());
    let state_root = base.join("state");
    let canon_before = std::fs::read(package_dir.join("canon/source.txt")).unwrap();
    let entities_before = std::fs::read(package_dir.join("canon/entities.json")).unwrap();

    let mut session =
        WorldSession::open(&package_dir, &state_root, "p13", "i1", "main").unwrap();
    session.act("go to the Hall of Embers").unwrap();
    session.act("take the Silver Key").unwrap();
    session.act("go to the Deep Well").unwrap();
    session.close().unwrap();

    // canon files untouched; runtime state lives only in the store
    let canon_after = std::fs::read(package_dir.join("canon/source.txt")).unwrap();
    let entities_after = std::fs::read(package_dir.join("canon/entities.json")).unwrap();
    assert_eq!(canon_before, canon_after);
    assert_eq!(entities_before, entities_after);
    let _ = std::fs::remove_dir_all(&base);
}

// ---------------------------------------------------------------------------
// 14. restart/resume identical state
// ---------------------------------------------------------------------------
#[test]
fn proof_14_restart_resume_is_identical() {
    let base = temp_base("p14");
    let package_dir = build_package(&base, "p14", &fixture_text());
    let state_root = base.join("state");
    let mut before: Option<String> = None;
    {
        let mut session =
            WorldSession::open(&package_dir, &state_root, "p14", "i1", "main").unwrap();
        session.act("go to the Hall of Embers").unwrap();
        session.act("take the Silver Key").unwrap();
        session.act("go to the Garden of Ash").unwrap();
        before = Some(serde_json::to_string(&session.snapshot().unwrap()).unwrap());
        session.close().unwrap();
    }
    {
        let mut session =
            WorldSession::open(&package_dir, &state_root, "p14", "i1", "main").unwrap();
        let after = serde_json::to_string(&session.snapshot().unwrap()).unwrap();
        session.close().unwrap();
        assert_eq!(before.unwrap(), after);
    }
    let _ = std::fs::remove_dir_all(&base);
}

// ---------------------------------------------------------------------------
// Negative: canon mismatch on bind
// ---------------------------------------------------------------------------
#[test]
fn negative_canon_mismatch_is_rejected() {
    let base = temp_base("neg1");
    let package_dir_a = build_package(&base, "nega", &fixture_text());
    // a different source, same world id — different canon identity
    let other = "The Quiet Grove\n\n## The Warden\n\nThe Warden keeps the Moss Key in the Sunken Chapel.";
    let package_dir_b = build_package_at(&base, "nega-b", "nega", other);
    let state_root = base.join("state");
    {
        let mut session =
            WorldSession::open(&package_dir_a, &state_root, "nega", "i1", "main").unwrap();
        session.close().unwrap();
    }
    // reopening with a different package for the same world must fail closed
    let result = WorldSession::open(&package_dir_b, &state_root, "nega", "i1", "main");
    assert!(matches!(result, Err(WorldSessionError::CanonMismatch { .. })));
    let _ = std::fs::remove_dir_all(&base);
}

// ---------------------------------------------------------------------------
// Negative: not-held / not-a-place / not-an-object
// ---------------------------------------------------------------------------
#[test]
fn negative_action_guards() {
    let base = temp_base("neg2");
    let package_dir = build_package(&base, "neg2", &fixture_text());
    let state_root = base.join("state");
    let mut session =
        WorldSession::open(&package_dir, &state_root, "neg2", "i1", "main").unwrap();

    // not-held: drop before take
    let result = session.act("drop the Silver Key");
    assert!(matches!(result, Err(WorldSessionError::NotHeld(_))));

    // not-a-place: going to an object
    let result = session.act("go to the Silver Key");
    assert!(matches!(result, Err(WorldSessionError::NotALocation(_))));

    // not-an-object: taking a person
    let result = session.act("take Keeper Sarn");
    assert!(matches!(result, Err(WorldSessionError::NotAnObject(_))));

    session.close().unwrap();
    let _ = std::fs::remove_dir_all(&base);
}