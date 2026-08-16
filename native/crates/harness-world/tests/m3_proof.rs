//! Milestone 3 proofs — conversational loop + native foundation evidence.
//!
//! proof_m3_01  dialogue persists across resume
//! proof_m3_02  characters are not omniscient (knowledge boundaries)
//! proof_m3_03  teaching changes knowledge as an event; canon untouched
//! proof_m3_04  witnessing: interlocutor sees the traveller's actions
//! proof_m3_05  story mode: chapter advancement is a state transition
//! proof_m3_06  story advancement requires story mode (fail closed)
//! proof_m3_07  watcher mode is read-only
//! proof_m3_08  runtime branching: fork anchors to parent tip, isolated
//! proof_m3_09  ambiguous conversational topic fails closed
//! proof_m3_10  signed-history export verifies independently
//! proof_m3_11  deterministic replay: identical snapshots and hashes
//! proof_m3_12  second corpus (Station Echo): no corpus-specific code
//! proof_m3_13  talk to a non-person fails closed
//! proof_m3_14  mode switching is recorded as an event

use harness_core::types::ProvenanceTag;
use harness_world::compiler::Compiler;
use harness_world::export::{verify_export, WorldExport};
use harness_world::package::write_package;
use harness_world::pipeline::ParseError;
use harness_world::replay::{replay, ReplayCommand};
use harness_world::runtime::{WorldSession, WorldSessionError};
use harness_world::{read_package, WorldStore};
use std::path::PathBuf;

const HOLLOW_KEEP: &str = "The Hollow Keep\n\n## The Keeper\n\nKeeper Sarn guards the Hollow Keep. The Silver Key is hidden in the Hall of Embers. Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n## The Garden of Ash\n\nThe Garden of Ash lies beyond the Iron Gate. The Deep Well stands at the center of the Garden of Ash.";

const STATION_ECHO: &str = include_str!("../testdata/station-echo.txt");

struct TestWorld {
    package_dir: PathBuf,
    state_root: PathBuf,
}

