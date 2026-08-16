//! .hdoor V1 package I/O: directory layout, strict validation, source
//! hash verification. The external contract is the manifest; the layout
//! below is the v1 carrier.

use crate::compiler::{CompiledWorld, Entity, Fact, Index, Location, SeedState, TimelineEvent};
use crate::manifest::{HDoorManifest, HDOR_SCHEMA_VERSION, PACKAGE_KIND};
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub enum PackageError {
    MissingManifest(PathBuf),
    WrongSchemaVersion(u32),
    WrongPackageKind(String),
    CorruptEntry(String),
    SourceHashMismatch { expected: String, actual: String },
    Io(String),
}

impl std::fmt::Display for PackageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PackageError::MissingManifest(path) => {
                write!(f, "package has no manifest.json at {}", path.display())
            }
            PackageError::WrongSchemaVersion(version) => {
                write!(f, "unsupported .hdoor schema version: {}", version)
            }
            PackageError::WrongPackageKind(kind) => {
                write!(f, "not a .hdoor package (kind: {})", kind)
            }
            PackageError::CorruptEntry(name) => write!(f, "corrupt package entry: {}", name),
            PackageError::SourceHashMismatch { expected, actual } => write!(
                f,
                "source identity mismatch: manifest says {}, file hashes to {}",
                expected, actual
            ),
            PackageError::Io(message) => write!(f, "package io: {}", message),
        }
    }
}

impl std::error::Error for PackageError {}

/// A validated, opened .hdoor package (canon only — runtime state never
/// lives inside the package).
#[derive(Debug)]
pub struct Package {
    pub manifest: HDoorManifest,
    pub entities: Vec<Entity>,
    pub locations: Vec<Location>,
    pub timeline: Vec<TimelineEvent>,
    pub facts: Vec<Fact>,
    pub index: Index,
    pub seed: SeedState,
    pub canon_source: String,
    pub canon_source_hash: String,
}

const MANIFEST: &str = "manifest.json";
const SOURCE: &str = "canon/source.txt";
const ENTITIES: &str = "canon/entities.json";
const LOCATIONS: &str = "canon/locations.json";
const TIMELINE: &str = "canon/timeline.json";
const FACTS: &str = "canon/facts.json";
const INDEX: &str = "index.json";
const SEED: &str = "seed_state.json";

fn read_json<T: serde::de::DeserializeOwned>(dir: &Path, name: &str) -> Result<T, PackageError> {
    let path = dir.join(name);
    let bytes = std::fs::read(&path).map_err(|err| PackageError::Io(err.to_string()))?;
    serde_json::from_slice(&bytes)
        .map_err(|err| PackageError::CorruptEntry(format!("{}: {}", name, err)))
}

fn write_json<T: serde::Serialize>(dir: &Path, name: &str, value: &T) -> Result<(), PackageError> {
    let path = dir.join(name);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|err| PackageError::Io(err.to_string()))?;
    }
    let bytes = serde_json::to_vec_pretty(value)
        .map_err(|err| PackageError::Io(err.to_string()))?;
    std::fs::write(&path, bytes).map_err(|err| PackageError::Io(err.to_string()))
}

/// Write a compiled world as a .hdoor v1 directory package.
pub fn write_package(dir: &Path, world: &CompiledWorld) -> Result<(), PackageError> {
    std::fs::create_dir_all(dir.join("canon")).map_err(|err| PackageError::Io(err.to_string()))?;
    write_json(dir, MANIFEST, &world.manifest)?;
    // verbatim source copy (canon)
    let source_path = dir.join(SOURCE);
    std::fs::write(&source_path, &world.source_text)
        .map_err(|err| PackageError::Io(err.to_string()))?;
    write_json(dir, ENTITIES, &world.entities)?;
    write_json(dir, LOCATIONS, &world.locations)?;
    write_json(dir, TIMELINE, &world.timeline)?;
    write_json(dir, FACTS, &world.facts)?;
    write_json(dir, INDEX, &world.index)?;
    write_json(dir, SEED, &world.seed)?;
    Ok(())
}

