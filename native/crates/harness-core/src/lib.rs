//! harness-core — canonical language-neutral Harness core (native foundation).
//!
//! Harness owns the semantics; languages implement capabilities. This
//! crate is the Rust implementation of the canonical v1 object model,
//! engine contract, runtime discovery, deterministic state-root
//! resolution and read-only interop with the existing Python state.
//!
//! Design laws honored here:
//! - a model does not own session truth (read-only interop);
//! - a language does not own Harness semantics (versioned JSON envelopes);
//! - loaded content is not authority (capability/engine contracts are
//!   explicit; nothing here executes prompts);
//! - preservation beats duplication (state is read in place, never copied).

pub mod engine;
pub mod runtime;
pub mod schema;
pub mod stateroot;
pub mod status;
pub mod types;

pub use engine::{DeterministicArithmeticEngine, Engine};
pub use schema::{current_schema, envelope, Envelope};
pub use stateroot::resolve_state_root;
pub use status::{list_sessions, read_state, StateSnapshot, StatusError};
pub use types::*;