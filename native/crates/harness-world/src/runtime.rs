//! World runtime: sessions over a .hdoor package + signed store.
//!
//! Actions are deterministic offline transformations of session state.
//! Every action is recorded as a signed, chained event; canon (the
//! package) is never mutated. Close/resume round-trips must reproduce
//! byte-identical snapshots.

use crate::compiler::Entity;
use crate::knowledge::{KnowledgeEntry, KnowledgeSource, KnowledgeStore};
use crate::package::{read_package, Package, PackageError};
use crate::pipeline::{Intent, ParseError, Pipeline};
use crate::store::{WorldStore, WorldStoreError};
use harness_core::types::ProvenanceTag;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;
use std::rc::Rc;
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
    NoTopic(String),
    InvalidMode(String),
    StoryModeRequired,
    ReadOnlyWatcher,
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
            WorldSessionError::NoTopic(name) => write!(f, "nothing to ask '{}' about", name),
            WorldSessionError::InvalidMode(mode) => {
                write!(f, "'{}' is not a valid mode (story/traveller/chat/watcher/replay)", mode)
            }
            WorldSessionError::StoryModeRequired => {
                write!(f, "story advancement requires story mode (mode story)")
            }
            WorldSessionError::ReadOnlyWatcher => {
                write!(f, "watcher mode is read-only: no state-changing actions")
            }
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
    pub mode: String,
    pub story_position: i64,
}

/// All modes the runtime understands. Modes are recorded in state and
/// guard behavior; watcher is read-only, story gates chapter advancement.
pub const MODES: &[&str] = &["story", "traveller", "chat", "watcher", "replay"];

/// A character present in the current location, used for witnessing.
pub const DIALOGUE_HISTORY_CAP: usize = 20;

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

/// Seed each person's canon knowledge from facts they participate in.
/// No character is omniscient by default.
fn seed_knowledge(package: &Package) -> KnowledgeStore {
    let mut store = KnowledgeStore::default();
    for person in package
        .entities
        .iter()
        .filter(|entity| entity.kind == "person")
    {
        let seeded = store.seed_from_canon(person, &package.facts, &package.entities);
        for entry in seeded.entries {
            store.add(entry);
        }
    }
    store
}

/// Extract the mode name from "mode <name>" / "switch <name>".
fn mode_from_raw(raw: &str) -> Option<String> {
    let tokens = crate::text::tokenize(raw);
    let index = tokens
        .iter()
        .position(|token| token == "mode" || token == "switch")?;
    tokens.get(index + 1).cloned()
}

/// Extract a sanitized branch name from "branch <name>" / "fork <name>".
fn branch_name_from(raw: &str) -> Option<String> {
    let tokens = crate::text::tokenize(raw);
    let index = tokens
        .iter()
        .position(|token| token == "branch" || token == "fork")?;
    let rest = tokens.get(index + 1..)?;
    if rest.is_empty() {
        return None;
    }
    let name = rest.join("-").to_lowercase();
    let sanitized: String = name
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric() || *ch == '-')
        .collect();
    if sanitized.is_empty() {
        None
    } else {
        Some(sanitized)
    }
}

fn source_label(source: &KnowledgeSource) -> &'static str {
    match source {
        KnowledgeSource::CanonFact => "canon",
        KnowledgeSource::Witnessed => "I saw it",
        KnowledgeSource::Learned => "learned",
        KnowledgeSource::Rumored => "rumor",
        KnowledgeSource::PlayerSupplied => "you told me",
        KnowledgeSource::Inferred => "inferred",
    }
}

