//! harness-world — offline world vertical slice.
//!
//! Real architecture, not a demo: a versioned portable world package
//! (.hdoor V1), a deterministic source compiler with exact provenance,
//! an offline lexical natural-language pipeline (no cloud, no model),
//! and a world runtime that persists signed branch events and resumes
//! with identical state.
//!
//! World-neutral by law: Overlord is a proving dataset, never a
//! hard-coded assumption. The same machinery must tolerate any novel,
//! original world, simulation, game, visual novel, graphic novel,
//! historical world, sci-fi, fantasy, mixed genre or future format.

pub mod compiler;
pub mod export;
pub mod index;
pub mod knowledge;
pub mod manifest;
pub mod package;
pub mod pipeline;
pub mod replay;
pub mod runtime;
pub mod store;
pub mod text;

pub use compiler::Compiler;
pub use export::{verify_export, WorldExport};
pub use knowledge::{KnowledgeEntry, KnowledgeSource, KnowledgeStore};
pub use manifest::{CompilerInfo, HDoorManifest, SourceFile, SourceIdentity};
pub use package::{read_package, validate_package, write_package, Package, PackageError};
pub use pipeline::{Intent, ParseError, ParseResult, Pipeline};
pub use replay::{replay, ReplayCommand, ReplayOutcome};
pub use runtime::{WorldSession, WorldSessionError};
pub use store::{WorldStore, WorldStoreError};