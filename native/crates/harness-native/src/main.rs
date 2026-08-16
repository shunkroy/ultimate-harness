//! harness-native — native Harness binary.
//!
//! Real commands backed by real functionality:
//!   harness-native status          read-only canonical state snapshot
//!   harness-native session list    read-only session listing
//!   harness-native runtime list    truthful runtime discovery
//!   harness-native engine demo     deterministic engine demo (no network)

use harness_core::engine::DeterministicArithmeticEngine;
use harness_core::runtime::detect_runtimes;
use harness_core::stateroot::resolve_state_root;
use harness_core::status::{list_sessions, read_state};
use harness_core::types::{RunRequest, RunOutcome};
use harness_core::{Engine, Envelope};
use std::process::ExitCode;

const VERSION: &str = env!("CARGO_PKG_VERSION");

fn emit<T: serde::Serialize>(value: &T) {
    println!("{}", serde_json::to_string_pretty(value).unwrap_or_else(|_| "{}".into()));
}

fn cmd_status() -> Result<(), String> {
    let root = resolve_state_root().map_err(|err| err.to_string())?;
    let snapshot = read_state(&root).map_err(|err| err.to_string())?;
    emit(&Envelope {
        schema: harness_core::current_schema(),
        kind: "state_snapshot".to_string(),
        payload: snapshot,
    });
    Ok(())
}

fn cmd_session_list(limit: i64) -> Result<(), String> {
    let root = resolve_state_root().map_err(|err| err.to_string())?;
    let sessions = list_sessions(&root, limit).map_err(|err| err.to_string())?;
    emit(&Envelope {
        schema: harness_core::current_schema(),
        kind: "session_list".to_string(),
        payload: serde_json::json!({"sessions": sessions, "count": sessions.len()}),
    });
    Ok(())
}

fn cmd_runtime_list() -> Result<(), String> {
    let runtimes = detect_runtimes();
    emit(&Envelope {
        schema: harness_core::current_schema(),
        kind: "runtime_list".to_string(),
        payload: serde_json::json!({"runtimes": runtimes}),
    });
    Ok(())
}

fn cmd_engine_demo(prompt: &str) -> Result<(), String> {
    let engine = DeterministicArithmeticEngine::default();
    let outcome: RunOutcome = engine.run(&RunRequest {
        prompt: prompt.to_string(),
        engine: None,
        model: None,
        timeout_secs: None,
    });
    emit(&Envelope {
        schema: harness_core::current_schema(),
        kind: "run_outcome".to_string(),
        payload: outcome,
    });
    Ok(())
}

fn usage() -> ! {
    eprintln!(
        "harness-native {VERSION} — native Harness foundation\n\
         \n\
         USAGE:\n\
         \x20 harness-native status\n\
         \x20 harness-native session list [limit]\n\
         \x20 harness-native runtime list\n\
         \x20 harness-native engine demo \"<prompt>\"\n\
         \x20 harness-native --version"
    );
    std::process::exit(2);
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        usage();
    }
    let result = match args[0].as_str() {
        "--version" | "-V" => {
            println!("harness-native {VERSION}");
            Ok(())
        }
        "status" => cmd_status(),
        "session" if args.len() >= 2 && args[1] == "list" => {
            let limit = args.get(2).and_then(|v| v.parse().ok()).unwrap_or(50);
            cmd_session_list(limit)
        }
        "runtime" if args.len() >= 2 && args[1] == "list" => cmd_runtime_list(),
        "engine" if args.len() >= 3 && args[1] == "demo" => {
            cmd_engine_demo(&args[2..].join(" "))
        }
        _ => usage(),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("harness-native error: {message}");
            ExitCode::from(1)
        }
    }
}