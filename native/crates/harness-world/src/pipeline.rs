//! Offline lexical natural-language pipeline.
//!
//! No cloud, no model, no network: normalization + tokenization +
//! verb->intent mapping + entity resolution. Errors are structured and
//! fail closed (ambiguity and unknown intent are explicit, never
//! guessed).

use crate::compiler::Index;
use crate::index::{resolve_all, EntityRef, ResolveError};
use crate::text::{canonical, normalize, tokenize};
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Intent {
    Inspect,
    Take,
    Drop,
    Go,
    Talk,
    Use,
    Open,
    Close,
    Give,
    Status,
    Help,
    Unknown,
}

#[derive(Debug, Clone, PartialEq)]
pub enum ParseError {
    Empty,
    UnknownIntent(String),
    NoTarget,
    AmbiguousTarget(ResolveError),
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ParseError::Empty => write!(f, "empty utterance"),
            ParseError::UnknownIntent(raw) => write!(f, "no intent recognized in '{}'", raw),
            ParseError::NoTarget => write!(f, "no target entity found"),
            ParseError::AmbiguousTarget(err) => write!(f, "{}", err),
        }
    }
}

impl std::error::Error for ParseError {}

#[derive(Debug, Clone, PartialEq)]
pub struct ParseResult {
    pub intent: Intent,
    pub targets: Vec<EntityRef>,
    pub raw: String,
}

fn intent_for_verb(verb: &str) -> Option<Intent> {
    match verb {
        "look" | "looks" | "inspect" | "inspects" | "examine" | "examines" | "search"
        | "searches" | "check" | "checks" | "view" | "see" => Some(Intent::Inspect),
        "take" | "takes" | "grab" | "grabs" | "pick" | "picks" | "collect" | "collects"
        | "lift" => Some(Intent::Take),
        "drop" | "drops" | "put" | "puts" | "place" | "places" => Some(Intent::Drop),
        "go" | "goes" | "walk" | "walks" | "move" | "moves" | "travel" | "travels"
        | "enter" | "enters" | "head" | "heads" => Some(Intent::Go),
        "talk" | "talks" | "speak" | "speaks" | "ask" | "asks" | "tell" | "tells"
        | "greet" | "greets" => Some(Intent::Talk),
        "use" | "uses" | "wield" | "wields" | "drink" | "drinks" | "read" | "reads" => {
            Some(Intent::Use)
        }
        "open" | "opens" | "unlock" | "unlocks" => Some(Intent::Open),
        "close" | "closes" | "shut" | "shuts" | "lock" | "locks" => Some(Intent::Close),
        "give" | "gives" | "hand" | "hands" | "offer" | "offers" => Some(Intent::Give),
        "status" | "inventory" | "inv" | "state" | "where" | "whereami" => Some(Intent::Status),
        "help" | "commands" | "?" | "what" => Some(Intent::Help),
        _ => None,
    }
}

const STOP_WORDS: &[&str] = &[
    "the", "a", "an", "to", "at", "in", "on", "of", "with", "my", "me", "it", "this", "that",
    "please", "now", "then", "there", "here", "for", "and", "i", "am", "is", "can", "could",
];

/// Split a phrase into candidate target spans (longest-first), so
/// "the bronze key" beats "bronze".
fn candidate_phrases(utterance: &str) -> Vec<String> {
    let tokens = tokenize(utterance);
    let mut phrases = Vec::new();
    for start in 0..tokens.len() {
        for end in (start + 1..=tokens.len()).rev() {
            let span = &tokens[start..end];
            let filtered: Vec<&String> = span
                .iter()
                .filter(|t| !STOP_WORDS.contains(&t.as_str()))
                .collect();
            if filtered.is_empty() {
                continue;
            }
            let joined = filtered
                .iter()
                .map(|t| t.as_str())
                .collect::<Vec<&str>>()
                .join(" ");
            let phrase = canonical(&joined);
            if !phrase.is_empty() && !phrases.contains(&phrase) {
                phrases.push(phrase);
            }
        }
    }
    phrases
}

pub struct Pipeline<'a> {
    index: &'a Index,
}

impl<'a> Pipeline<'a> {
    pub fn new(index: &'a Index) -> Self {
        Pipeline { index }
    }

