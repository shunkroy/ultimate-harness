//! harness-lowlevel — capability-governed native platform access.
//!
//! Security flow (enforced shape, from the lowlevel design):
//!
//! ```text
//! request ──► capability check ──► policy ──► governor ──► adapter ──► op ──► audit
//! ```
//!
//! Rules:
//! - There is NO unrestricted interface. Every operation requires an
//!   authorized `Capability` through `Governor::authorize`.
//! - The governor records every request (authorized or denied) in the
//!   audit log; the log is JSON-exportable.
//! - The adapter layer contains only SAFE code. `unsafe_adapter`
//!   contains DESIGNED-but-not-enabled paths: the default policy does
//!   NOT include them, so the governor denies them at runtime.

pub mod adapter;
pub mod governor;
pub mod unsafe_adapter;

pub use governor::{AuditEntry, Capability, Governor, Policy, Request};