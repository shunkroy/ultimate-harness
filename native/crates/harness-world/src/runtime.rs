//! World runtime: sessions over a .hdoor package + signed store.
//!
//! Actions are deterministic offline transformations of session state.
//! Every action is recorded as a signed, chained event; canon (the
//! package) is never mutated. Close/resume round-trips must reproduce
//! byte-identical snapshots.

use crate::compiler::Entity;
use crate::package::{read_package, Package, PackageError};
use crate::pipeline::{Intent, ParseError, Pipeline};
use crate::store::{WorldStore, WorldStoreError};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug)]
pub enum WorldSessionError {
    Package(PackageError),
    Store(WorldStoreError),
    Parse(ParseError),
    NotHere(String),
    NotHeld(String),
    NotALocation(String),
    NotAnObject(String),
    NotAPerson(String),
    BranchMismatch { expected: String, actual: String },
    CanonMismatch { expected: String, actual: String },
    Io(String),
}

impl std::fmt::Display for WorldSessionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WorldSessionError::Package(err) => write!(f, "{}", err),
            WorldSessionError::Store(err) => write!(f, "{}", err),
            WorldSessionError::Parse(err) => write!(f, "{}", err),
            WorldSessionError::NotHere(name) => write!(f, "'{}' is not here", name),
            WorldSessionError::NotHeld(name) => write!(f, "you do not hold '{}'", name),
            WorldSessionError::NotALocation(name) => write!(f, "'{}' is not a place", name),
            WorldSessionError::NotAnObject(name) => write!(f, "'{}' is not a thing you can hold", name),
            WorldSessionError::NotAPerson(name) => write!(f, "'{}' is not someone you can address", name),
            WorldSessionError::BranchMismatch { expected, actual } => write!(
                f,
                "branch mismatch: session on '{}', store has '{}'",
                expected, actual
            ),
            WorldSessionError::CanonMismatch { expected, actual } => write!(
                f,
                "canon mismatch: package hash '{}', store bound to '{}'",
                expected, actual
            ),
            WorldSessionError::Io(message) => write!(f, "session io: {}", message),
        }
    }
}

impl std::error::Error for WorldSessionError {}

