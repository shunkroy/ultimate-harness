//! Knowledge boundaries — characters are NOT omniscient.
//!
//! Every piece of knowledge a character holds carries a source:
//!   CanonFact      — facts present in canon where the character is a
//!                    participant (subject or object).
//!   Witnessed      — events observed at runtime in the character's
//!                    location.
//!   Learned        — explicit runtime learning events.
//!   Rumored        — secondhand information (mechanism reserved;
//!                    propagation is DESIGNED, not implemented).
//!   PlayerSupplied — the traveller/player told the character.
//!   Inferred       — derived from knowledge the character already holds
//!                    (reserved; not implemented in M3).
//!
//! Knowledge is runtime state: stored branch-scoped, created via events,
//! and NEVER written into the immutable canon package.

use crate::compiler::Entity;
use crate::text::canonical;
use harness_core::types::ProvenanceTag;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeSource {
    CanonFact,
    Witnessed,
    Learned,
    Rumored,
    PlayerSupplied,
    Inferred,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct KnowledgeEntry {
    /// character entity id holding the knowledge
    pub character_id: String,
    /// entity id the knowledge is about
    pub about_id: String,
    /// deterministic claim text (canon fact: "<subject> <predicate> <object>")
    pub claim: String,
    pub source: KnowledgeSource,
    /// provenance of the claim itself — canon facts are CANON, runtime
    /// learning lives on the BRANCH
    pub provenance: ProvenanceTag,
    /// event seq that created this knowledge, if runtime-derived
    pub event_seq: Option<i64>,
    pub confidence: f64,
}

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq)]
pub struct KnowledgeStore {
    pub entries: Vec<KnowledgeEntry>,
}

impl KnowledgeStore {
    /// Seed a character's canon knowledge: facts where the character is a
    /// participant. Nobody automatically knows facts they are not part of.
    ///
    /// Facts are keyed by canonical names ("keeper sarn"), entities by
    /// slug ids ("keeper-sarn") — both sides are matched canonically.
    pub fn seed_from_canon(
        &self,
        character: &Entity,
        facts: &[crate::compiler::Fact],
        entities: &[Entity],
    ) -> KnowledgeStore {
        let character_key = canonical(&character.name);
        let mut entries: Vec<KnowledgeEntry> = Vec::new();
        for fact in facts {
            let (about_key, role) = if fact.subject == character_key {
                (fact.object.clone(), Role::Subject)
            } else if fact.object == character_key {
                (fact.subject.clone(), Role::Object)
            } else {
                continue;
            };
            let _ = role;
            let subject_name = entity_name(entities, &fact.subject);
            let object_name = entity_name(entities, &fact.object);
            entries.push(KnowledgeEntry {
                character_id: character.id.clone(),
                about_id: about_key,
                claim: format!("{} {} {}", subject_name, fact.predicate, object_name),
                source: KnowledgeSource::CanonFact,
                provenance: ProvenanceTag::Canon,
                event_seq: None,
                confidence: 1.0,
            });
        }
        KnowledgeStore { entries }
    }

    pub fn add(&mut self, entry: KnowledgeEntry) {
        // no duplicate claims for the same character+about+source
        if !self.entries.iter().any(|existing| {
            existing.character_id == entry.character_id
                && existing.about_id == entry.about_id
                && existing.claim == entry.claim
                && existing.source == entry.source
        }) {
            self.entries.push(entry);
        }
    }

    /// Everything character `character_id` knows about `about_id`
    /// (canonical entity key or slug id both work).
    pub fn query(&self, character_id: &str, about_id: &str) -> Vec<&KnowledgeEntry> {
        let needle = canonical(about_id);
        self.entries
            .iter()
            .filter(|entry| {
                entry.character_id == character_id
                    && (entry.about_id == needle || canonical(&entry.about_id) == needle)
            })
            .collect()
    }

