//! Engine contract and the first real deterministic engine.
//!
//! "Engine" does not mean LLM. Harness supports deterministic, rule,
//! state-machine, retrieval, dialogue, story, world, simulation, dream,
//! procedural, local-model and remote-AI engines behind one contract.
//! An external AI API is never mandatory for operation.

use crate::types::{EngineKind, EngineStatus, RunOutcome, RunRequest};
use std::time::Instant;

pub trait Engine {
    fn name(&self) -> &str;
    fn kind(&self) -> EngineKind;
    fn status(&self) -> EngineStatus;
    fn run(&self, request: &RunRequest) -> RunOutcome;
}

// ---------------------------------------------------------------------------
// Deterministic arithmetic engine
// ---------------------------------------------------------------------------

/// A real, working deterministic engine: parses simple arithmetic from
/// natural phrasing and evaluates it exactly. No network, no model.
/// This is the proving implementation of the Engine contract.
pub struct DeterministicArithmeticEngine {
    name: String,
}

impl Default for DeterministicArithmeticEngine {
    fn default() -> Self {
        Self {
            name: "deterministic-arithmetic".into(),
        }
    }
}

fn parse_number(token: &str) -> Option<i64> {
    let cleaned: String = token.chars().filter(|c| c.is_ascii_digit() || *c == '-').collect();
    if cleaned.is_empty() {
        return None;
    }
    cleaned.parse::<i64>().ok()
}

fn normalize_prompt(prompt: &str) -> String {
    prompt
        .to_lowercase()
        .trim()
        .trim_start_matches("what is")
        .trim_start_matches("what's")
        .trim_start_matches("compute")
        .trim_start_matches("calculate")
        .trim_start_matches("evaluate")
        .trim_end_matches('?')
        .trim_end_matches('.')
        .trim()
        .replace(" plus ", "+")
        .replace(" minus ", "-")
        .replace(" times ", "*")
        .replace(" multiplied by ", "*")
        .replace(" divided by ", "/")
        .replace(" divided by", "/")
        .replace("x", "*")
        .replace("×", "*")
        .replace("÷", "/")
        .replace(" ", "")
        .to_string()
}

/// Evaluate a normalized arithmetic expression with exactly two operands
/// and one operator. Returns None when it is not arithmetic.
fn eval_expression(expr: &str) -> Option<i64> {
    let (a, op, b) = if let Some(idx) = expr.find('+') {
        (&expr[..idx], '+', &expr[idx + 1..])
    } else if let Some(idx) = expr.rfind('-') {
        // rfind so negative second operands still parse: 5--2
        (&expr[..idx], '-', &expr[idx + 1..])
    } else if let Some(idx) = expr.find('*') {
        (&expr[..idx], '*', &expr[idx + 1..])
    } else if let Some(idx) = expr.find('/') {
        (&expr[..idx], '/', &expr[idx + 1..])
    } else {
        return None;
    };
    if a.is_empty() || b.is_empty() {
        return None;
    }
    let left = parse_number(a)?;
    let right = parse_number(b)?;
    match op {
        '+' => Some(left + right),
        '-' => Some(left - right),
        '*' => Some(left * right),
        '/' => {
            if right == 0 {
                None
            } else if left % right == 0 {
                Some(left / right)
            } else {
                None // exact integer division only; fractional results are not yet supported
            }
        }
        _ => None,
    }
}

impl Engine for DeterministicArithmeticEngine {
    fn name(&self) -> &str {
        &self.name
    }

    fn kind(&self) -> EngineKind {
        EngineKind::Deterministic
    }

    fn status(&self) -> EngineStatus {
        EngineStatus {
            name: self.name.clone(),
            kind: self.kind(),
            available: true,
            detail: Some("local deterministic arithmetic rules; no network, no model".into()),
        }
    }

    fn run(&self, request: &RunRequest) -> RunOutcome {
        let started = Instant::now();
        let expr = normalize_prompt(&request.prompt);
        match eval_expression(&expr) {
            Some(value) => RunOutcome {
                success: true,
                text: Some(value.to_string()),
                error: None,
                error_code: None,
                engine: self.name.clone(),
                duration_ms: started.elapsed().as_secs_f64() * 1000.0,
                metadata: serde_json::json!({"expression": expr}),
            },
            None => RunOutcome {
                success: false,
                text: None,
                error: Some(format!("not arithmetic: {:?}", request.prompt)),
                error_code: Some("unparsed".into()),
                engine: self.name.clone(),
                duration_ms: started.elapsed().as_secs_f64() * 1000.0,
                metadata: serde_json::json!({"expression": expr}),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(prompt: &str) -> RunOutcome {
        DeterministicArithmeticEngine::default().run(&RunRequest {
            prompt: prompt.into(),
            engine: None,
            model: None,
            timeout_secs: None,
        })
    }

    #[test]
    fn engine_name_and_kind_are_deterministic() {
        let engine = DeterministicArithmeticEngine::default();
        assert_eq!(engine.name(), "deterministic-arithmetic");
        assert_eq!(engine.kind(), EngineKind::Deterministic);
        assert!(engine.status().available);
    }

    #[test]
    fn computes_plain_arithmetic() {
        assert_eq!(run("2+2").text.as_deref(), Some("4"));
        assert_eq!(run("12 * 3").text.as_deref(), Some("36"));
        assert_eq!(run("20 / 4").text.as_deref(), Some("5"));
        assert_eq!(run("9-4").text.as_deref(), Some("5"));
    }

    #[test]
    fn computes_natural_phrasing() {
        assert_eq!(run("What is 12 times 3?").text.as_deref(), Some("36"));
        assert_eq!(run("5 plus 7").text.as_deref(), Some("12"));
        assert_eq!(run("100 divided by 10").text.as_deref(), Some("10"));
    }

    #[test]
    fn non_arithmetic_fails_cleanly() {
        let outcome = run("hello there");
        assert!(!outcome.success);
        assert_eq!(outcome.error_code.as_deref(), Some("unparsed"));
    }

    #[test]
    fn division_by_zero_fails_cleanly() {
        let outcome = run("5 / 0");
        assert!(!outcome.success);
    }
}