impl From<PackageError> for WorldSessionError {
    fn from(err: PackageError) -> Self {
        WorldSessionError::Package(err)
    }
}
impl From<WorldStoreError> for WorldSessionError {
    fn from(err: WorldStoreError) -> Self {
        WorldSessionError::Store(err)
    }
}
impl From<ParseError> for WorldSessionError {
    fn from(err: ParseError) -> Self {
        WorldSessionError::Parse(err)
    }
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SessionSnapshot {
    pub world_id: String,
    pub instance_id: String,
    pub branch_id: String,
    pub location: String,
    pub inventory: Vec<String>,
    pub event_count: i64,
    pub canon_hash: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ActionResult {
    pub text: String,
    pub event: Option<ActionEvent>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ActionEvent {
    pub event_type: String,
    pub actor: String,
    pub detail: serde_json::Value,
    pub hash: String,
}

fn now_ms() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis()
        .to_string()
}

pub struct WorldSession {
    package: Package,
    store: WorldStore,
    branch_id: String,
    instance_id: String,
    location: String,
    inventory: Vec<String>,
    entities_by_id: BTreeMap<String, Entity>,
    pipeline: Pipeline<'static>,
}

impl WorldSession {
    /// Open a session on a package + store (fresh or resumable).
    pub fn open(
        package_path: &Path,
        state_root: &Path,
        world_id: &str,
        instance_id: &str,
        branch_id: &str,
    ) -> Result<Self, WorldSessionError> {
        let package = read_package(package_path)?;
        let store = WorldStore::open(state_root, world_id, instance_id)?;
        let canon_hash = format!("sha256:{}", package.canon_source_hash);
        match store.meta("canon_manifest_hash")? {
            Some(bound) if bound != canon_hash => {
                return Err(WorldSessionError::CanonMismatch {
                    expected: canon_hash,
                    actual: bound,
                });
            }
            Some(_) => {}
            None => store.bind_canon(&canon_hash)?,
        }
        store.create_branch(branch_id, branch_id, None, &now_ms())?;

        let entities_by_id: BTreeMap<String, Entity> = package
            .entities
            .iter()
            .map(|entity| (entity.id.clone(), entity.clone()))
            .collect();

        // resume state if present
        let location = store
            .kv_get(branch_id, "location")?
            .unwrap_or_else(|| package.seed.traveller_start.clone());
        let inventory: Vec<String> = store
            .kv_get(branch_id, "inventory")?
            .map(|raw| {
                serde_json::from_str::<Vec<String>>(&raw).unwrap_or_default()
            })
            .unwrap_or_default();

        let index = Box::leak(Box::new(package.index.clone()));
        let pipeline = Pipeline::new(index);

        Ok(WorldSession {
            package,
            store,
            branch_id: branch_id.to_string(),
            instance_id: instance_id.to_string(),
            location,
            inventory,
            entities_by_id,
            pipeline,
        })
    }

    pub fn snapshot(&self) -> Result<SessionSnapshot, WorldSessionError> {
        let event_count = self.store.verify_chain(&self.branch_id)? as i64;
        Ok(SessionSnapshot {
            world_id: self.package.manifest.world_id.clone(),
            instance_id: self.instance_id.clone(),
            branch_id: self.branch_id.clone(),
            location: self.location.clone(),
            inventory: {
                let mut inv = self.inventory.clone();
                inv.sort();
                inv
            },
            event_count,
            canon_hash: format!("sha256:{}", self.package.canon_source_hash),
        })
    }

    fn persist(&self) -> Result<(), WorldSessionError> {
        self.store
            .kv_set(&self.branch_id, "location", &self.location)?;
        let inventory_json = serde_json::to_string(&self.inventory)
            .map_err(|err| WorldSessionError::Io(err.to_string()))?;
        self.store
            .kv_set(&self.branch_id, "inventory", &inventory_json)?;
        Ok(())
    }

    fn entity_name(&self, id: &str) -> String {
        self.entities_by_id
            .get(id)
            .map(|e| e.name.clone())
            .unwrap_or_else(|| id.to_string())
    }

    fn kind(&self, id: &str) -> &str {
        self.entities_by_id
            .get(id)
            .map(|e| e.kind.as_str())
            .unwrap_or("other")
    }

    fn facts_about(&self, id: &str) -> Vec<String> {
        self.package
            .facts
            .iter()
            .filter(|fact| fact.subject == id || fact.object == id)
            .map(|fact| {
                format!(
                    "{} {} {}",
                    self.entity_name(&fact.subject),
                    fact.predicate,
                    self.entity_name(&fact.object)
                )
            })
            .collect()
    }

    fn record(
        &mut self,
        event_type: &str,
        detail: &serde_json::Value,
    ) -> Result<String, WorldSessionError> {
        let detail_json = serde_json::to_string(&detail)
            .map_err(|err| WorldSessionError::Io(err.to_string()))?;
        let hash = self.store.append_event(
            &self.branch_id,
            event_type,
            "traveller",
            &detail_json,
            &now_ms(),
        )?;
        self.persist()?;
        Ok(hash)
    }

    /// Resolve parse targets to a single entity, using context to break
    /// ambiguity: objects prefer current location, places prefer travel.
    fn resolve_target(
        &self,
        intent: &Intent,
        targets: &[crate::index::EntityRef],
    ) -> Result<crate::index::EntityRef, WorldSessionError> {
        if targets.is_empty() {
            return Err(WorldSessionError::Parse(ParseError::NoTarget));
        }
        if targets.len() == 1 {
            return Ok(targets[0].clone());
        }
        // contextual disambiguation — never silent randomness
        let mut candidates: Vec<&crate::index::EntityRef> = targets.iter().collect();
        match intent {
            Intent::Take | Intent::Drop | Intent::Use | Intent::Open | Intent::Close
            | Intent::Give => {
                candidates.retain(|t| {
                    self.kind(&t.id) == "object"
                        && (self.location == t.id
                            || self
                                .package
                                .seed
                                .objects_by_location
                                .get(&self.location)
                                .map(|objects| objects.contains(&t.id))
                                .unwrap_or(false)
                            || self.inventory.contains(&t.id))
                });
            }
            Intent::Go => {
                candidates.retain(|t| self.kind(&t.id) == "place");
            }
            _ => {}
        }
        match candidates.len() {
            0 => Err(WorldSessionError::Parse(ParseError::AmbiguousTarget(
                crate::index::ResolveError::Ambiguous {
                    query: targets[0].matched_alias.clone(),
                    candidates: targets.to_vec(),
                },
            ))),
            1 => Ok(candidates[0].clone()),
            _ => Err(WorldSessionError::Parse(ParseError::AmbiguousTarget(
                crate::index::ResolveError::Ambiguous {
                    query: targets[0].matched_alias.clone(),
                    candidates: targets.to_vec(),
                },
            ))),
        }
    }

    pub fn act(&mut self, utterance: &str) -> Result<ActionResult, WorldSessionError> {
        let parsed = self.pipeline.parse(utterance)?;

        // target-less intents handled before resolution
        match parsed.intent {
            Intent::Status => {
                let objects_here: Vec<String> = self
                    .package
                    .seed
                    .objects_by_location
                    .get(&self.location)
                    .cloned()
                    .unwrap_or_default()
                    .iter()
                    .map(|id| self.entity_name(id))
                    .collect();
                let detail = serde_json::json!({
                    "location": self.location,
                    "inventory": self.inventory,
                    "raw": parsed.raw,
                });
                let hash = self.record("status", &detail)?;
                return Ok(ActionResult {
                    text: format!(
                        "You are at {}.\nHere: {}\nYou hold: {}\nEvents: {}",
                        self.entity_name(&self.location),
                        if objects_here.is_empty() {
                            "nothing".to_string()
                        } else {
                            objects_here.join(", ")
                        },
                        if self.inventory.is_empty() {
                            "nothing".to_string()
                        } else {
                            self.inventory
                                .iter()
                                .map(|id| self.entity_name(id))
                                .collect::<Vec<_>>()
                                .join(", ")
                        },
                        self.store
                            .verify_chain(&self.branch_id)
                            .map(|count| count.to_string())
                            .unwrap_or_else(|_| "?".to_string())
                    ),
                    event: Some(ActionEvent {
                        event_type: "status".into(),
                        actor: "traveller".into(),
                        detail,
                        hash,
                    }),
                });
            }
            Intent::Help => {
                return Ok(ActionResult {
                    text: "Commands: inspect/take/drop/go/talk/use/open/close/give/status/help"
                        .to_string(),
                    event: None,
                });
            }
            _ => {}
        }

        let target = self.resolve_target(&parsed.intent, &parsed.targets)?;
        let id = target.id.clone();
        let name = self.entity_name(&id);

        let detail = serde_json::json!({
            "target": id,
            "location": self.location,
            "inventory": self.inventory,
            "raw": parsed.raw,
        });

        match parsed.intent {
            Intent::Inspect => {
                let at_location = self
                    .package
                    .seed
                    .objects_by_location
                    .get(&self.location)
                    .map(|objects| objects.contains(&id))
                    .unwrap_or(false)
                    || self.location == id;
                let held = self.inventory.contains(&id);
                let mut lines = vec![format!(
                    "{} — {} (tag: {}, location: {}, held: {})",
                    name,
                    self.kind(&id),
                    self.entities_by_id
                        .get(&id)
                        .map(|e| format!("{:?}", e.tag))
                        .unwrap_or_default(),
                    at_location,
                    held
                )];
                lines.extend(self.facts_about(&id));
                let hash = self.record("inspect", &detail)?;
                Ok(ActionResult {
                    text: lines.join("\n"),
                    event: Some(ActionEvent {
                        event_type: "inspect".into(),
                        actor: "traveller".into(),
                        detail: detail.clone(),
                        hash,
                    }),
                })
            }
            Intent::Take => {
                if self.kind(&id) != "object" {
                    return Err(WorldSessionError::NotAnObject(name));
                }
                let at_location = self
                    .package
                    .seed
                    .objects_by_location
                    .get(&self.location)
                    .map(|objects| objects.contains(&id))
                    .unwrap_or(false);
                if !at_location {
                    return Err(WorldSessionError::NotHere(name));
                }
                if self.inventory.contains(&id) {
                    return Err(WorldSessionError::NotHere(format!("{} (already held)", name)));
                }
                self.inventory.push(id.clone());
                let hash = self.record("take", &detail)?;
                Ok(ActionResult {
                    text: format!("You take the {}.", name),
                    event: Some(ActionEvent {
                        event_type: "take".into(),
                        actor: "traveller".into(),
                        detail: detail.clone(),
                        hash,
                    }),
                })
            }
            Intent::Drop => {
                if self.kind(&id) != "object" {
                    return Err(WorldSessionError::NotAnObject(name));
                }
                if !self.inventory.contains(&id) {
                    return Err(WorldSessionError::NotHeld(name));
                }
                self.inventory.retain(|item| item != &id);
                let hash = self.record("drop", &detail)?;
                Ok(ActionResult {
                    text: format!("You drop the {}.", name),
                    event: Some(ActionEvent {
                        event_type: "drop".into(),
                        actor: "traveller".into(),
                        detail: detail.clone(),
                        hash,
                    }),
                })
            }
            Intent::Go => {
                if self.kind(&id) != "place" {
                    return Err(WorldSessionError::NotALocation(name));
                }
                self.location = id.clone();
                let hash = self.record("move", &detail)?;
                Ok(ActionResult {
                    text: format!("You go to {}.", name),
                    event: Some(ActionEvent {
                        event_type: "move".into(),
                        actor: "traveller".into(),
                        detail: detail.clone(),
                        hash,
                    }),
                })
            }
            Intent::Use | Intent::Open | Intent::Close => {
                let event_type = match parsed.intent {
                    Intent::Use => "use",
                    Intent::Open => "open",
                    _ => "close",
                };
                let held = self.inventory.contains(&id);
                let at_location = self
                    .package
                    .seed
                    .objects_by_location
                    .get(&self.location)
                    .map(|objects| objects.contains(&id))
                    .unwrap_or(false)
                    || self.location == id;
                if !held && !at_location {
                    return Err(WorldSessionError::NotHere(name));
                }
                let hash = self.record(event_type, &detail)?;
                Ok(ActionResult {
                    text: format!("You {} the {}. (no deterministic effect in v1)", event_type, name),
                    event: Some(ActionEvent {
                        event_type: event_type.into(),
                        actor: "traveller".into(),
                        detail: detail.clone(),
                        hash,
                    }),
                })
            }
            Intent::Talk => {
                if self.kind(&id) != "person" {
                    return Err(WorldSessionError::NotAPerson(name));
                }
                let mut lines = vec![format!("{} speaks:", name)];
                lines.extend(self.facts_about(&id));
                let hash = self.record("talk", &detail)?;
                Ok(ActionResult {
                    text: lines.join("\n"),
                    event: Some(ActionEvent {
                        event_type: "talk".into(),
                        actor: "traveller".into(),
                        detail: detail.clone(),
                        hash,
                    }),
                })
            }
            Intent::Give => {
                if !self.inventory.contains(&id) {
                    return Err(WorldSessionError::NotHeld(name));
                }
                let hash = self.record("give", &detail)?;
                Ok(ActionResult {
                    text: format!("You offer the {} (nobody accepts in v1).", name),
                    event: Some(ActionEvent {
                        event_type: "give".into(),
                        actor: "traveller".into(),
                        detail: detail.clone(),
                        hash,
                    }),
                })
            }
            Intent::Status | Intent::Help | Intent::Unknown => {
                Err(WorldSessionError::Parse(ParseError::UnknownIntent(
                    utterance.to_string(),
                )))
            }
        }
    }

    /// Close the session (final persist + chain verification).
    pub fn close(&self) -> Result<(), WorldSessionError> {
        self.persist()?;
        self.store.verify_chain(&self.branch_id)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compiler::Compiler;
    use crate::package::write_package;
    use std::path::PathBuf;

    const FIXTURE: &str = "The Hollow Keep\n\n## The Keeper\n\nKeeper Sarn guards the Hollow Keep. The Silver Key is hidden in the Hall of Embers. Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n## The Garden of Ash\n\nThe Garden of Ash lies beyond the Iron Gate. The Deep Well stands at the center of the Garden of Ash.";

    struct TestWorld {
        package_dir: PathBuf,
        state_root: PathBuf,
    }

    impl TestWorld {
        fn new(tag: &str) -> Self {
            let base = std::env::temp_dir().join(format!(
                "hdoor-runtime-{}-{}",
                tag,
                std::process::id()
            ));
            let _ = std::fs::remove_dir_all(&base);
            let package_dir = base.join("package");
            let state_root = base.join("state");
            std::fs::create_dir_all(&package_dir).unwrap();
            let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
            write_package(&package_dir, &world).unwrap();
            TestWorld {
                package_dir,
                state_root,
            }
        }
    }

    fn cleanup(tag: &str) {
        let base = std::env::temp_dir().join(format!(
            "hdoor-runtime-{}-{}",
            tag,
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn open_take_go_close_round_trip() {
        let world = TestWorld::new("rt1");
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
            assert!(status.text.contains("Hollow Keep"), "{}", status.text);
            let moved = session.act("go to the Hall of Embers").unwrap();
            assert_eq!(moved.event.unwrap().event_type, "move");
            let taken = session.act("take the Silver Key").unwrap();
            assert_eq!(taken.event.unwrap().event_type, "take");
            session.close().unwrap();
        }
        // resume: state must be identical
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
            assert!(status.text.contains("Hall of Embers"), "{}", status.text);
            assert!(status.text.contains("Silver Key"), "{}", status.text);
            session.close().unwrap();
        }
        cleanup("rt1");
    }

    #[test]
    fn snapshot_is_identical_across_resume() {
        let world = TestWorld::new("rt2");
        let mut first: Option<SessionSnapshot> = None;
        {
            let mut session = WorldSession::open(
                &world.package_dir,
                &world.state_root,
                "fixture",
                "i1",
                "main",
            )
            .unwrap();
            session.act("go to the Deep Well").unwrap();
            session.act("take the Bronze Key").unwrap();
            session.act("go to the Garden of Ash").unwrap();
            first = Some(session.snapshot().unwrap());
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
            let second = session.snapshot().unwrap();
            session.close().unwrap();
            assert_eq!(first.unwrap(), second);
        }
        cleanup("rt2");
    }

    #[test]
    fn cannot_take_what_is_not_here() {
        let world = TestWorld::new("rt3");
        let mut session = WorldSession::open(
            &world.package_dir,
            &world.state_root,
            "fixture",
            "i1",
            "main",
        )
        .unwrap();
        // bronze key is at the Deep Well; start is Hall of Embers
        let result = session.act("take the Bronze Key");
        assert!(matches!(result, Err(WorldSessionError::NotHere(_))));
        cleanup("rt3");
    }

    #[test]
    fn ambiguous_target_fails_closed() {
        let world = TestWorld::new("rt4");
        let mut session = WorldSession::open(
            &world.package_dir,
            &world.state_root,
            "fixture",
            "i1",
            "main",
        )
        .unwrap();
        // "the key" resolves to both silver and bronze
        let result = session.act("take the key");
        assert!(matches!(result, Err(WorldSessionError::Parse(_))));
        cleanup("rt4");
    }

    #[test]
    fn canon_identity_is_bound() {
        let world = TestWorld::new("rt5");
        // opening a session binds the store to the package canon
        {
            let mut session = WorldSession::open(
                &world.package_dir,
                &world.state_root,
                "fixture",
                "i1",
                "main",
            )
            .unwrap();
            session.close().unwrap();
        }
        let store = WorldStore::open(&world.state_root, "fixture", "i1").unwrap();
        let bound = store.meta("canon_manifest_hash").unwrap().unwrap();
        let package = read_package(&world.package_dir).unwrap();
        assert_eq!(
            bound,
            format!("sha256:{}", package.canon_source_hash)
        );
        cleanup("rt5");
    }

    #[test]
    fn branch_isolation_persists() {
        let world = TestWorld::new("rt6");
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
        }
        {
            // a fresh branch starts from canon seed: silver key still at hall
            let mut session = WorldSession::open(
                &world.package_dir,
                &world.state_root,
                "fixture",
                "i1",
                "alt",
            )
            .unwrap();
            session.act("go to the Hall of Embers").unwrap();
            let result = session.act("take the Silver Key");
            assert!(result.is_ok());
            session.close().unwrap();
        }
        cleanup("rt6");
    }

    #[test]
    fn normalize_utterance_is_tolerant() {
        let world = TestWorld::new("rt7");
        let mut session = WorldSession::open(
            &world.package_dir,
            &world.state_root,
            "fixture",
            "i1",
            "main",
        )
        .unwrap();
        session.act("go to the Hall of Embers").unwrap();
        let result = session.act("TAKE the Silver Key!");
        assert!(result.is_ok());
        cleanup("rt7");
    }
}