    /// Everything character `character_id` knows, period.
    pub fn query_character(&self, character_id: &str) -> Vec<&KnowledgeEntry> {
        self.entries
            .iter()
            .filter(|entry| entry.character_id == character_id)
            .collect()
    }
}

enum Role {
    Subject,
    Object,
}

fn entity_name(entities: &[Entity], key: &str) -> String {
    entities
        .iter()
        .find(|entity| entity.id == key || canonical(&entity.name) == key)
        .map(|entity| entity.name.clone())
        .unwrap_or_else(|| key.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compiler::Compiler;

    const FIXTURE: &str = "The Hollow Keep\n\n## The Keeper\n\nKeeper Sarn guards the Hollow Keep. The Silver Key is hidden in the Hall of Embers. Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n## The Garden of Ash\n\nThe Garden of Ash lies beyond the Iron Gate. The Deep Well stands at the center of the Garden of Ash.";

    fn compiled() -> crate::compiler::CompiledWorld {
        Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE)
    }

    #[test]
    fn canon_seed_is_participant_only() {
        let world = compiled();
        let sarn = world.entities.iter().find(|e| e.id == "keeper-sarn").unwrap();
        let store = KnowledgeStore::default().seed_from_canon(sarn, &world.facts, &world.entities);
        let knowledge = store.query_character("keeper-sarn");
        // Sarn knows the facts he participates in
        assert!(
            knowledge
                .iter()
                .any(|entry| entry.about_id == "hollow keep"),
            "{knowledge:?}"
        );
        assert!(
            knowledge
                .iter()
                .any(|entry| entry.about_id == "bronze key"),
            "{knowledge:?}"
        );
        // Sarn does NOT know about the silver key — he is not in that fact
        assert!(store.query("keeper-sarn", "silver-key").is_empty());
        for entry in knowledge {
            assert_eq!(entry.source, KnowledgeSource::CanonFact);
            assert_eq!(entry.provenance, ProvenanceTag::Canon);
        }
    }

    #[test]
    fn runtime_learning_creates_branch_entries() {
        let world = compiled();
        let sarn = world.entities.iter().find(|e| e.id == "keeper-sarn").unwrap();
        let mut store = KnowledgeStore::default().seed_from_canon(sarn, &world.facts, &world.entities);
        store.add(KnowledgeEntry {
            character_id: "keeper-sarn".into(),
            about_id: "silver-key".into(),
            claim: "the traveller took the silver key".into(),
            source: KnowledgeSource::Witnessed,
            provenance: ProvenanceTag::BranchDiverged,
            event_seq: Some(7),
            confidence: 0.9,
        });
        let knowledge = store.query("keeper-sarn", "silver-key");
        assert_eq!(knowledge.len(), 1);
        assert_eq!(knowledge[0].source, KnowledgeSource::Witnessed);
        assert_eq!(knowledge[0].provenance, ProvenanceTag::BranchDiverged);
        // duplicates are not added
        store.add(KnowledgeEntry {
            character_id: "keeper-sarn".into(),
            about_id: "silver-key".into(),
            claim: "the traveller took the silver key".into(),
            source: KnowledgeSource::Witnessed,
            provenance: ProvenanceTag::BranchDiverged,
            event_seq: Some(8),
            confidence: 0.9,
        });
        assert_eq!(store.query("keeper-sarn", "silver-key").len(), 1);
    }

    #[test]
    fn no_omniscience_by_default() {
        let world = compiled();
        // The traveller entity (if it existed) knows nothing until it
        // participates; a character with zero facts knows nothing.
        let sarn = world.entities.iter().find(|e| e.id == "keeper-sarn").unwrap();
        let store = KnowledgeStore::default().seed_from_canon(sarn, &world.facts, &world.entities);
        assert!(store.query("keeper-sarn", "iron-gate").is_empty());
        assert!(store.query("keeper-sarn", "garden-of-ash").is_empty());
    }
}