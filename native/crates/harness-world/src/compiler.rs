//! Deterministic source compiler / indexer.
//!
//! Turns plain source text into structured world material. Every entry
//! traces back to its source reference, and every derived entry carries
//! an explicit provenance tag: entities present in the source are CANON;
//! kinds, locations, facts, timeline structure and seed placement are
//! INFERRED. Inference is never silently promoted to canon.
//!
//! Determinism: identical input produces byte-identical output (no
//! timestamps, no randomness, no hash-map iteration order).

use crate::manifest::{
    BranchPolicy, CanonCounts, CompilerInfo, HDoorManifest, HDOR_SCHEMA_VERSION, PACKAGE_KIND,
    SourceFile, SourceIdentity,
};
use crate::text::{canonical, normalize};
use harness_core::types::ProvenanceTag;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub const COMPILER_NAME: &str = "harness-world-compiler";
pub const COMPILER_VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Entity {
    pub id: String,
    pub name: String,
    pub aliases: Vec<String>,
    /// kind inference is a heuristic; existence in source is canon.
    pub kind: String,
    pub source_ref: String,
    pub tag: ProvenanceTag,
    pub confidence: f64,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Location {
    pub id: String,
    pub name: String,
    pub aliases: Vec<String>,
    pub source_ref: String,
    pub tag: ProvenanceTag,
    pub confidence: f64,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct TimelineEvent {
    pub seq: usize,
    pub chapter: String,
    pub summary: String,
    pub source_ref: String,
    pub tag: ProvenanceTag,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Fact {
    pub subject: String,
    pub predicate: String,
    pub object: String,
    pub source_ref: String,
    pub tag: ProvenanceTag,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Index {
    /// canonical name -> entity id (entities + locations)
    pub names: BTreeMap<String, String>,
    /// canonical alias -> entity ids (may be ambiguous)
    pub aliases: BTreeMap<String, Vec<String>>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SeedState {
    pub traveller_start: String,
    /// location id -> object entity ids present at compile time
    pub objects_by_location: BTreeMap<String, Vec<String>>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CompiledWorld {
    pub manifest: HDoorManifest,
    pub entities: Vec<Entity>,
    pub locations: Vec<Location>,
    pub timeline: Vec<TimelineEvent>,
    pub facts: Vec<Fact>,
    pub index: Index,
    pub seed: SeedState,
    /// Verbatim source text (canon carrier for the package).
    pub source_text: String,
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn slugify(name: &str) -> String {
    let mut slug = String::new();
    for ch in canonical(name).chars() {
        if ch.is_alphanumeric() {
            slug.push(ch);
        } else if !slug.ends_with('-') && !slug.is_empty() {
            slug.push('-');
        }
    }
    slug.trim_matches('-').to_string()
}

const PLACE_SUFFIXES: &[&str] = &[
    "keep", "castle", "city", "village", "town", "forest", "tomb", "guild", "hall", "garden",
    "well", "tower", "dungeon", "kingdom", "room", "gate", "bridge", "mountain", "cave", "lake",
    "river", "temple", "cathedral", "inn", "tavern", "library", "workshop", "lair", "hollow",
    "embers", "ash", "vale", "field", "plain", "desert", "sea", "coast", "harbor", "street",
    "square", "market", "palace", "fortress", "citadel", "sanctum",
];
const OBJECT_SUFFIXES: &[&str] = &[
    "staff", "sword", "key", "ring", "weapon", "scroll", "coin", "armor", "shield", "bow",
    "dagger", "book", "letter", "box", "chest", "crown", "goblet", "amulet", "orb", "blade",
    "spear", "hammer", "lantern", "map", "potion", "gem", "mirror", "helmet", "cloak", "boots",
    "gloves", "banner", "bell", "candle", "compass", "quill", "wax", "seal",
];
const PERSON_MARKERS: &[&str] = &["sama", "san", "sir", "lord", "lady", "keeper", "king", "queen"];
const ACTION_VERBS: &[&str] = &[
    "keeps", "holds", "guards", "rules", "lives", "stands", "sits", "carries", "gives", "takes",
    "protects", "watches", "sleeps", "walks", "enters", "opens", "closes", "finds", "uses",
    "hides", "commands", "wields", "owns", "presides",
];
/// Interior connectors glue capitalized words ("Hall of Embers").
const INTERIOR_CONNECTORS: &[&str] = &["of", "and", "the", "a", "an"];
/// Leading connectors may open a phrase ("the Deep Well").
const LEADING_CONNECTORS: &[&str] = &["the", "a", "an"];
/// Prepositional enders terminate a phrase ("in", "at", ...).
const PHRASE_ENDERS: &[&str] = &[
    "in", "at", "into", "from", "to", "with", "on", "beyond", "inside", "outside", "near",
    "under", "behind", "above", "below", "across", "through", "toward", "towards", "by", "for",
];

struct Chapter {
    title: String,
    paragraphs: Vec<Vec<String>>, // sentences per paragraph
}

fn split_sentences(text: &str) -> Vec<String> {
    let mut sentences = Vec::new();
    let mut current = String::new();
    let chars: Vec<char> = text.chars().collect();
    let mut idx = 0;
    while idx < chars.len() {
        let ch = chars[idx];
        current.push(ch);
        if matches!(ch, '.' | '!' | '?' | '…') {
            let is_end = idx + 1 >= chars.len()
                || chars[idx + 1].is_whitespace()
                || chars[idx + 1] == '"'
                || chars[idx + 1] == '“'
                || chars[idx + 1] == '”';
            if is_end {
                let sentence = current.trim().to_string();
                if !sentence.is_empty() {
                    sentences.push(sentence);
                }
                current.clear();
            }
        }
        idx += 1;
    }
    if !current.trim().is_empty() {
        sentences.push(current.trim().to_string());
    }
    sentences
}

fn split_chapters(source: &str) -> Vec<Chapter> {
    let mut chapters: Vec<Chapter> = Vec::new();
    let mut current_title = String::from("body");
    let mut body = String::new();
    for line in source.lines() {
        let trimmed = line.trim();
        let lower = normalize(trimmed);
        let is_chapter_heading = lower.starts_with("chapter ")
            || lower.starts_with("prologue")
            || lower.starts_with("epilogue")
            || trimmed.starts_with("## ")
            || trimmed.starts_with("# ");
        if is_chapter_heading && !body.trim().is_empty() {
            let paragraphs: Vec<Vec<String>> = body
                .split("\n\n")
                .filter(|p| !p.trim().is_empty())
                .map(|p| split_sentences(p))
                .filter(|p| !p.is_empty())
                .collect();
            chapters.push(Chapter {
                title: current_title,
                paragraphs,
            });
            current_title = trimmed
                .trim_start_matches('#')
                .trim()
                .trim_end_matches(':')
                .to_string();
            body.clear();
        } else {
            body.push_str(trimmed);
            body.push('\n');
        }
    }
    let paragraphs: Vec<Vec<String>> = body
        .split("\n\n")
        .filter(|p| !p.trim().is_empty())
        .map(|p| split_sentences(p))
        .filter(|p| !p.is_empty())
        .collect();
    chapters.push(Chapter {
        title: current_title,
        paragraphs,
    });
    chapters
}

/// Raw (case-preserving, punctuation-trimmed) tokens of a sentence.
fn raw_tokens(sentence: &str) -> Vec<String> {
    sentence
        .split_whitespace()
        .map(|token| {
            token
                .trim_matches(|ch: char| !ch.is_alphanumeric() && ch != '\'' && ch != '’')
                .to_string()
        })
        .filter(|token| !token.is_empty())
        .collect()
}

fn is_capitalized(token: &str) -> bool {
    matches!(token.chars().next(), Some(ch) if ch.is_uppercase())
}

fn is_leading_connector(token: &str) -> bool {
    LEADING_CONNECTORS.contains(&token.to_lowercase().as_str())
}

fn is_interior_connector(token: &str) -> bool {
    INTERIOR_CONNECTORS.contains(&token.to_lowercase().as_str())
}

fn is_ender(token: &str) -> bool {
    PHRASE_ENDERS.contains(&token.to_lowercase().as_str())
}

/// Extract the maximal phrase starting at `start` in raw tokens.
/// Returns (raw_phrase, canonical_key) or None.
fn take_phrase(tokens: &[String], start: usize) -> Option<(String, String)> {
    let mut end = start;
    let mut seen_cap = false;
    while end < tokens.len() {
        let token = &tokens[end];
        let lower = token.to_lowercase();
        if is_ender(&lower) {
            break;
        }
        if is_capitalized(token) {
            seen_cap = true;
            end += 1;
        } else if is_interior_connector(&lower) {
            let at_start = end == start;
            let next_cap = end + 1 < tokens.len() && is_capitalized(&tokens[end + 1]);
            if seen_cap && next_cap {
                // interior connector between capitalized words: "Hall of Embers"
                end += 1;
            } else if at_start && is_leading_connector(&lower) && next_cap {
                // leading connector opening a phrase: "the Deep Well"
                end += 1;
            } else {
                break;
            }
        } else {
            break;
        }
    }
    if end == start {
        return None;
    }
    // strip leading connectors that were not followed by a capitalized word
    let mut phrase_tokens: Vec<&String> = tokens[start..end].iter().collect();
    while let Some(first) = phrase_tokens.first() {
        if is_leading_connector(first) {
            // keep leading connector only if it precedes a capitalized word
            if phrase_tokens.len() >= 2 && is_capitalized(phrase_tokens[1]) {
                break;
            }
            phrase_tokens.remove(0);
        } else {
            break;
        }
    }
    if phrase_tokens.is_empty() {
        return None;
    }
    // interior connectors must sit between capitalized words; the phrase
    // head (index 0) is always valid
    let mut kept: Vec<&String> = Vec::new();
    for (i, token) in phrase_tokens.iter().enumerate() {
        let lower = token.to_lowercase();
        if i > 0 && is_interior_connector(&lower) {
            let has_prev_cap = is_capitalized(phrase_tokens[i - 1]);
            let has_next_cap = i + 1 < phrase_tokens.len() && is_capitalized(phrase_tokens[i + 1]);
            if !(has_prev_cap && has_next_cap) {
                continue; // drop orphan connector
            }
        }
        kept.push(token);
    }
    if kept.is_empty() {
        return None;
    }
    let raw = kept.iter().map(|t| t.as_str()).collect::<Vec<_>>().join(" ");
    let key = canonical(&raw);
    if key.is_empty() {
        return None;
    }
    Some((raw, key))
}

pub struct Compiler;

impl Compiler {
    pub fn new() -> Self {
        Compiler
    }

    pub fn compile(
        &self,
        world_id: &str,
        title: &str,
        source_name: &str,
        source_text: &str,
    ) -> CompiledWorld {
        let source_hash = sha256_hex(source_text.as_bytes());
        let identity = format!("sha256:{}", source_hash);
        let chapters = split_chapters(source_text);

        // ---- pass 1: count phrase occurrences for sentence-start filtering
        let mut phrase_counts: BTreeMap<String, usize> = BTreeMap::new();
        for chapter in &chapters {
            for paragraph in &chapter.paragraphs {
                for sentence in paragraph {
                    let tokens = raw_tokens(sentence);
                    for idx in 0..tokens.len() {
                        if let Some((_raw, key)) = take_phrase(&tokens, idx) {
                            *phrase_counts.entry(key).or_insert(0) += 1;
                        }
                    }
                }
            }
        }

        // ---- pass 2: extract entities with source references
        let mut entities: BTreeMap<String, Entity> = BTreeMap::new();
        let mut locations: BTreeMap<String, Location> = BTreeMap::new();
        let mut location_order: Vec<String> = Vec::new();
        let mut possessive_aliases: Vec<(String, String)> = Vec::new();
        let mut object_hints: Vec<String> = Vec::new();
        let mut order: Vec<String> = Vec::new(); // first-appearance order (canonical keys)

        let mut chapter_idx = 0usize;
        for chapter in &chapters {
            chapter_idx += 1;
            for (p_idx, paragraph) in chapter.paragraphs.iter().enumerate() {
                for (s_idx, sentence) in paragraph.iter().enumerate() {
                    let source_ref = format!(
                        "{}:chapter:{}:paragraph:{}:sentence:{}",
                        source_name, chapter_idx, p_idx + 1, s_idx + 1
                    );
                    let tokens = raw_tokens(sentence);
                    let mut idx = 0;
                    while idx < tokens.len() {
                        // possessive: "Keeper Sarn's staff" -> staff + "staff of keeper sarn"
                        if tokens[idx].ends_with("'s") || tokens[idx].ends_with('’') {
                            let owner_raw = tokens[..idx]
                                .iter()
                                .rev()
                                .take(4)
                                .filter(|t| is_capitalized(t))
                                .collect::<Vec<_>>();
                            if !owner_raw.is_empty() {
                                let owner = owner_raw
                                    .iter()
                                    .rev()
                                    .map(|t| t.as_str())
                                    .collect::<Vec<_>>()
                                    .join(" ");
                                let owner_key = canonical(&owner);
                                if idx + 1 < tokens.len() && is_capitalized(&tokens[idx + 1]) {
                                    if let Some((owned_raw, owned_key)) =
                                        take_phrase(&tokens, idx + 1)
                                    {
                                        let alias = format!("{} of {}", owned_raw, owner);
                                        possessive_aliases.push((owned_key.clone(), alias.clone()));
                                        if !entities.contains_key(&owned_key) {
                                            order.push(owned_key.clone());
                                            let id = slugify(&owned_raw);
                                            entities.insert(
                                                owned_key.clone(),
                                                Entity {
                                                    id,
                                                    name: owned_raw.clone(),
                                                    aliases: vec![owned_raw.clone(), alias],
                                                    kind: "object".into(),
                                                    source_ref: source_ref.clone(),
                                                    tag: ProvenanceTag::Canon,
                                                    confidence: 1.0,
                                                },
                                            );
                                            object_hints.push(owned_key);
                                        }
                                        idx += 2 + owned_raw.split(' ').count();
                                        continue;
                                    }
                                }
                                let _ = owner_key;
                            }
                            idx += 1;
                            continue;
                        }
                        if let Some((raw, key)) = take_phrase(&tokens, idx) {
                            let count = phrase_counts.get(&key).copied().unwrap_or(0);
                            let at_sentence_start = idx == 0;
                            if (!at_sentence_start || count >= 2) && count >= 1 {
                                if !entities.contains_key(&key) {
                                    order.push(key.clone());
                                    let kind = infer_kind(&key, &tokens, idx);
                                    let id = slugify(&raw);
                                    entities.insert(
                                        key.clone(),
                                        Entity {
                                            id,
                                            name: raw.clone(),
                                            aliases: vec![raw.clone(), key.clone()],
                                            kind: kind.clone(),
                                            source_ref: source_ref.clone(),
                                            tag: ProvenanceTag::Canon,
                                            confidence: 1.0,
                                        },
                                    );
                                    if kind == "place" && !locations.contains_key(&key) {
                                        location_order.push(key.clone());
                                        locations.insert(
                                            key.clone(),
                                            Location {
                                                id: slugify(&raw),
                                                name: raw.clone(),
                                                aliases: vec![raw.clone(), key.clone()],
                                                source_ref: source_ref.clone(),
                                                tag: ProvenanceTag::Inferred,
                                                confidence: 0.7,
                                            },
                                        );
                                    }
                                    if kind == "object" {
                                        object_hints.push(key.clone());
                                    }
                                }
                                idx += raw.split(' ').count().max(1);
                                continue;
                            }
                        }
                        idx += 1;
                    }
                }
            }
        }

        // ---- facts: subject-verb-object triples (INFERRED)
        let mut facts: Vec<Fact> = Vec::new();
        let mut chapter_idx = 0usize;
        for chapter in &chapters {
            chapter_idx += 1;
            for (p_idx, paragraph) in chapter.paragraphs.iter().enumerate() {
                for (s_idx, sentence) in paragraph.iter().enumerate() {
                    let source_ref = format!(
                        "{}:chapter:{}:paragraph:{}:sentence:{}",
                        source_name, chapter_idx, p_idx + 1, s_idx + 1
                    );
                    let tokens = raw_tokens(sentence);
                    for (idx, token) in tokens.iter().enumerate() {
                        if ACTION_VERBS.contains(&token.to_lowercase().as_str()) && idx > 0 {
                            if let (Some((subject_raw, _)), Some((object_raw, _))) =
                                (take_phrase(&tokens, 0), take_phrase(&tokens, idx + 1))
                            {
                                let subject = canonical(&subject_raw);
                                let object = canonical(&object_raw);
                                if entities.contains_key(&subject) && !object.is_empty() {
                                    facts.push(Fact {
                                        subject,
                                        predicate: token.to_lowercase(),
                                        object,
                                        source_ref: source_ref.clone(),
                                        tag: ProvenanceTag::Inferred,
                                    });
                                }
                            }
                        }
                    }
                }
            }
        }

        // ---- timeline: chapter structure (INFERRED)
        let mut timeline: Vec<TimelineEvent> = Vec::new();
        for (idx, chapter) in chapters.iter().enumerate() {
            let summary = chapter
                .paragraphs
                .iter()
                .flatten()
                .next()
                .cloned()
                .unwrap_or_else(|| chapter.title.clone());
            timeline.push(TimelineEvent {
                seq: idx + 1,
                chapter: chapter.title.clone(),
                summary,
                source_ref: format!("{}:chapter:{}", source_name, idx + 1),
                tag: ProvenanceTag::Inferred,
            });
        }

        // ---- seed state: place objects at mentioned locations
        let mut objects_by_location: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut placed: BTreeMap<String, bool> = BTreeMap::new();
        for chapter in &chapters {
            for paragraph in &chapter.paragraphs {
                for sentence in paragraph {
                    let tokens = raw_tokens(sentence);
                    let mut mentioned: Vec<String> = Vec::new();
                    for idx in 0..tokens.len() {
                        if let Some((_raw, key)) = take_phrase(&tokens, idx) {
                            if entities.contains_key(&key) && !mentioned.contains(&key) {
                                mentioned.push(key);
                            }
                        }
                    }
                    for object in &mentioned {
                        if object_hints.contains(object) {
                            let object_id = entities[object].id.clone();
                            for other in &mentioned {
                                if locations.contains_key(other) && other != object {
                                    let location_id = locations[other].id.clone();
                                    if !objects_by_location
                                        .get(&location_id)
                                        .map(|list| list.contains(&object_id))
                                        .unwrap_or(false)
                                    {
                                        objects_by_location
                                            .entry(location_id.clone())
                                            .or_default()
                                            .push(object_id.clone());
                                    }
                                    placed.insert(object.clone(), true);
                                }
                            }
                        }
                    }
                }
            }
        }
        let traveller_start = location_order
            .first()
            .map(|key| locations[key].id.clone())
            .unwrap_or_else(|| "world".into());
        let unplaced: Vec<String> = object_hints
            .iter()
            .filter(|o| !placed.contains_key(*o))
            .map(|o| entities[o].id.clone())
            .collect();
        if !unplaced.is_empty() {
            objects_by_location
                .entry(traveller_start.clone())
                .or_default()
                .extend(unplaced);
        }

        // ---- index
        let mut names: BTreeMap<String, String> = BTreeMap::new();
        let mut aliases: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for (key, entity) in &entities {
            names.insert(key.clone(), entity.id.clone());
            for alias in &entity.aliases {
                let canonical_alias = canonical(alias);
                if !aliases
                    .get(&canonical_alias)
                    .map(|ids| ids.contains(&entity.id))
                    .unwrap_or(false)
                {
                    aliases
                        .entry(canonical_alias)
                        .or_default()
                        .push(entity.id.clone());
                }
            }
            // last-word alias enables natural reference and ambiguity detection
            let last_word = key.split(' ').last().unwrap_or(key).to_string();
            if last_word.chars().count() >= 3 {
                if !aliases
                    .get(&last_word)
                    .map(|ids| ids.contains(&entity.id))
                    .unwrap_or(false)
                {
                    aliases
                        .entry(last_word)
                        .or_default()
                        .push(entity.id.clone());
                }
            }
        }
        for (key, location) in &locations {
            names.insert(key.clone(), location.id.clone());
            for alias in &location.aliases {
                let canonical_alias = canonical(alias);
                if !aliases
                    .get(&canonical_alias)
                    .map(|ids| ids.contains(&location.id))
                    .unwrap_or(false)
                {
                    aliases
                        .entry(canonical_alias)
                        .or_default()
                        .push(location.id.clone());
                }
            }
        }
        for (owned, alias) in possessive_aliases {
            if let Some(id) = entities.get(&owned).map(|e| e.id.clone()) {
                let canonical_alias = canonical(&alias);
                if !aliases
                    .get(&canonical_alias)
                    .map(|ids| ids.contains(&id))
                    .unwrap_or(false)
                {
                    aliases
                        .entry(canonical_alias)
                        .or_default()
                        .push(id);
                }
            }
        }

        let entities_vec: Vec<Entity> = order
            .iter()
            .filter_map(|key| entities.get(key).cloned())
            .collect();
        let locations_vec: Vec<Location> = location_order
            .iter()
            .filter_map(|key| locations.get(key).cloned())
            .collect();

        let manifest = HDoorManifest {
            schema_version: HDOR_SCHEMA_VERSION,
            world_id: world_id.to_string(),
            title: title.to_string(),
            package_kind: PACKAGE_KIND.to_string(),
            compiler: CompilerInfo {
                name: COMPILER_NAME.to_string(),
                version: COMPILER_VERSION.to_string(),
            },
            source: SourceIdentity {
                identity,
                files: vec![SourceFile {
                    name: source_name.to_string(),
                    sha256: source_hash,
                    bytes: source_text.len() as u64,
                }],
            },
            index_version: 1,
            canon: CanonCounts {
                entities: entities_vec.len(),
                locations: locations_vec.len(),
                facts: facts.len(),
                timeline_events: timeline.len(),
            },
            modes: vec![
                "story".into(),
                "chat".into(),
                "watcher".into(),
                "traveller".into(),
                "replay".into(),
            ],
            capabilities: vec![
                "offline_lexical_v1".into(),
                "deterministic_actions_v1".into(),
                "signed_branch_events_v1".into(),
            ],
            branches: BranchPolicy {
                supported: true,
                immutable_canon: true,
            },
        };

        CompiledWorld {
            manifest,
            entities: entities_vec,
            locations: locations_vec,
            timeline,
            facts,
            index: Index { names, aliases },
            seed: SeedState {
                traveller_start,
                objects_by_location,
            },
            source_text: source_text.to_string(),
        }
    }
}

fn infer_kind(key: &str, sentence_tokens: &[String], position: usize) -> String {
    if OBJECT_SUFFIXES.iter().any(|suffix| key.ends_with(suffix)) {
        return "object".into();
    }
    if PLACE_SUFFIXES.iter().any(|suffix| key.ends_with(suffix)) {
        return "place".into();
    }
    if PERSON_MARKERS.iter().any(|marker| key.contains(marker)) {
        return "person".into();
    }
    // prepositional location hint: "in/at/into/from the X" shortly before
    let window_start = position.saturating_sub(4);
    let window: Vec<&str> = sentence_tokens[window_start..position.min(sentence_tokens.len())]
        .iter()
        .map(String::as_str)
        .collect();
    if window
        .iter()
        .any(|t| matches!(*t, "in" | "at" | "into" | "from" | "inside" | "outside"))
    {
        return "place".into();
    }
    "other".into()
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = "The Hollow Keep\n\n## The Keeper\n\nKeeper Sarn guards the Hollow Keep. The Silver Key is hidden in the Hall of Embers. Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n## The Garden of Ash\n\nThe Garden of Ash lies beyond the Iron Gate. The Deep Well stands at the center of the Garden of Ash.";

    #[test]
    fn compile_is_deterministic() {
        let compiler = Compiler::new();
        let first = compiler.compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        let second = compiler.compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        assert_eq!(
            serde_json::to_string(&first).unwrap(),
            serde_json::to_string(&second).unwrap()
        );
    }

    #[test]
    fn compiles_entities_locations_objects() {
        let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        let names: Vec<String> = world
            .entities
            .iter()
            .map(|e| canonical(&e.name))
            .collect();
        assert!(names.contains(&"keeper sarn".to_string()), "{names:?}");
        assert!(names.contains(&"silver key".to_string()), "{names:?}");
        assert!(names.contains(&"bronze key".to_string()), "{names:?}");
        assert!(names.contains(&"hollow keep".to_string()), "{names:?}");
        let location_names: Vec<String> = world
            .locations
            .iter()
            .map(|l| canonical(&l.name))
            .collect();
        assert!(location_names.contains(&"hall of embers".to_string()), "{location_names:?}");
        assert!(location_names.contains(&"deep well".to_string()), "{location_names:?}");
        assert!(location_names.contains(&"garden of ash".to_string()), "{location_names:?}");
        assert!(location_names.contains(&"iron gate".to_string()), "{location_names:?}");
    }

    #[test]
    fn provenance_tags_are_explicit() {
        let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        for entity in &world.entities {
            assert_eq!(entity.tag, ProvenanceTag::Canon);
            assert!(!entity.source_ref.is_empty());
        }
        assert!(!world.facts.is_empty());
        for fact in &world.facts {
            assert_eq!(fact.tag, ProvenanceTag::Inferred);
            assert!(!fact.source_ref.is_empty());
        }
    }

    #[test]
    fn source_identity_is_recorded_and_deterministic() {
        let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        let expected = format!("sha256:{}", sha256_hex(FIXTURE.as_bytes()));
        assert_eq!(world.manifest.source.identity, expected);
        assert_eq!(world.manifest.source.files[0].bytes, FIXTURE.len() as u64);
    }

    #[test]
    fn seed_places_objects_at_mentioned_locations() {
        let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        let hall = world
            .locations
            .iter()
            .find(|l| canonical(&l.name) == "hall of embers")
            .unwrap();
        let objects = world.seed.objects_by_location.get(&hall.id).unwrap();
        assert!(objects.contains(&"silver-key".to_string()), "{objects:?}");
        let well = world
            .locations
            .iter()
            .find(|l| canonical(&l.name) == "deep well")
            .unwrap();
        let objects = world.seed.objects_by_location.get(&well.id).unwrap();
        assert!(objects.contains(&"bronze-key".to_string()), "{objects:?}");
    }

    #[test]
    fn index_supports_alias_and_ambiguity() {
        let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        // last-word alias "key" is ambiguous between silver and bronze
        let key_alias = world.index.aliases.get("key").unwrap();
        assert_eq!(key_alias.len(), 2, "{key_alias:?}");
        // full name resolves uniquely
        let silver = world.index.names.get("silver key").unwrap();
        assert_eq!(silver, "silver-key");
    }

    #[test]
    fn traveller_starts_at_first_mentioned_location() {
        let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        assert_eq!(world.seed.traveller_start, "hollow-keep");
    }
}