pub struct WorldSession {
    package: Package,
    store: WorldStore,
    branch_id: String,
    instance_id: String,
    location: String,
    inventory: Vec<String>,
    mode: String,
    story_position: i64,
    interlocutor: Option<String>,
    dialogue_history: Vec<(String, String)>,
    knowledge: KnowledgeStore,
    entities_by_id: BTreeMap<String, Entity>,
    pipeline: Pipeline<'static>,
    /// clock() returns the timestamp used for event records; injectable
    /// for deterministic replay
    clock: Rc<dyn Fn() -> String>,
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
        Self::open_with_clock(
            package_path,
            state_root,
            world_id,
            instance_id,
            branch_id,
            Rc::new(now_ms),
        )
    }

    /// Open with an explicit clock (deterministic replay entry point).
    pub fn open_with_clock(
        package_path: &Path,
        state_root: &Path,
        world_id: &str,
        instance_id: &str,
        branch_id: &str,
        clock: Rc<dyn Fn() -> String>,
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
        store.create_branch(branch_id, branch_id, None, &(clock)())?;

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
        let mode = store
            .kv_get(branch_id, "mode")?
            .unwrap_or_else(|| "traveller".to_string());
        let story_position: i64 = store
            .kv_get(branch_id, "story_position")?
            .and_then(|raw| raw.parse().ok())
            .unwrap_or(0);
        let interlocutor = store.kv_get(branch_id, "interlocutor")?;
        let dialogue_history: Vec<(String, String)> = store
            .kv_get(branch_id, "dialogue_history")?
            .and_then(|raw| serde_json::from_str(&raw).ok())
            .unwrap_or_default();
        let knowledge: KnowledgeStore = match store.kv_get(branch_id, "knowledge")? {
            Some(raw) => serde_json::from_str(&raw).unwrap_or_default(),
            None => seed_knowledge(&package),
        };

        let index = Box::leak(Box::new(package.index.clone()));
        let pipeline = Pipeline::new(index);

        Ok(WorldSession {
            package,
            store,
            branch_id: branch_id.to_string(),
            instance_id: instance_id.to_string(),
            location,
            inventory,
            mode,
            story_position,
            interlocutor,
            dialogue_history,
            knowledge,
            entities_by_id,
            pipeline,
            clock,
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
            mode: self.mode.clone(),
            story_position: self.story_position,
        })
    }

    pub fn mode(&self) -> &str {
        &self.mode
    }

    pub fn story_position(&self) -> i64 {
        self.story_position
    }

    pub fn interlocutor(&self) -> Option<&str> {
        self.interlocutor.as_deref()
    }

    pub fn knowledge(&self) -> &KnowledgeStore {
        &self.knowledge
    }

    fn persist(&self) -> Result<(), WorldSessionError> {
        self.store
            .kv_set(&self.branch_id, "location", &self.location)?;
        let inventory_json = serde_json::to_string(&self.inventory)
            .map_err(|err| WorldSessionError::Io(err.to_string()))?;
        self.store
            .kv_set(&self.branch_id, "inventory", &inventory_json)?;
        self.store.kv_set(&self.branch_id, "mode", &self.mode)?;
        self.store
            .kv_set(&self.branch_id, "story_position", &self.story_position.to_string())?;
        match &self.interlocutor {
            Some(id) => self.store.kv_set(&self.branch_id, "interlocutor", id)?,
            None => {
                let _ = self.store.kv_get(&self.branch_id, "interlocutor")?;
            }
        }
        let dialogue_json = serde_json::to_string(&self.dialogue_history)
            .map_err(|err| WorldSessionError::Io(err.to_string()))?;
        self.store
            .kv_set(&self.branch_id, "dialogue_history", &dialogue_json)?;
        let knowledge_json = serde_json::to_string(&self.knowledge)
            .map_err(|err| WorldSessionError::Io(err.to_string()))?;
        self.store
            .kv_set(&self.branch_id, "knowledge", &knowledge_json)?;
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
        let key = self
            .entities_by_id
            .get(id)
            .map(|entity| crate::text::canonical(&entity.name))
            .unwrap_or_else(|| id.to_string());
        self.package
            .facts
            .iter()
            .filter(|fact| fact.subject == key || fact.object == key)
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
        self.record_at(event_type, detail, &(self.clock)())
    }

    fn record_at(
        &mut self,
        event_type: &str,
        detail: &serde_json::Value,
        created_at: &str,
    ) -> Result<String, WorldSessionError> {
        let detail_json = serde_json::to_string(detail)
            .map_err(|err| WorldSessionError::Io(err.to_string()))?;
        let hash = self.store.append_event(
            &self.branch_id,
            event_type,
            "traveller",
            &detail_json,
            created_at,
        )?;
        self.persist()?;
        Ok(hash)
    }

    /// Story mode: advance the chapter pointer. The package is immutable;
    /// history expresses the advance as an event. No-op at the final
    /// chapter (story_position is capped at timeline length).
    fn act_advance(&mut self, parsed: &crate::pipeline::ParseResult) -> Result<ActionResult, WorldSessionError> {
        if self.mode != "story" {
            return Err(WorldSessionError::StoryModeRequired);
        }
        if self.story_position >= self.package.timeline.len() as i64 {
            return Ok(ActionResult {
                text: "The story is at its end; there is nothing further to advance to.".to_string(),
                event: None,
            });
        }
        let (chapter_seq, chapter_summary) = {
            let chapter = &self.package.timeline[self.story_position as usize];
            (chapter.seq, chapter.summary.clone())
        };
        self.story_position += 1;
        let detail = serde_json::json!({
            "chapter_seq": chapter_seq,
            "summary": chapter_summary,
            "story_position": self.story_position,
            "raw": parsed.raw,
        });
        let hash = self.record("chapter_advance", &detail)?;
        Ok(ActionResult {
            text: format!("Chapter {} — {}", chapter_seq, chapter_summary),
            event: Some(ActionEvent {
                event_type: "chapter_advance".into(),
                actor: "traveller".into(),
                detail,
                hash,
            }),
        })
    }

    /// Switch session mode. Every switch is recorded as an event; modes
    /// guard behavior (watcher = read-only, story gates advancement).
    fn act_mode(&mut self, parsed: &crate::pipeline::ParseResult) -> Result<ActionResult, WorldSessionError> {
        let mode = mode_from_raw(&parsed.raw).ok_or_else(|| {
            WorldSessionError::InvalidMode("missing mode name".to_string())
        })?;
        if !MODES.contains(&mode.as_str()) {
            return Err(WorldSessionError::InvalidMode(mode));
        }
        let old_mode = self.mode.clone();
        self.mode = mode.clone();
        let detail = serde_json::json!({
            "from": old_mode,
            "to": mode,
            "raw": parsed.raw,
        });
        let hash = self.record("mode_change", &detail)?;
        Ok(ActionResult {
            text: format!("Mode switched from {} to {}.", old_mode, mode),
            event: Some(ActionEvent {
                event_type: "mode_change".into(),
                actor: "traveller".into(),
                detail,
                hash,
            }),
        })
    }

    /// Fork the current branch at the current tip. The child resumes the
    /// parent's state (location, inventory, dialogue, knowledge, mode,
    /// story position) and chains its genesis from the parent's last
    /// hash with provenance branch_diverged. The session continues on
    /// the new branch.
    fn act_branch(&mut self, parsed: &crate::pipeline::ParseResult) -> Result<ActionResult, WorldSessionError> {
        let name = branch_name_from(&parsed.raw).ok_or_else(|| {
            WorldSessionError::Io("branch name required, e.g. 'branch what-if'".to_string())
        })?;
        if name == self.branch_id {
            return Err(WorldSessionError::Io(format!(
                "branch '{}' already exists",
                name
            )));
        }
        let created_at = (self.clock)();
        let fork = self.store.fork_branch(&name, &name, &self.branch_id, &created_at)?;
        self.store.kv_copy(&self.branch_id, &name)?;
        self.branch_id = name.clone();
        let detail = serde_json::json!({
            "from_branch": fork.parent,
            "fork_seq": fork.fork_seq,
            "fork_hash": fork.fork_hash,
            "provenance": "branch_diverged",
            "raw": parsed.raw,
        });
        // one clock tick per act: genesis and record share the timestamp
        let hash = self.record_at("branch_fork", &detail, &created_at)?;
        Ok(ActionResult {
            text: format!(
                "Forked branch '{}' from '{}' at event {}.\nThe session now continues on '{}'.",
                name, fork.parent, fork.fork_seq, name
            ),
            event: Some(ActionEvent {
                event_type: "branch_fork".into(),
                actor: "traveller".into(),
                detail,
                hash,
            }),
        })
    }

    /// Deterministic, knowledge-bounded conversation with a character:
    /// the character answers ONLY from what they know, labeled with its
    /// source. Learning is explicit (tell), witnessing is automatic for
    /// the current interlocutor, and nothing ever mutates canon.
    fn talk_response(
        &mut self,
        target: &crate::index::EntityRef,
        parsed: &crate::pipeline::ParseResult,
    ) -> Result<ActionResult, WorldSessionError> {
        let id = target.id.clone();
        let name = self.entity_name(&id);
        // plain talk without a topic: establish/keep the conversation
        if parsed.topic.is_empty() {
            let is_location_question = parsed.raw.to_lowercase().contains("where");
            if is_location_question {
                return Err(WorldSessionError::NoTopic(name));
            }
            self.interlocutor = Some(id.clone());
            self.push_dialogue("traveller", &parsed.raw);
            self.push_dialogue(&name, &format!("{} waits to hear what you ask.", name));
            let detail = serde_json::json!({
                "target": id,
                "raw": parsed.raw,
            });
            let hash = self.record("talk", &detail)?;
            return Ok(ActionResult {
                text: format!(
                    "You talk to {}.\n{} waits to hear what you ask.",
                    name, name
                ),
                event: Some(ActionEvent {
                    event_type: "talk".into(),
                    actor: "traveller".into(),
                    detail,
                    hash,
                }),
            });
        }

        let topic = parsed.topic[0].clone();
        let topic_id = topic.id.clone();
        let topic_name = self.entity_name(&topic_id);
        let teaching = parsed.raw.to_lowercase().contains("tell");

        self.interlocutor = Some(id.clone());
        self.push_dialogue("traveller", &parsed.raw);

        let known = self.knowledge.query(&id, &topic_id);
        if known.is_empty() && teaching {
            // explicit teaching: the character learns, branch-scoped
            self.knowledge.add(KnowledgeEntry {
                character_id: id.clone(),
                about_id: topic_id.clone(),
                claim: format!("the traveller told me about {}", topic_name),
                source: KnowledgeSource::PlayerSupplied,
                provenance: ProvenanceTag::BranchDiverged,
                event_seq: None,
                confidence: 1.0,
            });
            let detail = serde_json::json!({
                "target": id,
                "topic": topic_id,
                "source": "player_supplied",
                "raw": parsed.raw,
            });
            let hash = self.record("learn", &detail)?;
            let reply = format!("{} nods. \"The traveller told me about {}.\"", name, topic_name);
            self.push_dialogue(&name, &reply);
            return Ok(ActionResult {
                text: reply,
                event: Some(ActionEvent {
                    event_type: "learn".into(),
                    actor: "traveller".into(),
                    detail,
                    hash,
                }),
            });
        }

        if known.is_empty() {
            let reply = format!(
                "{} says: \"I don't know anything about {}.\"",
                name, topic_name
            );
            self.push_dialogue(&name, &reply);
            let detail = serde_json::json!({
                "target": id,
                "topic": topic_id,
                "raw": parsed.raw,
            });
            let hash = self.record("talk", &detail)?;
            return Ok(ActionResult {
                text: reply,
                event: Some(ActionEvent {
                    event_type: "talk".into(),
                    actor: "traveller".into(),
                    detail,
                    hash,
                }),
            });
        }

        // location question: claims referencing a place, else the canon
        // seed location of the object (objects_by_location is compile-
        // time canon data — grounded, never invented)
        let is_location_question = parsed.raw.to_lowercase().contains("where");
        let mut lines = Vec::new();
        let mut answered = false;
        for entry in &known {
            if is_location_question && self.kind(&entry.about_id) != "place" {
                let claim_mentions_place = self.package.entities.iter().any(|entity| {
                    entity.kind == "place"
                        && entry
                            .claim
                            .to_lowercase()
                            .contains(&entity.name.to_lowercase())
                });
                if !claim_mentions_place {
                    continue;
                }
            }
            lines.push(format!(
                "{} says: \"{}\" (I know this: {})",
                name,
                entry.claim,
                source_label(&entry.source)
            ));
            answered = true;
        }
        if !answered && is_location_question {
            if let Some(place_id) = self
                .package
                .seed
                .objects_by_location
                .iter()
                .find(|(_, objects)| objects.contains(&topic_id))
                .map(|(place_id, _)| place_id.clone())
            {
                let place_name = self.entity_name(&place_id);
                let reply = format!(
                    "{} says: \"I know the {} is in the {}.\"",
                    name, topic_name, place_name
                );
                self.push_dialogue(&name, &reply);
                let detail = serde_json::json!({
                    "target": id,
                    "topic": topic_id,
                    "answer": "seed_location",
                    "raw": parsed.raw,
                });
                let hash = self.record("talk", &detail)?;
                return Ok(ActionResult {
                    text: reply,
                    event: Some(ActionEvent {
                        event_type: "talk".into(),
                        actor: "traveller".into(),
                        detail,
                        hash,
                    }),
                });
            }
        }
        let text = if answered {
            lines.join("\n")
        } else {
            format!("{} says: \"I don't know where {} is.\"", name, topic_name)
        };
        self.push_dialogue(&name, &text);
        let detail = serde_json::json!({
            "target": id,
            "topic": topic_id,
            "raw": parsed.raw,
        });
        let hash = self.record("talk", &detail)?;
        Ok(ActionResult {
            text,
            event: Some(ActionEvent {
                event_type: "talk".into(),
                actor: "traveller".into(),
                detail,
                hash,
            }),
        })
    }

    /// The current interlocutor witnesses the traveller's actions.
    /// Witnessed knowledge is branch-scoped; it never touches canon.
    fn witness(&mut self, action: &str, object_id: &str) {
        let Some(interlocutor) = self.interlocutor.clone() else {
            return;
        };
        let object_name = self.entity_name(object_id);
        let claim = format!("the traveller {} {}", action, object_name);
        self.knowledge.add(KnowledgeEntry {
            character_id: interlocutor,
            about_id: object_id.to_string(),
            claim,
            source: KnowledgeSource::Witnessed,
            provenance: ProvenanceTag::BranchDiverged,
            event_seq: None,
            confidence: 0.9,
        });
    }

    fn push_dialogue(&mut self, speaker: &str, text: &str) {
        self.dialogue_history.push((speaker.to_string(), text.to_string()));
        while self.dialogue_history.len() > DIALOGUE_HISTORY_CAP {
            self.dialogue_history.remove(0);
        }
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
                    "mode": self.mode,
                    "story_position": self.story_position,
                    "raw": parsed.raw,
                });
                let hash = self.record("status", &detail)?;
                let mut lines = vec![
                    format!("You are at {}.", self.entity_name(&self.location)),
                    format!(
                        "Here: {}",
                        if objects_here.is_empty() {
                            "nothing".to_string()
                        } else {
                            objects_here.join(", ")
                        }
                    ),
                    format!(
                        "You hold: {}",
                        if self.inventory.is_empty() {
                            "nothing".to_string()
                        } else {
                            self.inventory
                                .iter()
                                .map(|id| self.entity_name(id))
                                .collect::<Vec<_>>()
                                .join(", ")
                        }
                    ),
                    format!("Mode: {}", self.mode),
                ];
                if self.mode == "story" {
                    lines.push(format!(
                        "Story: chapter {}/{}",
                        self.story_position,
                        self.package.timeline.len()
                    ));
                }
                if let Some(interlocutor) = &self.interlocutor {
                    lines.push(format!(
                        "Speaking with: {}",
                        self.entity_name(interlocutor)
                    ));
                }
                lines.push(format!(
                    "Events: {}",
                    self.store
                        .verify_chain(&self.branch_id)
                        .map(|count| count.to_string())
                        .unwrap_or_else(|_| "?".to_string())
                ));
                return Ok(ActionResult {
                    text: lines.join("\n"),
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
                    text: "Commands: inspect/take/drop/go/talk/use/open/close/give/status/help | advance | mode <story|traveller|chat|watcher|replay> | branch <name>"
                        .to_string(),
                    event: None,
                });
            }
            _ => {}
        }

        // watcher mode is read-only (mode switching is the escape hatch)
        if self.mode == "watcher" {
            match parsed.intent {
                Intent::Take
                | Intent::Drop
                | Intent::Go
                | Intent::Use
                | Intent::Open
                | Intent::Close
                | Intent::Give
                | Intent::Advance
                | Intent::Branch => {
                    return Err(WorldSessionError::ReadOnlyWatcher);
                }
                _ => {}
            }
        }

        // story/mode/branch operate on session state, not entities
        match parsed.intent {
            Intent::Advance => return self.act_advance(&parsed),
            Intent::Mode => return self.act_mode(&parsed),
            Intent::Branch => return self.act_branch(&parsed),
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
                self.witness("took", &id);
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
                self.witness("dropped", &id);
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
                self.talk_response(&target, &parsed)
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
            Intent::Status | Intent::Help | Intent::Unknown | Intent::Advance | Intent::Mode
            | Intent::Branch => {
                Err(WorldSessionError::Parse(ParseError::UnknownIntent(
                    utterance.to_string(),
                )))
            }
        }
    }

    /// Renderer-neutral signed export of this branch's history.
    pub fn export(&self) -> Result<crate::export::WorldExport, WorldSessionError> {
        let canon_hash = format!("sha256:{}", self.package.canon_source_hash);
        let state = crate::export::ExportState {
            location: self.location.clone(),
            inventory: self.inventory.clone(),
            mode: self.mode.clone(),
            story_position: self.story_position,
            interlocutor: self.interlocutor.clone(),
            knowledge_entries: self.knowledge.entries.len(),
            knowledge: self.knowledge.clone(),
        };
        crate::export::export_branch(
            &self.store,
            &self.package.manifest,
            &canon_hash,
            &canon_hash,
            &self.instance_id,
            &self.branch_id,
            state,
            &(self.clock)(),
        )
        .map_err(|err| WorldSessionError::Io(err.to_string()))
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