    pub fn parse(&self, utterance: &str) -> Result<ParseResult, ParseError> {
        let normalized = normalize(utterance);
        if normalized.is_empty() {
            return Err(ParseError::Empty);
        }
        let tokens = tokenize(&normalized);

        // 1. intent via first meaningful verb
        let mut intent = Intent::Unknown;
        for token in &tokens {
            if let Some(candidate) = intent_for_verb(token) {
                intent = candidate;
                break;
            }
        }
        if intent == Intent::Unknown {
            return Err(ParseError::UnknownIntent(utterance.to_string()));
        }

        // status and help need no target
        if matches!(intent, Intent::Status | Intent::Help) {
            return Ok(ParseResult {
                intent,
                targets: Vec::new(),
                raw: utterance.to_string(),
            });
        }

        // 2. target entity via longest resolvable phrase — the longest
        //    unambiguous phrase pins the target; shorter ambiguous
        //    phrases are ignored once a longer one resolved. Ambiguity
        //    only fails closed when NO longer phrase resolved.
        let mut targets: Vec<EntityRef> = Vec::new();
        let mut seen: Vec<String> = Vec::new();
        for phrase in candidate_phrases(&normalized) {
            if seen.contains(&phrase) {
                continue;
            }
            seen.push(phrase.clone());
            let mut matches = resolve_all(self.index, &phrase);
            if matches.is_empty() {
                continue;
            }
            for m in &mut matches {
                m.matched_alias = phrase.clone();
            }
            if matches.len() > 1 {
                if targets.is_empty() {
                    return Err(ParseError::AmbiguousTarget(ResolveError::Ambiguous {
                        query: phrase.clone(),
                        candidates: matches,
                    }));
                }
                continue; // a longer phrase already pinned the target
            }
            targets.extend(matches);
        }
        // de-dup by id
        let mut unique: Vec<EntityRef> = Vec::new();
        for target in targets {
            if !unique.iter().any(|u| u.id == target.id) {
                unique.push(target);
            }
        }
        if unique.is_empty() {
            return Err(ParseError::NoTarget);
        }

        Ok(ParseResult {
            intent,
            targets: unique,
            raw: utterance.to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compiler::Compiler;

    const FIXTURE: &str = "The Hollow Keep\n\n## The Keeper\n\nKeeper Sarn guards the Hollow Keep. The Silver Key is hidden in the Hall of Embers. Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n## The Garden of Ash\n\nThe Garden of Ash lies beyond the Iron Gate. The Deep Well stands at the center of the Garden of Ash.";

    fn pipeline_with() -> Pipeline<'static> {
        let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        let index: &'static Index = Box::leak(Box::new(world.index));
        Pipeline::new(index)
    }

    #[test]
    fn parses_take_intent() {
        let pipeline = pipeline_with();
        let result = pipeline.parse("take the bronze key").unwrap();
        assert_eq!(result.intent, Intent::Take);
        assert!(result.targets.iter().any(|t| t.id == "bronze-key"));
    }

    #[test]
    fn parses_go_intent() {
        let pipeline = pipeline_with();
        let result = pipeline.parse("go to the Garden of Ash").unwrap();
        assert_eq!(result.intent, Intent::Go);
        assert!(result.targets.iter().any(|t| t.id == "garden-of-ash"));
    }

    #[test]
    fn ambiguous_target_is_explicit() {
        let pipeline = pipeline_with();
        let result = pipeline.parse("take the key");
        assert!(matches!(
            result,
            Err(ParseError::AmbiguousTarget(ResolveError::Ambiguous { .. }))
        ));
    }

    #[test]
    fn unknown_intent_fails_closed() {
        let pipeline = pipeline_with();
        let result = pipeline.parse("banana the moon");
        assert!(matches!(result, Err(ParseError::UnknownIntent(_))));
    }

    #[test]
    fn empty_utterance_fails() {
        let pipeline = pipeline_with();
        assert!(matches!(pipeline.parse("   "), Err(ParseError::Empty)));
    }

    #[test]
    fn typo_tolerant_parse() {
        let pipeline = pipeline_with();
        let result = pipeline.parse("take the bronz key").unwrap();
        assert!(result.targets.iter().any(|t| t.id == "bronze-key"));
    }
}