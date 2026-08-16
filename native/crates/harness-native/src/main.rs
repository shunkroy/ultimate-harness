//! harness-native — native Harness binary.
//!
//! Real commands backed by real functionality:
//!   harness-native status          read-only canonical state snapshot
//!   harness-native session list    read-only session listing
//!   harness-native runtime list    truthful runtime discovery
//!   harness-native engine demo     deterministic engine demo (no network)
//!   harness-native world compile   deterministic .hdoor package build
//!   harness-native world open      offline world session (act/close/resume)
//!   harness-native world list      enumerate world instances/branches

use harness_core::engine::DeterministicArithmeticEngine;
use harness_core::runtime::detect_runtimes;
use harness_core::stateroot::resolve_state_root;
use harness_core::status::{list_sessions, read_state};
use harness_core::types::{RunRequest, RunOutcome};
use harness_core::{Engine, Envelope};
use harness_world::compiler::Compiler;
use harness_world::package::{read_package, write_package};
use harness_world::runtime::{SessionSnapshot, WorldSession};
use harness_world::store::WorldStore;
use std::path::{Path, PathBuf};
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

// ---------------------------------------------------------------------------
// world subcommands (Milestone 2)
// ---------------------------------------------------------------------------

fn cmd_world_compile(source: &str, world_id: &str, title: &str, out: &str) -> Result<(), String> {
    let source_text = std::fs::read_to_string(source).map_err(|err| err.to_string())?;
    let source_name = PathBuf::from(source)
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| "source.txt".to_string());
    let world = Compiler::new().compile(world_id, title, &source_name, &source_text);
    let out_dir = PathBuf::from(out);
    write_package(&out_dir, &world).map_err(|err| err.to_string())?;
    emit(&Envelope {
        schema: harness_core::current_schema(),
        kind: "world_compiled".to_string(),
        payload: serde_json::json!({
            "package": out_dir.to_string_lossy(),
            "manifest": world.manifest,
            "entities": world.entities.len(),
            "locations": world.locations.len(),
            "facts": world.facts.len(),
            "timeline_events": world.timeline.len(),
            "seed": world.seed,
        }),
    });
    Ok(())
}

/// world_id from the package manifest (single source of truth).
fn world_id_from_package(package_dir: &str) -> Result<String, String> {
    let package = harness_world::read_package(Path::new(package_dir)).map_err(|err| err.to_string())?;
    Ok(package.manifest.world_id)
}

fn cmd_world_open(
    package_dir: &str,
    state_root: &str,
    instance: &str,
    branch: &str,
    acts: &[String],
) -> Result<(), String> {
    let root = PathBuf::from(state_root);
    // world_id comes from the package manifest — the store path follows the
    // package identity, not the CLI flags
    let package = read_package(Path::new(package_dir)).map_err(|err| err.to_string())?;
    let world_id = package.manifest.world_id.clone();
    let mut session = WorldSession::open(
        Path::new(package_dir),
        &root,
        &world_id,
        instance,
        branch,
    )
    .map_err(|err| err.to_string())?;
    for act in acts {
        match session.act(act) {
            Ok(result) => emit(&Envelope {
                schema: harness_core::current_schema(),
                kind: "world_action".to_string(),
                payload: serde_json::json!({
                    "utterance": act,
                    "text": result.text,
                    "event": result.event,
                }),
            }),
            Err(err) => emit(&Envelope {
                schema: harness_core::current_schema(),
                kind: "world_action_error".to_string(),
                payload: serde_json::json!({
                    "utterance": act,
                    "error": err.to_string(),
                }),
            }),
        }
    }
    let snapshot: SessionSnapshot = session.snapshot().map_err(|err| err.to_string())?;
    session.close().map_err(|err| err.to_string())?;
    emit(&Envelope {
        schema: harness_core::current_schema(),
        kind: "world_session_snapshot".to_string(),
        payload: snapshot,
    });
    Ok(())
}

fn cmd_world_export(
    package_dir: &str,
    state_root: &str,
    instance_id: &str,
    branch_id: &str,
    out_path: &str,
) -> Result<(), String> {
    let session = WorldSession::open(
        Path::new(package_dir),
        Path::new(state_root),
        &world_id_from_package(package_dir)?,
        instance_id,
        branch_id,
    )
    .map_err(|err| err.to_string())?;
    let export = session.export().map_err(|err| err.to_string())?;
    session.close().map_err(|err| err.to_string())?;
    let verified = harness_world::export::verify_export(&export).map_err(|err| err.to_string())?;
    let json = serde_json::to_string_pretty(&export).map_err(|err| err.to_string())?;
    std::fs::write(out_path, json).map_err(|err| err.to_string())?;
    emit(&Envelope {
        schema: harness_core::current_schema(),
        kind: "world_export".to_string(),
        payload: serde_json::json!({
            "branch": branch_id,
            "events": verified,
            "schema": export.schema,
            "out": out_path,
        }),
    });
    Ok(())
}