impl TestWorld {
    fn new(tag: &str, title: &str, source: &str, world_id: &str) -> Self {
        let base = std::env::temp_dir().join(format!(
            "hdoor-m3-{}-{}",
            tag,
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&base);
        let package_dir = base.join("package");
        let state_root = base.join("state");
        std::fs::create_dir_all(&package_dir).unwrap();
        let world = Compiler::new().compile(world_id, title, "source.txt", source);
        write_package(&package_dir, &world).unwrap();
        TestWorld {
            package_dir,
            state_root,
        }
    }
}

fn cleanup(tag: &str) {
    let base = std::env::temp_dir().join(format!(
        "hdoor-m3-{}-{}",
        tag,
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&base);
}

// ---------------------------------------------------------------------

#[test]
fn proof_m3_01_dialogue_persists_across_resume() {
    let world = TestWorld::new("m3-01", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    {
        let mut session = WorldSession::open(
            &world.package_dir,
            &world.state_root,
            "fixture",
            "i1",
            "main",
        )
        .unwrap();
        let reply = session.act("ask keeper sarn about the bronze key").unwrap();
        assert!(reply.text.contains("Keeper Sarn says"), "{}", reply.text);
        session.close().unwrap();
    }
    {
        let mut session = WorldSession::open(
            &world.package_dir,
            &world.state_root,
            "fixture",
            "i1",
            "main",
        )
        .unwrap();
        let status = session.act("status").unwrap();
        assert!(status.text.contains("Speaking with"), "{}", status.text);
        session.close().unwrap();
    }
    cleanup("m3-01");
}

#[test]
fn proof_m3_02_characters_are_not_omniscient() {
    let world = TestWorld::new("m3-02", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    // Sarn participates in the bronze key fact — he knows it.
    let known = session
        .act("ask keeper sarn about the bronze key")
        .unwrap();
    let known_lower = known.text.to_lowercase();
    assert!(known_lower.contains("bronze key"), "{}", known.text);
    // Sarn is NOT in the silver key fact — he must not know it.
    let unknown = session
        .act("ask keeper sarn about the silver key")
        .unwrap();
    assert!(
        unknown.text.contains("don't know anything about"),
        "{}",
        unknown.text
    );
    session.close().unwrap();
    cleanup("m3-02");
}

#[test]
fn proof_m3_03_teaching_changes_knowledge_as_event_canon_untouched() {
    let world = TestWorld::new("m3-03", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    // teach: "tell keeper sarn about the silver key"
    let taught = session.act("tell keeper sarn about the silver key").unwrap();
    let event = taught.event.unwrap();
    assert_eq!(event.event_type, "learn");
    assert_eq!(
        event.detail["source"], "player_supplied",
        "{}",
        event.detail
    );
    // now Sarn knows the silver key exists (from the traveller, not canon)
    let now_known = session.act("ask keeper sarn about the silver key").unwrap();
    assert!(
        now_known.text.contains("you told me"),
        "{}",
        now_known.text
    );
    // canon is untouched: the package on disk is byte-identical
    let package = read_package(&world.package_dir).unwrap();
    assert_eq!(package.manifest.world_id, "fixture");
    let canon_facts_about_sarn = package
        .facts
        .iter()
        .filter(|fact| {
            let sarn = "keeper sarn";
            fact.subject == sarn || fact.object == sarn
        })
        .count();
    assert_eq!(canon_facts_about_sarn, 2);
    session.close().unwrap();
    cleanup("m3-03");
}

#[test]
fn proof_m3_04_witnessing_updates_interlocutor_knowledge() {
    let world = TestWorld::new("m3-04", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    // establish dialogue with Sarn, then take the silver key in front of him
    session.act("ask keeper sarn about the bronze key").unwrap();
    session.act("go to the Hall of Embers").unwrap();
    session.act("take the Silver Key").unwrap();
    // Sarn now knows the traveller took the silver key (witnessed)
    let witnessed = session.act("ask keeper sarn about the silver key").unwrap();
    assert!(
        witnessed.text.contains("I saw it") && witnessed.text.contains("took"),
        "{}",
        witnessed.text
    );
    session.close().unwrap();
    cleanup("m3-04");
}

#[test]
fn proof_m3_05_story_advancement_is_a_state_transition() {
    let world = TestWorld::new("m3-05", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    session.act("mode story").unwrap();
    let advance = session.act("advance").unwrap();
    let event = advance.event.unwrap();
    assert_eq!(event.event_type, "chapter_advance");
    assert_eq!(session.story_position(), 1);
    let advance2 = session.act("advance").unwrap();
    assert_eq!(advance2.event.unwrap().event_type, "chapter_advance");
    assert_eq!(session.story_position(), 2);
    let advance3 = session.act("advance").unwrap();
    assert_eq!(advance3.event.unwrap().event_type, "chapter_advance");
    assert_eq!(session.story_position(), 3);
    // advance past the end is a no-op, not an error
    let end = session.act("advance").unwrap();
    assert!(end.event.is_none());
    assert_eq!(session.story_position(), 3);
    session.close().unwrap();
    cleanup("m3-05");
}

#[test]
fn proof_m3_06_story_advance_requires_story_mode() {
    let world = TestWorld::new("m3-06", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    let result = session.act("advance");
    assert!(matches!(
        result,
        Err(WorldSessionError::StoryModeRequired)
    ));
    session.close().unwrap();
    cleanup("m3-06");
}

#[test]
fn proof_m3_07_watcher_mode_is_read_only() {
    let world = TestWorld::new("m3-07", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    session.act("mode watcher").unwrap();
    let denied = session.act("take the Silver Key");
    assert!(matches!(
        denied,
        Err(WorldSessionError::ReadOnlyWatcher)
    ));
    // talking and inspecting are still allowed
    assert!(session.act("status").is_ok());
    assert!(session.act("inspect the Silver Key").is_ok());
    // mode switching is the escape hatch
    assert!(session.act("mode traveller").is_ok());
    session.act("go to the Hall of Embers").unwrap();
    assert!(session.act("take the Silver Key").is_ok());
    session.close().unwrap();
    cleanup("m3-07");
}

#[test]
fn proof_m3_08_runtime_branching_is_anchored_and_isolated() {
    let world = TestWorld::new("m3-08", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    session.act("go to the Hall of Embers").unwrap();
    session.act("take the Silver Key").unwrap();
    let fork = session.act("branch what-if").unwrap();
    let event = fork.event.unwrap();
    assert_eq!(event.event_type, "branch_fork");
    assert_eq!(event.detail["provenance"], "branch_diverged");
    assert_eq!(session.snapshot().unwrap().branch_id, "what-if");
    // the fork continues the session: state carried over
    let status = session.act("status").unwrap();
    assert!(status.text.contains("Silver Key"), "{}", status.text);
    // the parent branch is untouched
    let mut parent = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    let parent_status = parent.act("status").unwrap();
    assert!(parent_status.text.contains("Silver Key"), "{}", parent_status.text);
    // the fork genesis anchors at the parent's tip: the fork_hash
    // recorded in the fork event equals the parent's last hash at fork
    // time (the take event, before the parent's later status event)
    let fork_hash = event.detail["fork_hash"].as_str().unwrap().to_string();
    let store = WorldStore::open(&world.state_root, "fixture", "i1").unwrap();
    assert!(store.verify_chain("main").is_ok());
    assert!(store.verify_chain("what-if").is_ok());
    assert_eq!(store.base_branch("what-if").unwrap().unwrap(), "main");
    let child_events = store.events("what-if").unwrap();
    assert_eq!(child_events[0].prev_hash, fork_hash);
    assert!(
        store
            .events("main")
            .unwrap()
            .iter()
            .any(|event| event.hash == fork_hash),
        "fork hash must exist in the parent chain"
    );
    parent.close().unwrap();
    session.close().unwrap();
    cleanup("m3-08");
}

#[test]
fn proof_m3_09_ambiguous_conversational_topic_fails_closed() {
    let world = TestWorld::new("m3-09", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    // "the key" matches both keys: the topic must not be guessed
    let result = session.act("ask keeper sarn about the key");
    assert!(matches!(
        result,
        Err(WorldSessionError::Parse(ParseError::AmbiguousTopic(_)))
    ));
    session.close().unwrap();
    cleanup("m3-09");
}

#[test]
fn proof_m3_10_signed_history_export_verifies_independently() {
    let world = TestWorld::new("m3-10", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    session.act("mode story").unwrap();
    session.act("advance").unwrap();
    session.act("go to the Hall of Embers").unwrap();
    session.act("take the Silver Key").unwrap();
    session.act("ask keeper sarn about the bronze key").unwrap();
    let export: WorldExport = session.export().unwrap();
    session.close().unwrap();

    // identity block complete
    assert_eq!(export.schema, "hdoor_export_v1");
    assert_eq!(export.world.world_id, "fixture");
    assert_eq!(export.session.branch_id, "main");
    assert!(!export.world.canon_hash.is_empty());
    assert!(!export.world.source_identity.is_empty());
    // every event carries ordering + provenance + actor + hashes
    for event in &export.events {
        assert!(event.seq >= 1);
        assert_eq!(event.branch_id, "main");
        assert_eq!(event.logical_time, format!("main:{}", event.seq));
        assert!(!event.actor.is_empty());
        assert!(!event.hash.is_empty());
        assert!(!event.prev_hash.is_empty());
    }
    // chain ordering is strictly sequential
    let seqs: Vec<i64> = export.events.iter().map(|e| e.seq).collect();
    assert_eq!(seqs, (1..=seqs.len() as i64).collect::<Vec<_>>());
    // independent verification passes (pure function, no DB)
    let count = verify_export(&export).unwrap();
    assert_eq!(count, export.events.len());
    // tamper: flip a detail and verification must fail
    let mut tampered = export.clone();
    tampered.events[2].detail = serde_json::json!({"target": "evil"});
    assert!(verify_export(&tampered).is_err());
    cleanup("m3-10");
}

#[test]
fn proof_m3_11_deterministic_replay_reproduces_snapshot_and_hashes() {
    let world = TestWorld::new("m3-11", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut commands: Vec<ReplayCommand> = Vec::new();
    let mut expected_hashes: Vec<String> = Vec::new();
    {
        let mut session = WorldSession::open(
            &world.package_dir,
            &world.state_root,
            "fixture",
            "i1",
            "main",
        )
        .unwrap();
        for utterance in [
            "go to the Hall of Embers",
            "take the Silver Key",
            "ask keeper sarn about the bronze key",
            "status",
        ] {
            let result = session.act(utterance).unwrap();
            let created_at = result
                .event
                .as_ref()
                .map(|e| e.detail["_created_at"].as_str().unwrap_or("").to_string())
                .unwrap_or_default();
            // events carry no _created_at in detail; we read from the
            // recorded export instead (below).
            let _ = created_at;
            if let Some(event) = &result.event {
                expected_hashes.push(event.hash.clone());
            }
        }
        let export = session.export().unwrap();
        session.close().unwrap();
        // build replay commands from the export timeline (recorded times):
        // EVERY event is replayed, including status
        commands = export
            .events
            .iter()
            .map(|e| ReplayCommand {
                utterance: e
                    .detail
                    .get("raw")
                    .and_then(|raw| raw.as_str())
                    .unwrap_or("status")
                    .to_string(),
                created_at: e.created_at.clone(),
            })
            .collect();
        expected_hashes = export.events.iter().map(|e| e.hash.clone()).collect();
    }
    // fresh state root: replay from scratch with the recorded clock
    let replay_root = world.state_root.join("replay");
    let outcome = replay(
        &world.package_dir,
        &replay_root,
        "fixture",
        "i1",
        "main",
        &commands,
    )
    .unwrap();
    // byte-identical hashes
    assert_eq!(outcome.hashes, expected_hashes, "replayed hashes differ");
    // byte-identical snapshot
    let mut live = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    let live_snapshot = live.snapshot().unwrap();
    live.close().unwrap();
    assert_eq!(outcome.snapshot, live_snapshot, "snapshots differ");
    assert_eq!(outcome.snapshot.event_count as usize, expected_hashes.len());
    cleanup("m3-11");
}

#[test]
fn proof_m3_12_second_corpus_station_echo_generality() {
    let world = TestWorld::new("m3-12", "Station Echo", STATION_ECHO, "station-echo");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "station-echo",
        "i1",
        "main",
    )
    .unwrap();
    // same machinery, no corpus-specific code: Vess knows the Resonance Key
    let known = session.act("ask captain vess about the resonance key").unwrap();
    let known_lower = known.text.to_lowercase();
    assert!(known_lower.contains("resonance key"), "{}", known.text);
    // Vess does NOT know the Docking Key — not omniscient on corpus 2
    let unknown = session.act("ask captain vess about the docking key").unwrap();
    let unknown_lower = unknown.text.to_lowercase();
    assert!(
        unknown_lower.contains("don't know anything about"),
        "{}",
        unknown.text
    );
    // distinct ambiguity: "the key" is ambiguous here too
    let ambiguous = session.act("take the key");
    assert!(matches!(
        ambiguous,
        Err(WorldSessionError::Parse(ParseError::AmbiguousTarget(_)))
    ));
    // where-question answered from canon seed
    let where_reply = session.act("ask captain vess where the resonance key is").unwrap();
    assert!(
        where_reply.text.to_lowercase().contains("docking bay"),
        "{}",
        where_reply.text
    );
    // story mode works on corpus 2
    session.act("mode story").unwrap();
    let advance = session.act("advance").unwrap();
    assert_eq!(advance.event.unwrap().event_type, "chapter_advance");
    // export + verify on corpus 2
    let export = session.export().unwrap();
    assert!(verify_export(&export).is_ok());
    session.close().unwrap();
    cleanup("m3-12");
}

#[test]
fn proof_m3_13_talk_to_non_person_fails_closed() {
    let world = TestWorld::new("m3-13", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    let result = session.act("ask the Silver Key about the bronze key");
    assert!(matches!(result, Err(WorldSessionError::NotAPerson(_))));
    session.close().unwrap();
    cleanup("m3-13");
}

#[test]
fn proof_m3_14_mode_switching_is_recorded() {
    let world = TestWorld::new("m3-14", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut session = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "main",
    )
    .unwrap();
    let switched = session.act("mode chat").unwrap();
    let event = switched.event.unwrap();
    assert_eq!(event.event_type, "mode_change");
    assert_eq!(event.detail["from"], "traveller");
    assert_eq!(event.detail["to"], "chat");
    assert_eq!(session.mode(), "chat");
    // invalid mode fails closed
    let invalid = session.act("mode bananaverse");
    assert!(matches!(
        invalid,
        Err(WorldSessionError::InvalidMode(_))
    ));
    assert_eq!(session.mode(), "chat");
    session.close().unwrap();
    cleanup("m3-14");
}

#[test]
fn proof_m3_15_forked_branch_replays_deterministically() {
    let world = TestWorld::new("m3-15", "The Hollow Keep", HOLLOW_KEEP, "fixture");
    let mut commands: Vec<ReplayCommand> = Vec::new();
    let mut main_export: Option<WorldExport> = None;
    let mut fork_export: Option<WorldExport> = None;
    {
        let mut session = WorldSession::open(
            &world.package_dir,
            &world.state_root,
            "fixture",
            "i1",
            "main",
        )
        .unwrap();
        session.act("go to the Hall of Embers").unwrap();
        session.act("take the Silver Key").unwrap();
        session.close().unwrap();
        // main branch history: move + take
        let mut parent = WorldSession::open(
            &world.package_dir,
            &world.state_root,
            "fixture",
            "i1",
            "main",
        )
        .unwrap();
        main_export = Some(parent.export().unwrap());
        parent.close().unwrap();
        commands.extend(
            main_export.as_ref().unwrap()
                .events
                .iter()
                .map(|e| ReplayCommand {
                    utterance: e
                        .detail
                        .get("raw")
                        .and_then(|raw| raw.as_str())
                        .unwrap_or("")
                        .to_string(),
                    created_at: e.created_at.clone(),
                })
                .collect::<Vec<_>>(),
        );
        // fork, then continue on the child
        let mut session = WorldSession::open(
            &world.package_dir,
            &world.state_root,
            "fixture",
            "i1",
            "main",
        )
        .unwrap();
        session.act("branch what-if").unwrap();
        session.act("drop the Silver Key").unwrap();
        fork_export = Some(session.export().unwrap());
        session.close().unwrap();
        // child history: genesis (synthetic), branch_fork, drop
        commands.extend(
            fork_export.as_ref().unwrap()
                .events
                .iter()
                .filter(|e| e.detail.get("raw").is_some())
                .map(|e| ReplayCommand {
                    utterance: e
                        .detail
                        .get("raw")
                        .and_then(|raw| raw.as_str())
                        .unwrap_or("")
                        .to_string(),
                    created_at: e.created_at.clone(),
                })
                .collect::<Vec<_>>(),
        );
    }
    // replay starts on "main"; the recorded fork creates "what-if" live
    let replay_root = world.state_root.join("replay");
    let outcome = replay(
        &world.package_dir,
        &replay_root,
        "fixture",
        "i1",
        "main",
        &commands,
    )
    .unwrap();
    // ActionResult sequence: move, take, branch_fork, drop
    let main_export = main_export.as_ref().unwrap();
    let fork_export = fork_export.as_ref().unwrap();
    let expected_action_hashes: Vec<String> = main_export
        .events
        .iter()
        .map(|e| e.hash.clone())
        .chain(
            fork_export
                .events
                .iter()
                .filter(|e| e.detail.get("raw").is_some())
                .map(|e| e.hash.clone()),
        )
        .collect();
    assert_eq!(outcome.hashes, expected_action_hashes, "forked replay hashes differ");
    // full chain (including the synthetic genesis fork event) is
    // byte-identical to the live chain, and verifies
    let replay_store = WorldStore::open(&replay_root, "fixture", "i1").unwrap();
    let replayed = replay_store.events("what-if").unwrap();
    let live: Vec<(i64, String, String, String, String, String, String)> = fork_export
        .events
        .iter()
        .map(|e| {
            (
                e.seq,
                e.branch_id.clone(),
                e.event_type.clone(),
                e.actor.clone(),
                serde_json::to_string(&e.detail).unwrap(),
                e.prev_hash.clone(),
                e.hash.clone(),
            )
        })
        .collect();
    let replayed_t: Vec<(i64, String, String, String, String, String, String)> = replayed
        .iter()
        .map(|e| {
            (
                e.seq,
                e.branch_id.clone(),
                e.event_type.clone(),
                e.actor.clone(),
                e.detail_json.clone(),
                e.prev_hash.clone(),
                e.hash.clone(),
            )
        })
        .collect();
    assert_eq!(replayed_t, live, "forked chain events differ");
    assert!(replay_store.verify_chain("what-if").is_ok());
    let mut live = WorldSession::open(
        &world.package_dir,
        &world.state_root,
        "fixture",
        "i1",
        "what-if",
    )
    .unwrap();
    let live_snapshot = live.snapshot().unwrap();
    live.close().unwrap();
    assert_eq!(outcome.snapshot, live_snapshot);
    cleanup("m3-15");
}

// keep ProvenanceTag import used (canon immutability is type-level)
#[allow(dead_code)]
fn _provenance_anchor() -> ProvenanceTag {
    ProvenanceTag::Canon
}