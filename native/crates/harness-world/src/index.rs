//! Entity resolution against a compiled world index.
//!
//! Exact first, alias second, fuzzy last. Ambiguity is a first-class
//! result, never silently resolved — the traveller must disambiguate.

use crate::compiler::Index;
use crate::text::{canonical, levenshtein};
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EntityRef {
    pub id: String,
    pub name: String,
    pub matched_alias: String,
    pub confidence: f64,
    pub via_fuzzy: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub enum ResolveError {
    NotFound(String),
    Ambiguous {
        query: String,
        candidates: Vec<EntityRef>,
    },
}

impl std::fmt::Display for ResolveError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ResolveError::NotFound(query) => write!(f, "nothing matches '{}'", query),
            ResolveError::Ambiguous { query, candidates } => {
                write!(
                    f,
                    "'{}' is ambiguous: {}",
                    query,
                    candidates
                        .iter()
                        .map(|c| c.name.clone())
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            }
        }
    }
}

impl std::error::Error for ResolveError {}

/// Resolve a user phrase to entity ids.
/// Returns all matching ids (may be several => ambiguous).
pub fn resolve_all(index: &Index, phrase: &str) -> Vec<EntityRef> {
    let query = canonical(phrase);
    if query.is_empty() {
        return Vec::new();
    }

    // 1. exact name
    if let Some(id) = index.names.get(&query) {
        return vec![EntityRef {
            id: id.clone(),
            name: id.clone(),
            matched_alias: query.clone(),
            confidence: 1.0,
            via_fuzzy: false,
        }];
    }

    // 2. exact alias (may be ambiguous)
    if let Some(ids) = index.aliases.get(&query) {
        return ids
            .iter()
            .map(|id| EntityRef {
                id: id.clone(),
                name: id.clone(),
                matched_alias: query.clone(),
                confidence: 0.95,
                via_fuzzy: false,
            })
            .collect();
    }

    // 3. fuzzy over full canonical names only (typo tolerance for real
//    names; short alias words must never fuzzy-match — "deep" is not
//    "keep")
    let mut fuzzy: Vec<EntityRef> = Vec::new();
    for (name, id) in &index.names {
        let distance = levenshtein(&query, name);
        let threshold = (name.chars().count() / 4).max(1);
        if distance <= threshold {
            fuzzy.push(EntityRef {
                id: id.clone(),
                name: name.clone(),
                matched_alias: name.clone(),
                confidence: 0.8 - (distance as f64 * 0.1),
                via_fuzzy: true,
            });
        }
    }
    fuzzy.sort_by(|a, b| {
        b.confidence
            .partial_cmp(&a.confidence)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    fuzzy.dedup_by(|a, b| a.id == b.id);
    fuzzy
}

/// Resolve to exactly one entity or fail closed.
pub fn resolve_unique(index: &Index, phrase: &str) -> Result<EntityRef, ResolveError> {
    let matches = resolve_all(index, phrase);
    match matches.len() {
        0 => Err(ResolveError::NotFound(phrase.to_string())),
        1 => Ok(matches.into_iter().next().unwrap()),
        _ => Err(ResolveError::Ambiguous {
            query: phrase.to_string(),
            candidates: matches,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compiler::Compiler;

    const FIXTURE: &str = "The Hollow Keep\n\n## The Keeper\n\nKeeper Sarn guards the Hollow Keep. The Silver Key is hidden in the Hall of Embers. Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n## The Garden of Ash\n\nThe Garden of Ash lies beyond the Iron Gate. The Deep Well stands at the center of the Garden of Ash.";

    fn fixture_index() -> Index {
        Compiler::new()
            .compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE)
            .index
    }

    #[test]
    fn exact_name_resolves() {
        let index = fixture_index();
        let resolved = resolve_unique(&index, "Keeper Sarn").unwrap();
        assert_eq!(resolved.id, "keeper-sarn");
        assert!(!resolved.via_fuzzy);
    }

    #[test]
    fn ambiguous_alias_fails_closed() {
        let index = fixture_index();
        let result = resolve_unique(&index, "the key");
        assert!(matches!(result, Err(ResolveError::Ambiguous { .. })));
    }

    #[test]
    fn fuzzy_typo_resolves() {
        let index = fixture_index();
        let resolved = resolve_unique(&index, "Keeper Sarnn").unwrap();
        assert_eq!(resolved.id, "keeper-sarn");
        assert!(resolved.via_fuzzy);
    }

    #[test]
    fn unknown_phrase_not_found() {
        let index = fixture_index();
        let result = resolve_unique(&index, "the flying spaghetti");
        assert!(matches!(result, Err(ResolveError::NotFound(_))));
    }
}