fn cmd_world_list(state_root: &str) -> Result<(), String> {
    let root = PathBuf::from(state_root).join("worlds");
    let mut instances: Vec<serde_json::Value> = Vec::new();
    if root.exists() {
        for world_entry in std::fs::read_dir(&root).map_err(|err| err.to_string())? {
            let world_entry = world_entry.map_err(|err| err.to_string())?;
            if !world_entry.path().is_dir() {
                continue;
            }
            let world_id = world_entry.file_name().to_string_lossy().to_string();
            for instance_entry in
                std::fs::read_dir(world_entry.path()).map_err(|err| err.to_string())?
            {
                let instance_entry = instance_entry.map_err(|err| err.to_string())?;
                let instance_dir = instance_entry.path();
                if !instance_dir.is_dir() {
                    continue;
                }
                let instance_id = instance_entry.file_name().to_string_lossy().to_string();
                let db_path = instance_dir.join("world.db");
                let mut branch_count = 0usize;
                let mut event_count = 0i64;
                if db_path.exists() {
                    match WorldStore::open(&root, &world_id, &instance_id) {
                        Ok(store) => {
                            branch_count = store.branch_count().unwrap_or(0);
                            event_count = store.event_count().unwrap_or(0);
                        }
                        Err(_) => {}
                    }
                }
                instances.push(serde_json::json!({
                    "world_id": world_id,
                    "instance_id": instance_id,
                    "branches": branch_count,
                    "events": event_count,
                    "path": instance_dir.to_string_lossy(),
                }));
            }
        }
    }
    emit(&Envelope {
        schema: harness_core::current_schema(),
        kind: "world_instance_list".to_string(),
        payload: serde_json::json!({"instances": instances, "count": instances.len()}),
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
         \x20 harness-native world compile <source.txt> --id <world_id> --title <title> --out <dir>\n\
         \x20 harness-native world open <package-dir> --root <state_root> --instance <id> --branch <name> [--act \"<utterance>\"]...\n\
         \x20 harness-native world list --root <state_root>\n\
         \x20 harness-native --version"
    );
    std::process::exit(2);
}

fn flag_value(args: &[String], flag: &str) -> String {
    let mut idx = 0;
    while idx < args.len() {
        if args[idx] == flag {
            return args.get(idx + 1).cloned().unwrap_or_default();
        }
        idx += 1;
    }
    String::new()
}

fn cmd_world_compile_cli(args: &[String]) -> Result<(), String> {
    let source = args
        .iter()
        .find(|a| !a.starts_with("--"))
        .cloned()
        .ok_or_else(|| "world compile requires <source.txt>".to_string())?;
    let world_id = flag_value(args, "--id");
    let title = flag_value(args, "--title");
    let out = flag_value(args, "--out");
    if out.is_empty() {
        return Err("world compile requires --out <dir>".to_string());
    }
    cmd_world_compile(&source, &world_id, &title, &out)
}

fn cmd_world_open_cli(args: &[String]) -> Result<(), String> {
    let package_dir = args
        .iter()
        .find(|a| !a.starts_with("--"))
        .cloned()
        .ok_or_else(|| "world open requires <package-dir>".to_string())?;
    let root = flag_value(args, "--root");
    let instance = flag_value(args, "--instance");
    let branch = flag_value(args, "--branch");
    if root.is_empty() || instance.is_empty() || branch.is_empty() {
        return Err(
            "world open requires --root <state_root> --instance <id> --branch <name>".to_string(),
        );
    }
    let mut acts: Vec<String> = Vec::new();
    let mut idx = 0;
    while idx < args.len() {
        if args[idx] == "--act" {
            if let Some(act) = args.get(idx + 1) {
                acts.push(act.clone());
            }
        }
        idx += 1;
    }
    cmd_world_open(&package_dir, &root, &instance, &branch, &acts)
}

fn cmd_world_list_cli(args: &[String]) -> Result<(), String> {
    let root = flag_value(args, "--root");
    if root.is_empty() {
        return Err("world list requires --root <state_root>".to_string());
    }
    cmd_world_list(&root)
}

fn cmd_world_export_cli(args: &[String]) -> Result<(), String> {
    let package_dir = args
        .iter()
        .find(|a| !a.starts_with("--"))
        .cloned()
        .ok_or_else(|| "world export requires <package-dir>".to_string())?;
    let root = flag_value(args, "--root");
    let instance = flag_value(args, "--instance");
    let branch = flag_value(args, "--branch");
    let out = flag_value(args, "--out");
    if root.is_empty() || instance.is_empty() || branch.is_empty() || out.is_empty() {
        return Err(
            "world export requires --root <state_root> --instance <id> --branch <name> --out <file.json>"
                .to_string(),
        );
    }
    cmd_world_export(&package_dir, &root, &instance, &branch, &out)
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
        "world" if args.len() >= 2 && args[1] == "compile" => {
            cmd_world_compile_cli(&args[2..])
        }
        "world" if args.len() >= 2 && args[1] == "open" => {
            cmd_world_open_cli(&args[2..])
        }
        "world" if args.len() >= 2 && args[1] == "export" => {
            cmd_world_export_cli(&args[2..])
        }
        "world" if args.len() >= 2 && args[1] == "list" => {
            cmd_world_list_cli(&args[2..])
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