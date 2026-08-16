//! .hdoor V1 manifest — the stable, versioned, language-neutral contract
//! of a Harness world package.
//!
//! The manifest alone defines what a package IS. The on-disk layout may
//! evolve (directory today, archive tomorrow); the manifest contract is
//! what external tools, SDKs and future renderers must honor.

use serde::{Deserialize, Serialize};

pub const HDOR_SCHEMA_VERSION: u32 = 1;
pub const PACKAGE_KIND: &str = "hdoor";

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CompilerInfo {
    pub name: String,
    pub version: String,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SourceFile {
    pub name: String,
    pub sha256: String,
    pub bytes: u64,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SourceIdentity {
    /// Combined identity: "sha256:<hash of the concatenation of all source files>".
    pub identity: String,
    pub files: Vec<SourceFile>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CanonCounts {
    pub entities: usize,
    pub locations: usize,
    pub facts: usize,
    pub timeline_events: usize,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct BranchPolicy {
    pub supported: bool,
    /// Canon is immutable: runtime actions never rewrite source history.
    pub immutable_canon: bool,
}

/// Language-neutral V1 world package manifest.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct HDoorManifest {
    pub schema_version: u32,
    pub world_id: String,
    pub title: String,
    pub package_kind: String,
    pub compiler: CompilerInfo,
    pub source: SourceIdentity,
    pub index_version: u32,
    pub canon: CanonCounts,
    /// Modes this package can open into (story/chat/watcher/traveller/...).
    pub modes: Vec<String>,
    /// Declared capabilities (e.g. "offline_lexical_v1", "deterministic_actions_v1").
    pub capabilities: Vec<String>,
    pub branches: BranchPolicy,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> HDoorManifest {
        HDoorManifest {
            schema_version: HDOR_SCHEMA_VERSION,
            world_id: "w-1".into(),
            title: "Sample".into(),
            package_kind: PACKAGE_KIND.into(),
            compiler: CompilerInfo {
                name: "test".into(),
                version: "0.0.0".into(),
            },
            source: SourceIdentity {
                identity: "sha256:abc".into(),
                files: vec![SourceFile {
                    name: "source.txt".into(),
                    sha256: "abc".into(),
                    bytes: 3,
                }],
            },
            index_version: 1,
            canon: CanonCounts {
                entities: 0,
                locations: 0,
                facts: 0,
                timeline_events: 0,
            },
            modes: vec!["traveller".into()],
            capabilities: vec!["offline_lexical_v1".into()],
            branches: BranchPolicy {
                supported: true,
                immutable_canon: true,
            },
        }
    }

    #[test]
    fn manifest_round_trips() {
        let manifest = sample();
        let encoded = serde_json::to_string_pretty(&manifest).unwrap();
        let decoded: HDoorManifest = serde_json::from_str(&encoded).unwrap();
        assert_eq!(manifest, decoded);
        assert_eq!(decoded.schema_version, HDOR_SCHEMA_VERSION);
        assert_eq!(decoded.package_kind, "hdoor");
    }

    #[test]
    fn unknown_manifest_fields_are_rejected() {
        let encoded = r#"{"schema_version":1,"world_id":"w","title":"t","package_kind":"hdoor","compiler":{"name":"c","version":"1"},"source":{"identity":"sha256:x","files":[]},"index_version":1,"canon":{"entities":0,"locations":0,"facts":0,"timeline_events":0},"modes":[],"capabilities":[],"branches":{"supported":true,"immutable_canon":true},"bogus":1}"#;
        let result: Result<HDoorManifest, _> = serde_json::from_str(encoded);
        assert!(result.is_err());
    }

    #[test]
    fn wrong_schema_version_is_detectable() {
        let mut manifest = sample();
        manifest.schema_version = 2;
        let encoded = serde_json::to_string(&manifest).unwrap();
        let decoded: HDoorManifest = serde_json::from_str(&encoded).unwrap();
        assert_ne!(decoded.schema_version, HDOR_SCHEMA_VERSION);
    }
}