/// Validate a package directory without fully opening it.
pub fn validate_package(dir: &Path) -> Result<(), PackageError> {
    if !dir.join(MANIFEST).exists() {
        return Err(PackageError::MissingManifest(dir.to_path_buf()));
    }
    let manifest: HDoorManifest = read_json(dir, MANIFEST)?;
    if manifest.schema_version != HDOR_SCHEMA_VERSION {
        return Err(PackageError::WrongSchemaVersion(manifest.schema_version));
    }
    if manifest.package_kind != PACKAGE_KIND {
        return Err(PackageError::WrongPackageKind(manifest.package_kind));
    }
    let source_path = dir.join(SOURCE);
    let source_bytes =
        std::fs::read(&source_path).map_err(|err| PackageError::Io(err.to_string()))?;
    let actual = crate::compiler::sha256_hex(&source_bytes);
    let expected = manifest
        .source
        .files
        .first()
        .map(|f| f.sha256.clone())
        .unwrap_or_default();
    if !expected.is_empty() && actual != expected {
        return Err(PackageError::SourceHashMismatch { expected, actual });
    }
    // structural entries must parse
    let _: Vec<Entity> = read_json(dir, ENTITIES)?;
    let _: Vec<Location> = read_json(dir, LOCATIONS)?;
    let _: Vec<TimelineEvent> = read_json(dir, TIMELINE)?;
    let _: Vec<Fact> = read_json(dir, FACTS)?;
    let _: Index = read_json(dir, INDEX)?;
    let _: SeedState = read_json(dir, SEED)?;
    Ok(())
}

/// Open and fully validate a .hdoor package.
pub fn read_package(dir: &Path) -> Result<Package, PackageError> {
    validate_package(dir)?;
    let manifest: HDoorManifest = read_json(dir, MANIFEST)?;
    let entities: Vec<Entity> = read_json(dir, ENTITIES)?;
    let locations: Vec<Location> = read_json(dir, LOCATIONS)?;
    let timeline: Vec<TimelineEvent> = read_json(dir, TIMELINE)?;
    let facts: Vec<Fact> = read_json(dir, FACTS)?;
    let index: Index = read_json(dir, INDEX)?;
    let seed: SeedState = read_json(dir, SEED)?;
    let canon_source =
        std::fs::read_to_string(dir.join(SOURCE)).map_err(|err| PackageError::Io(err.to_string()))?;
    let canon_source_hash = crate::compiler::sha256_hex(canon_source.as_bytes());
    Ok(Package {
        manifest,
        entities,
        locations,
        timeline,
        facts,
        index,
        seed,
        canon_source,
        canon_source_hash,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compiler::{Compiler, sha256_hex};

    const FIXTURE: &str = "The Hollow Keep\n\n## The Keeper\n\nKeeper Sarn guards the Hollow Keep. The Silver Key is hidden in the Hall of Embers. Keeper Sarn keeps the Bronze Key in the Deep Well.\n\n## The Garden of Ash\n\nThe Garden of Ash lies beyond the Iron Gate. The Deep Well stands at the center of the Garden of Ash.";

    fn compile_to(dir: &Path) -> CompiledWorld {
        let world = Compiler::new().compile("fixture", "The Hollow Keep", "fixture.txt", FIXTURE);
        write_package(dir, &world).unwrap();
        world
    }

    #[test]
    fn package_round_trip() {
        let tmp = std::env::temp_dir().join(format!("hdoor-rt-{}", std::process::id()));
        compile_to(&tmp);
        let opened = read_package(&tmp).unwrap();
        assert_eq!(opened.manifest.schema_version, 1);
        assert_eq!(opened.manifest.world_id, "fixture");
        assert_eq!(opened.canon_source, FIXTURE);
        assert_eq!(opened.canon_source_hash, sha256_hex(FIXTURE.as_bytes()));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn tampered_source_is_rejected() {
        let tmp = std::env::temp_dir().join(format!("hdoor-tamper-{}", std::process::id()));
        compile_to(&tmp);
        std::fs::write(tmp.join("canon/source.txt"), "tampered").unwrap();
        let result = read_package(&tmp);
        assert!(matches!(result, Err(PackageError::SourceHashMismatch { .. })));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn missing_manifest_is_rejected() {
        let tmp = std::env::temp_dir().join(format!("hdoor-nomanifest-{}", std::process::id()));
        std::fs::create_dir_all(&tmp).unwrap();
        let result = read_package(&tmp);
        assert!(matches!(result, Err(PackageError::MissingManifest(_))));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn wrong_schema_version_is_rejected() {
        let tmp = std::env::temp_dir().join(format!("hdoor-schema-{}", std::process::id()));
        let world = compile_to(&tmp);
        let mut manifest = world.manifest.clone();
        manifest.schema_version = 99;
        write_json(&tmp, MANIFEST, &manifest).unwrap();
        let result = read_package(&tmp);
        assert!(matches!(result, Err(PackageError::WrongSchemaVersion(99))));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn corrupt_entry_is_rejected() {
        let tmp = std::env::temp_dir().join(format!("hdoor-corrupt-{}", std::process::id()));
        compile_to(&tmp);
        std::fs::write(tmp.join("index.json"), "{not json").unwrap();
        let result = read_package(&tmp);
        assert!(matches!(result, Err(PackageError::CorruptEntry(_))));
        let _ = std::fs::remove_dir_all(&tmp);
    }
}